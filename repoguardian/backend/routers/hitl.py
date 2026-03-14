"""
HITL action router.

POST /api/hitl/{finding_id}/action  — approve, reject, snooze, explain a finding
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.hitl_gateway import HITLGatewayAgent
from backend.config import get_settings
from backend.models.database import get_db
from backend.models.schemas import HITLActionRequest, HITLActionResponse
from backend.services.github_service import GitHubAPIClient
from backend.services.redis_service import StateStore, get_redis

router = APIRouter(prefix="/api/hitl", tags=["hitl"])
settings = get_settings()


@router.post("/{finding_id}/action", response_model=HITLActionResponse)
async def hitl_action(
    finding_id: str,
    body: HITLActionRequest,
    x_actor: str = Header(default="api-user"),
    db: AsyncSession = Depends(get_db),
) -> HITLActionResponse:
    """
    Process a HITL action on a specific finding.

    This endpoint is called directly by the dashboard UI.
    (GitHub webhook-based commands go through /webhooks/github/comment)

    The X-Actor header identifies the user taking the action.
    """
    redis = await get_redis()
    state = StateStore(redis)
    github_client = GitHubAPIClient(settings.github_token)
    gateway = HITLGatewayAgent(github_client, state)

    try:
        return await gateway.handle_command(
            finding_id=finding_id,
            request=body,
            actor=x_actor,
            db=db,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
