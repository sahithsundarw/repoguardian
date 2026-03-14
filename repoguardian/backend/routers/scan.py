"""
On-demand full repository scan endpoint.

POST /api/repositories/{repo_id}/scan
  — Triggers a full codebase scan (not just a PR diff).
  — Queues a FULL_SCAN event onto the Redis stream.
  — Returns immediately; the worker processes it asynchronously.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import EventType, Platform, Repository, get_db
from backend.models.schemas import WebhookEvent
from backend.services.redis_service import EventQueueProducer, get_redis

router = APIRouter(prefix="/api/repositories", tags=["scan"])


@router.post("/{repo_id}/scan")
async def trigger_full_scan(
    repo_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Queue a full-repo scan for an already-registered repository."""
    try:
        rid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id")

    result = await db.execute(select(Repository).where(Repository.id == rid))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not repo.is_active:
        raise HTTPException(status_code=400, detail="Repository is not active")

    event = WebhookEvent(
        event_type=EventType.FULL_SCAN,
        platform=Platform.GITHUB,
        repo_full_name=repo.full_name,
        repo_clone_url=repo.clone_url,
        repo_default_branch=repo.default_branch or "main",
    )

    redis = await get_redis()
    producer = EventQueueProducer(redis)
    entry_id = await producer.publish(event)

    return {
        "status": "accepted",
        "event_id": event.event_id,
        "repo": repo.full_name,
        "stream_entry": entry_id,
    }
