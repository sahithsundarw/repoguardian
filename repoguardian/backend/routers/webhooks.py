"""
Webhook ingestion router.

POST /webhooks/github  — receives GitHub webhook events
POST /webhooks/github/comment — receives PR comment events (HITL commands)

Both endpoints:
  1. Verify the HMAC-SHA256 signature
  2. Parse the payload
  3. Normalise to WebhookEvent
  4. Publish to Redis event stream
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status

from backend.config import get_settings
from backend.models.schemas import WebhookEvent
from backend.services.github_service import (
    GitHubAPIClient,
    parse_github_webhook,
    verify_github_signature,
)
from backend.services.redis_service import EventQueueProducer, StateStore, get_redis

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(...),
) -> dict:
    """
    Receive and enqueue a GitHub webhook event.

    Steps:
      1. Verify HMAC signature (reject if invalid — prevents replay attacks)
      2. Normalise to WebhookEvent
      3. Publish to Redis stream (returns immediately — 202 Accepted)
    """
    raw_body = await request.body()

    # ── 1. Signature verification ──────────────────────────────────────────────
    if not verify_github_signature(raw_body, x_hub_signature_256):
        logger.warning("Invalid GitHub webhook signature — rejecting")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # ── 2. Parse payload ───────────────────────────────────────────────────────
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        )

    event = parse_github_webhook(x_github_event, payload)
    if event is None:
        # Unsupported event type or action — acknowledge and discard
        return {"status": "ignored", "reason": "unsupported event type or action"}

    # ── 3. Enqueue ─────────────────────────────────────────────────────────────
    background_tasks.add_task(_enqueue_event, event)

    logger.info(
        "Received %s event for %s PR#%s → enqueued",
        x_github_event, event.repo_full_name, event.pr_number,
    )
    return {
        "status": "accepted",
        "event_id": event.event_id,
        "event_type": event.event_type.value,
    }


@router.post("/github/comment", status_code=status.HTTP_200_OK)
async def github_pr_comment_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: str = Header(...),
) -> dict:
    """
    Handle PR comment events for HITL bot commands.
    When a developer posts /ai-approve, /ai-reject, etc., GitHub
    fires a 'issue_comment' webhook which is routed here.
    """
    raw_body = await request.body()

    if not verify_github_signature(raw_body, x_hub_signature_256):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    if x_github_event not in ("issue_comment", "pull_request_review_comment"):
        return {"status": "ignored"}

    payload = await request.json()
    action = payload.get("action", "")
    if action != "created":
        return {"status": "ignored"}

    comment_body = payload.get("comment", {}).get("body", "")
    commenter = payload.get("comment", {}).get("user", {}).get("login", "unknown")
    pr_number = payload.get("issue", {}).get("number") or payload.get("pull_request", {}).get("number")

    from backend.services.github_service import GitHubAPIClient

    command = GitHubAPIClient.parse_bot_command(comment_body)
    if not command:
        return {"status": "no_command"}

    # Handle the HITL command
    from backend.models.database import AsyncSessionLocal
    from backend.agents.hitl_gateway import HITLGatewayAgent
    from backend.models.schemas import HITLActionRequest

    async with AsyncSessionLocal() as db:
        redis = await get_redis()
        state = StateStore(redis)
        github_client = GitHubAPIClient(settings.github_token)
        gateway = HITLGatewayAgent(github_client, state)

        req = HITLActionRequest(
            action=command["action"],
            reason_code=command.get("reason_code"),
            snooze_days=command.get("snooze_days"),
        )
        try:
            response = await gateway.handle_command(
                finding_id=command["finding_id"],
                request=req,
                actor=commenter,
                db=db,
            )
            logger.info("HITL command '%s' on finding %s by %s",
                        command["action"], command["finding_id"], commenter)
            return {"status": "processed", "message": response.message}
        except Exception as e:
            logger.error("HITL command processing failed: %s", e)
            return {"status": "error", "detail": str(e)}


# ── Background task ────────────────────────────────────────────────────────────

async def _enqueue_event(event: WebhookEvent) -> None:
    """Publish the event to the Redis stream (runs as a background task)."""
    try:
        redis = await get_redis()
        producer = EventQueueProducer(redis)
        await producer.publish(event)
    except Exception as e:
        logger.error("Failed to enqueue event %s: %s", event.event_id, e)
