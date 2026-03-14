"""
Repository management router.

POST   /api/repositories          — register a new repository
GET    /api/repositories          — list all registered repos
GET    /api/repositories/{repo_id} — get repo details
DELETE /api/repositories/{repo_id} — deactivate a repo
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import Repository, get_db
from backend.models.schemas import RepositoryCreate, RepositoryResponse

router = APIRouter(prefix="/api/repositories", tags=["repositories"])


@router.post("", response_model=RepositoryResponse, status_code=status.HTTP_201_CREATED)
async def register_repository(
    body: RepositoryCreate,
    db: AsyncSession = Depends(get_db),
) -> RepositoryResponse:
    """Register a new repository for monitoring."""
    full_name = f"{body.owner}/{body.name}"

    # Check for duplicates
    stmt = select(Repository).where(
        Repository.platform == body.platform,
        Repository.full_name == full_name,
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Repository {full_name} is already registered.",
        )

    repo = Repository(
        platform=body.platform,
        owner=body.owner,
        name=body.name,
        full_name=full_name,
        clone_url=body.clone_url,
        default_branch=body.default_branch,
        config=body.config,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return RepositoryResponse(
        id=str(repo.id),
        platform=repo.platform,
        full_name=repo.full_name,
        clone_url=repo.clone_url,
        default_branch=repo.default_branch,
        primary_language=repo.primary_language,
        is_active=repo.is_active,
        created_at=repo.created_at,
    )


@router.get("", response_model=list[RepositoryResponse])
async def list_repositories(
    db: AsyncSession = Depends(get_db),
) -> list[RepositoryResponse]:
    """List all registered repositories."""
    result = await db.execute(select(Repository).where(Repository.is_active == True))
    repos = result.scalars().all()
    return [
        RepositoryResponse(
            id=str(r.id),
            platform=r.platform,
            full_name=r.full_name,
            clone_url=r.clone_url,
            default_branch=r.default_branch,
            primary_language=r.primary_language,
            is_active=r.is_active,
            created_at=r.created_at,
        )
        for r in repos
    ]


@router.get("/{repo_id}", response_model=RepositoryResponse)
async def get_repository(
    repo_id: str,
    db: AsyncSession = Depends(get_db),
) -> RepositoryResponse:
    """Get details for a specific repository."""
    try:
        rid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id")

    result = await db.execute(select(Repository).where(Repository.id == rid))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    return RepositoryResponse(
        id=str(repo.id),
        platform=repo.platform,
        full_name=repo.full_name,
        clone_url=repo.clone_url,
        default_branch=repo.default_branch,
        primary_language=repo.primary_language,
        is_active=repo.is_active,
        created_at=repo.created_at,
    )


@router.delete("/{repo_id}", status_code=status.HTTP_200_OK)
async def deactivate_repository(
    repo_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete (deactivate) a repository."""
    try:
        rid = uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid repo_id")

    result = await db.execute(select(Repository).where(Repository.id == rid))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")

    repo.is_active = False
    await db.commit()
