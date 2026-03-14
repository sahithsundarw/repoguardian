"""
Repository health dashboard router.

GET  /api/health/{repo_id}          — full dashboard payload
GET  /api/health/{repo_id}/score    — current score only
GET  /api/health/{repo_id}/trend    — 30-day trend data
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.health_aggregator import HealthAggregatorAgent
from backend.models.database import get_db
from backend.models.schemas import HealthDashboard

router = APIRouter(prefix="/api/health", tags=["health"])
_health_agent = HealthAggregatorAgent()


@router.get("/{repo_id}", response_model=HealthDashboard)
async def get_health_dashboard(
    repo_id: str,
    db: AsyncSession = Depends(get_db),
) -> HealthDashboard:
    """Return the full health dashboard for a repository."""
    try:
        rid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id format")

    dashboard = await _health_agent.get_dashboard(rid, db)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Repository not found")

    return dashboard


@router.get("/{repo_id}/score")
async def get_health_score(
    repo_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return just the current health score and grade."""
    try:
        rid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id format")

    dashboard = await _health_agent.get_dashboard(rid, db)
    if not dashboard:
        raise HTTPException(status_code=404, detail="Repository not found")

    return {
        "repo_id": repo_id,
        "overall_score": dashboard.overall_score,
        "grade": dashboard.grade.value,
        "trend_delta_7d": dashboard.trend_delta_7d,
        "trend_velocity": dashboard.trend_velocity,
    }
