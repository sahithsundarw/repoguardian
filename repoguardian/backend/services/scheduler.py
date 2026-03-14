"""
APScheduler-based periodic audit scheduler.

On startup, loads all registered repositories that have an audit_schedule
in their config JSONB and registers recurring APScheduler jobs.

Each job publishes a FULL_SCAN WebhookEvent to the Redis stream,
which the background worker then picks up and processes normally.

Schedule formats accepted:
  "hourly"         — every 1 hour
  "daily"          — every day at 02:00 UTC
  "weekly"         — every Monday at 02:00 UTC
  "<cron expr>"    — 5-part cron string, e.g. "0 9 * * 1"
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

SCHEDULE_PRESETS: dict[str, object] = {
    "hourly": IntervalTrigger(hours=1),
    "daily":  CronTrigger(hour=2, minute=0),
    "weekly": CronTrigger(day_of_week="mon", hour=2, minute=0),
}


async def start_scheduler(db_factory) -> AsyncIOScheduler:
    """
    Start the global scheduler and load all existing repo schedules from DB.
    Call this once from main.py lifespan startup.
    """
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")

    # Load all active repos with an audit_schedule
    try:
        from sqlalchemy import select
        from backend.models.database import Repository

        async with db_factory() as db:
            result = await db.execute(
                select(Repository).where(Repository.is_active == True)
            )
            repos = result.scalars().all()

        for repo in repos:
            schedule_str = (repo.config or {}).get("audit_schedule")
            if schedule_str:
                _add_job(
                    _scheduler,
                    str(repo.id),
                    repo.full_name,
                    repo.default_branch or "main",
                    schedule_str,
                )
    except Exception as e:
        logger.warning("Could not load scheduled repos from DB: %s", e)

    _scheduler.start()
    logger.info("APScheduler started with %d jobs", len(_scheduler.get_jobs()))
    return _scheduler


def add_repo_job(
    repo_id: str,
    full_name: str,
    default_branch: str,
    schedule_str: str,
) -> None:
    """Register or replace a periodic audit job for a repo."""
    if _scheduler is None:
        logger.warning("Scheduler not started — skipping job for %s", full_name)
        return
    _add_job(_scheduler, repo_id, full_name, default_branch, schedule_str)
    logger.info("Scheduled '%s' audit for %s", schedule_str, full_name)


def remove_repo_job(repo_id: str) -> None:
    """Remove a scheduled job when a repo is deactivated."""
    if _scheduler is None:
        return
    job_id = f"audit_{repo_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info("Removed scheduled job %s", job_id)


def stop_scheduler() -> None:
    """Graceful shutdown — called from main.py lifespan cleanup."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")


# ── Internal helpers ───────────────────────────────────────────────────────────


def _add_job(
    scheduler: AsyncIOScheduler,
    repo_id: str,
    full_name: str,
    default_branch: str,
    schedule_str: str,
) -> None:
    try:
        trigger = _parse_schedule(schedule_str)
    except ValueError as e:
        logger.error("Invalid schedule '%s' for %s: %s — skipping", schedule_str, full_name, e)
        return

    scheduler.add_job(
        _fire_scheduled_audit,
        trigger=trigger,
        id=f"audit_{repo_id}",
        args=[repo_id, full_name, default_branch],
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
    )


def _parse_schedule(s: str):
    if s in SCHEDULE_PRESETS:
        return SCHEDULE_PRESETS[s]
    parts = s.split()
    if len(parts) == 5:
        minute, hour, day, month, day_of_week = parts
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        )
    raise ValueError(f"Unrecognised schedule string: '{s}'. Use hourly/daily/weekly or a 5-part cron.")


async def _fire_scheduled_audit(
    repo_id: str,
    full_name: str,
    default_branch: str,
) -> None:
    """Publish a FULL_SCAN event for the repo to the Redis stream."""
    try:
        from backend.services.redis_service import get_redis, EventQueueProducer
        from backend.models.schemas import WebhookEvent
        from backend.models.database import EventType, Platform

        event = WebhookEvent(
            event_type=EventType.FULL_SCAN,
            platform=Platform.GITHUB,
            repo_full_name=full_name,
            repo_clone_url=f"https://github.com/{full_name}.git",
            repo_default_branch=default_branch,
            raw_payload={"trigger": "scheduled_audit", "repo_id": repo_id},
        )
        redis = await get_redis()
        producer = EventQueueProducer(redis)
        entry_id = await producer.publish(event)
        logger.info(
            "[scheduler] Fired scheduled audit for %s → stream entry %s",
            full_name, entry_id,
        )
    except Exception as e:
        logger.error("[scheduler] Failed to fire audit for %s: %s", full_name, e)
