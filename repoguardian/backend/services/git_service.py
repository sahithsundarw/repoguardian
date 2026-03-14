"""
Git operations service.

Handles repository cloning, file fetching, diff retrieval, and basic
call-graph indexing using ctags/ripgrep as a lightweight fallback
(no language server required).

Design principles:
  - Use shallow clones (--depth 1) to minimise disk and network usage
  - Delete clones after analysis (ephemeral)
  - Cache frequently accessed repos in memory-mapped structures
  - Read-only access only (never push, never modify)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import httpx

from backend.config import get_settings
from backend.models.schemas import CallGraphEdge

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Repository context manager ─────────────────────────────────────────────────


class RepoContext:
    """
    An ephemeral local clone of a repository.
    Used as an async context manager to guarantee cleanup.

    Usage:
        async with RepoContext(clone_url, head_sha) as repo:
            content = await repo.read_file("src/auth.py")
    """

    def __init__(self, clone_url: str, ref: str = "HEAD", token: str = "") -> None:
        self.clone_url = _inject_token(clone_url, token)
        self.ref = ref
        self._tmpdir: str | None = None

    async def __aenter__(self) -> "RepoContext":
        self._tmpdir = tempfile.mkdtemp(prefix="rg_clone_", dir=settings.clone_base_dir)
        await self._clone()
        return self

    async def __aexit__(self, *_) -> None:
        if self._tmpdir and Path(self._tmpdir).exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def _clone(self) -> None:
        """Shallow clone at `self.ref`."""
        os.makedirs(settings.clone_base_dir, exist_ok=True)
        cmd = [
            "git", "clone",
            "--depth", "1",
            "--filter=blob:none",  # partial clone — don't fetch blobs upfront
            self.clone_url,
            self._tmpdir,
        ]
        await _run_subprocess(cmd)

        # Check out the specific SHA if provided
        if self.ref and self.ref != "HEAD":
            await _run_subprocess(
                ["git", "-C", self._tmpdir, "fetch", "--depth", "1", "origin", self.ref]
            )
            await _run_subprocess(
                ["git", "-C", self._tmpdir, "checkout", self.ref]
            )

    # ── File operations ────────────────────────────────────────────────────────

    def read_file(self, relative_path: str) -> str | None:
        """Read a file from the cloned repo. Returns None if not found."""
        if not self._tmpdir:
            return None
        full_path = Path(self._tmpdir) / relative_path
        if not full_path.exists():
            return None
        try:
            return full_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.error("Failed to read %s: %s", relative_path, e)
            return None

    def list_files(self, pattern: str = "**/*") -> list[str]:
        """Glob all files matching the pattern. Returns relative paths."""
        if not self._tmpdir:
            return []
        base = Path(self._tmpdir)
        return [
            str(p.relative_to(base))
            for p in base.glob(pattern)
            if p.is_file() and ".git" not in str(p)
        ]

    def get_directory_tree(self, depth: int = 2) -> str:
        """Return a simple directory tree string (depth-limited)."""
        if not self._tmpdir:
            return ""
        lines: list[str] = [self._tmpdir]
        _build_tree(Path(self._tmpdir), depth, 0, lines)
        return "\n".join(lines)

    def find_files_by_name(self, filename: str) -> list[str]:
        """Find all files matching a given filename (not path)."""
        return [f for f in self.list_files("**/*") if Path(f).name == filename]

    def find_test_files_for(self, source_file: str) -> list[str]:
        """Heuristically find test files related to a source file."""
        stem = Path(source_file).stem
        candidates = []
        for f in self.list_files("**/*.py") + self.list_files("**/*.test.js") + self.list_files("**/*.spec.ts"):
            fn = Path(f).name
            if stem in fn and ("test" in fn.lower() or "spec" in fn.lower()):
                candidates.append(f)
        return candidates

    # ── Dependency manifests ───────────────────────────────────────────────────

    def find_dependency_manifests(self) -> list[str]:
        """Return paths to all dependency manifest files in the repo."""
        manifest_names = {
            "requirements.txt", "requirements-dev.txt", "Pipfile", "pyproject.toml",
            "package.json", "package-lock.json", "yarn.lock",
            "Cargo.toml", "go.mod", "go.sum",
            "pom.xml", "build.gradle", "build.gradle.kts",
            "Gemfile", "composer.json",
        }
        return [f for f in self.list_files("**/*") if Path(f).name in manifest_names]

    # ── Call graph ─────────────────────────────────────────────────────────────

    def build_call_graph_for_symbols(
        self,
        symbol_names: list[str],
        file_paths: list[str],
    ) -> list[CallGraphEdge]:
        """
        Build a lightweight call graph for the given symbols.

        Strategy:
          1. Try to use ctags (universal-ctags) if available
          2. Fall back to regex-based grep search

        Returns direct callers and callees (1-hop neighbourhood).
        """
        if not self._tmpdir:
            return []

        edges: list[CallGraphEdge] = []

        for symbol in symbol_names:
            # Find callers (files that call this symbol)
            caller_files = self._grep_callers(symbol)
            for caller_file, caller_sym in caller_files:
                edges.append(CallGraphEdge(
                    caller_symbol=caller_sym,
                    caller_file=caller_file,
                    callee_symbol=symbol,
                    callee_file=file_paths[0] if file_paths else "unknown",
                ))

        return edges

    def _grep_callers(self, symbol: str) -> list[tuple[str, str]]:
        """Use grep to find files calling `symbol`."""
        if not self._tmpdir:
            return []
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include=*.py", "--include=*.js", "--include=*.ts",
                 "-l", symbol, self._tmpdir],
                capture_output=True, text=True, timeout=10
            )
            callers: list[tuple[str, str]] = []
            for line in result.stdout.strip().splitlines():
                rel_path = line.replace(self._tmpdir + "/", "")
                callers.append((rel_path, f"caller_of_{symbol}"))
            return callers[:5]  # Limit to top 5
        except Exception:
            return []


# ── GitHub API diff fetcher ────────────────────────────────────────────────────


class GitHubDiffFetcher:
    """Fetches PR diffs directly from the GitHub REST API."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def fetch_pr_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Fetch the full unified diff for a pull request."""
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3.diff",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.text

    async def fetch_file_content(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str = "HEAD",
    ) -> str | None:
        """Fetch a single file's content from GitHub."""
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3.raw",
        }
        params = {"ref": ref}
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as e:
                logger.error("Failed to fetch %s: %s", file_path, e)
                return None

    async def get_repo_tree(self, owner: str, repo: str, ref: str = "HEAD") -> list[str]:
        """
        Fetch the file tree of a repo (non-recursive, using Git Trees API).
        Returns a flat list of file paths.
        """
        url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}"
        headers = {"Authorization": f"Bearer {self._token}"}
        params = {"recursive": "1"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
                return [
                    item["path"]
                    for item in data.get("tree", [])
                    if item["type"] == "blob"
                ]
            except Exception as e:
                logger.error("Failed to fetch repo tree: %s", e)
                return []


# ── Helpers ────────────────────────────────────────────────────────────────────


def _inject_token(clone_url: str, token: str) -> str:
    """Inject a GitHub token into an HTTPS clone URL for authentication."""
    if not token or not clone_url.startswith("https://"):
        return clone_url
    return clone_url.replace("https://", f"https://{token}@", 1)


async def _run_subprocess(cmd: list[str], timeout: int = 60) -> str:
    """Run a subprocess asynchronously, raising on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"Command timed out after {timeout}s: {' '.join(cmd)}")

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, stdout, stderr
        )
    return stdout.decode("utf-8", errors="replace")


def _build_tree(path: Path, max_depth: int, current_depth: int, lines: list[str]) -> None:
    """Recursively build a tree string, skipping .git and __pycache__."""
    if current_depth >= max_depth:
        return
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
    except PermissionError:
        return
    for entry in entries:
        if entry.name in (".git", "__pycache__", "node_modules", ".venv"):
            continue
        indent = "  " * (current_depth + 1)
        lines.append(f"{indent}{'📁' if entry.is_dir() else '📄'} {entry.name}")
        if entry.is_dir():
            _build_tree(entry, max_depth, current_depth + 1, lines)
