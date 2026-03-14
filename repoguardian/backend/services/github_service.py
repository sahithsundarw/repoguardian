"""
GitHub API service.

Handles:
  - Webhook signature verification (HMAC-SHA256)
  - Normalising GitHub webhook payloads into WebhookEvent objects
  - Posting PR review comments and inline annotations
  - Managing PR status checks
  - Parsing HITL bot commands from PR comments (/ai-approve, /ai-reject, etc.)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from backend.config import get_settings
from backend.models.schemas import WebhookEvent
from backend.models.database import EventType, Platform

logger = logging.getLogger(__name__)
settings = get_settings()

# ── HMAC Signature Verification ───────────────────────────────────────────────


def verify_github_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Verify the X-Hub-Signature-256 header sent by GitHub.

    Returns True if the signature is valid, False otherwise.
    Always use a constant-time comparison to prevent timing attacks.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)


# ── Payload Normalisation ──────────────────────────────────────────────────────


def parse_github_webhook(event_name: str, payload: dict[str, Any]) -> WebhookEvent | None:
    """
    Convert a raw GitHub webhook payload into a normalised WebhookEvent.

    Supported event_name values (X-GitHub-Event header):
      pull_request  → PR_OPEN or PR_UPDATE
      push          → PUSH_TO_MAIN (only for pushes to the default branch)
      pull_request_review_comment → HITL bot command

    Returns None for unsupported or irrelevant events (e.g. PR closed without merge).
    """
    action = payload.get("action", "")
    repo_data = payload.get("repository", {})

    repo_full_name = repo_data.get("full_name", "")
    clone_url = repo_data.get("clone_url", "")
    default_branch = repo_data.get("default_branch", "main")

    if event_name == "pull_request":
        if action not in ("opened", "synchronize", "reopened"):
            return None

        pr = payload.get("pull_request", {})
        event_type = EventType.PR_OPEN if action == "opened" else EventType.PR_UPDATE

        return WebhookEvent(
            event_type=event_type,
            platform=Platform.GITHUB,
            repo_full_name=repo_full_name,
            repo_clone_url=clone_url,
            repo_default_branch=default_branch,
            pr_number=pr.get("number"),
            pr_title=pr.get("title"),
            pr_description=pr.get("body", ""),
            pr_author=pr.get("user", {}).get("login"),
            base_sha=pr.get("base", {}).get("sha"),
            head_sha=pr.get("head", {}).get("sha"),
            base_branch=pr.get("base", {}).get("ref"),
            head_branch=pr.get("head", {}).get("ref"),
            diff_url=pr.get("diff_url"),
            raw_payload=payload,
        )

    elif event_name == "push":
        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        if branch != default_branch:
            return None  # Only analyse pushes to the default branch

        return WebhookEvent(
            event_type=EventType.PUSH_TO_MAIN,
            platform=Platform.GITHUB,
            repo_full_name=repo_full_name,
            repo_clone_url=clone_url,
            repo_default_branch=default_branch,
            head_sha=payload.get("after"),
            base_sha=payload.get("before"),
            raw_payload=payload,
        )

    return None


# ── GitHub API Client ──────────────────────────────────────────────────────────


class GitHubAPIClient:
    """
    Async GitHub REST API client for writing back to PRs.
    Uses a Personal Access Token or GitHub App installation token.
    """

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── PR Comments ────────────────────────────────────────────────────────────

    async def post_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        """Post a general comment to a pull request."""
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json={"body": body}, headers=self._headers)
            response.raise_for_status()
            return response.json()

    async def post_inline_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_id: str,
        file_path: str,
        line: int,
        body: str,
        side: str = "RIGHT",
    ) -> dict[str, Any]:
        """
        Post an inline comment on a specific line in the PR diff.

        Args:
            commit_id:  The head SHA of the PR.
            file_path:  Relative file path in the repo.
            line:       Line number in the **new** file.
            side:       "RIGHT" for the new version (additions), "LEFT" for removals.
        """
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        payload = {
            "body": body,
            "commit_id": commit_id,
            "path": file_path,
            "line": line,
            "side": side,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=self._headers)
            response.raise_for_status()
            return response.json()

    async def create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_id: str,
        verdict: str,
        summary_body: str,
        inline_comments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Submit a full PR review (summary + inline comments as a single review).

        Args:
            verdict:  "APPROVE", "REQUEST_CHANGES", or "COMMENT".
            inline_comments: List of {path, line, body} dicts.
        """
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

        # Convert our generic inline_comments to GitHub's review comment format
        comments = [
            {
                "path": c["path"],
                "line": c["line"],
                "body": c["body"],
                "side": c.get("side", "RIGHT"),
            }
            for c in inline_comments
            if c.get("path") and c.get("line")
        ]

        payload = {
            "commit_id": commit_id,
            "body": summary_body,
            "event": verdict,
            "comments": comments,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=self._headers)
            response.raise_for_status()
            return response.json()

    # ── Status Checks ──────────────────────────────────────────────────────────

    async def set_commit_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        state: str,  # "pending" | "success" | "failure" | "error"
        description: str,
        context: str = "RepoGuardian AI Review",
        target_url: str | None = None,
    ) -> dict[str, Any]:
        """Set a status check on a commit (shows in PR checks panel)."""
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/statuses/{sha}"
        payload = {
            "state": state,
            "description": description[:139],  # GitHub limit
            "context": context,
        }
        if target_url:
            payload["target_url"] = target_url

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=self._headers)
            response.raise_for_status()
            return response.json()

    # ── HITL bot command parsing ───────────────────────────────────────────────

    @staticmethod
    def parse_bot_command(comment_body: str) -> dict[str, Any] | None:
        """
        Parse an AI bot command from a PR comment.

        Supported commands:
          /ai-approve <finding_id>
          /ai-reject <finding_id> [reason_code]
          /ai-snooze <finding_id> <days>d
          /ai-explain <finding_id>

        Returns a dict with action, finding_id, and optional parameters,
        or None if no valid command found.
        """
        import re
        body = comment_body.strip()

        patterns = {
            "approve": re.compile(r"^/ai-approve\s+(\S+)", re.IGNORECASE),
            "reject":  re.compile(r"^/ai-reject\s+(\S+)(?:\s+(\S+))?", re.IGNORECASE),
            "snooze":  re.compile(r"^/ai-snooze\s+(\S+)\s+(\d+)d", re.IGNORECASE),
            "explain": re.compile(r"^/ai-explain\s+(\S+)", re.IGNORECASE),
        }

        for action, pattern in patterns.items():
            match = pattern.match(body)
            if match:
                result: dict[str, Any] = {
                    "action": action,
                    "finding_id": match.group(1),
                }
                if action == "reject" and match.lastindex >= 2 and match.group(2):
                    result["reason_code"] = match.group(2)
                if action == "snooze":
                    result["snooze_days"] = int(match.group(2))
                return result

        return None
