"""
Ephemeral (Flash Audit) scan router.

POST /api/scan/ephemeral        — queue a one-off analysis of any public repo
GET  /api/scan/ephemeral/{sid}  — poll for results (stored in Redis, 1-hr TTL)

No database writes happen for ephemeral scans.
Results are stored in Redis under key `repoguardian:state:ephemeral:{session_id}`.
"""

from __future__ import annotations

import redwabjiiiiiiiiiiiiiiiiiiiiiiiiiii
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException

from backend.models.database import EventType, Platform
from backend.models.schemas import (
    EphemeralScanRequest,
    EphemeralScanStatus,
    WebhookEvent,
)
from backend.services.redis_service import EventQueueProducer, StateStore, get_redis

router = APIRouter(prefix="/api/scan", tags=["scan"])

_GITHUB_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?(?:/.*)?$"
)


def _parse_github_url(url: str):
    """Extract (owner, repo_name) from a GitHub URL. Returns None if invalid."""
    m = _GITHUB_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


@router.post("/ephemeral", status_code=202)
async def start_ephemeral_scan(body: EphemeralScanRequest) -> dict:
    """
    Queue a one-time, ephemeral analysis of any public GitHub repository.
    Returns a session_id to poll for results.
    """
    parsed = _parse_github_url(body.repo_url)
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="Invalid GitHub URL. Expected format: https://github.com/owner/repo",
        )
    owner, repo_name = parsed
    full_name = f"{owner}/{repo_name}"
    session_id = str(uuid.uuid4())

    redis = await get_redis()
    state = StateStore(redis)

    # Store initial status so the GET endpoint returns immediately
    initial = EphemeralScanStatus(
        session_id=session_id,
        status="pending",
        repo_full_name=full_name,
        created_at=datetime.now(timezone.utc),
    )
    await state.set(
        f"ephemeral:{session_id}",
        initial.model_dump(mode="json"),
        ttl=timedelta(hours=1),
    )

    # Publish event to Redis stream — worker will pick this up
    event = WebhookEvent(
        event_type=EventType.FULL_SCAN,
        platform=Platform.GITHUB,
        repo_full_name=full_name,
        repo_clone_url=f"https://github.com/{full_name}.git",
        repo_default_branch="main",
        is_ephemeral=True,
        ephemeral_session_id=session_id,
    )
    producer = EventQueueProducer(redis)
    await producer.publish(event)

    return {"session_id": session_id, "repo_full_name": full_name}


@router.get("/ephemeral/{session_id}", response_model=EphemeralScanStatus)
async def get_ephemeral_result(session_id: str) -> EphemeralScanStatus:
    """
    Poll for the result of an ephemeral scan.
    Returns 404 if the session has expired (> 1 hr) or never existed.
    """
    redis = await get_redis()
    state = StateStore(redis)
    data = await state.get(f"ephemeral:{session_id}")

    if data is None:
        raise HTTPException(
            status_code=404,
            detail="Ephemeral scan session not found or expired.",
        )

    return EphemeralScanStatus(**data)
