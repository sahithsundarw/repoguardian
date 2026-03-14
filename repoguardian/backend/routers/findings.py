"""
Findings router.

GET  /api/findings                  — list findings (filterable)
GET  /api/findings/{finding_id}     — get single finding details
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import Finding, FindingStatus, Severity, get_db
from backend.models.schemas import FindingResponse

router = APIRouter(prefix="/api/findings", tags=["findings"])


@router.get("", response_model=list[FindingResponse])
async def list_findings(
    repo_id: str | None = Query(None),
    severity: str | None = Query(None),
    status: str | None = Query(None),
    pr_number: int | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
) -> list[FindingResponse]:
    """
    List findings with optional filters.

    Query params:
      repo_id:   Filter by repository UUID
      severity:  CRITICAL | HIGH | MEDIUM | LOW | INFO
      status:    open | approved | rejected | snoozed | expired
      pr_number: Filter by PR number
    """
    stmt = select(Finding)

    if repo_id:
        try:
            stmt = stmt.where(Finding.repository_id == uuid.UUID(repo_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid repo_id")

    if severity:
        try:
            stmt = stmt.where(Finding.severity == Severity(severity.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid severity: {severity}")

    if status:
        try:
            stmt = stmt.where(Finding.status == FindingStatus(status.lower()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if pr_number:
        stmt = stmt.where(Finding.pr_number == pr_number)

    stmt = stmt.order_by(Finding.created_at.desc()).limit(limit).offset(offset)
    results = (await db.execute(stmt)).scalars().all()

    return [
        FindingResponse(
            id=str(f.id),
            repository_id=str(f.repository_id),
            file_path=f.file_path,
            line_start=f.line_start,
            line_end=f.line_end,
            category=f.category,
            severity=f.severity,
            title=f.title,
            description=f.description,
            evidence=f.evidence,
            suggested_fix=f.suggested_fix,
            reasoning=f.reasoning,
            cwe_id=f.cwe_id,
            confidence=f.confidence,
            agent_source=f.agent_source,
            status=f.status,
            pr_number=f.pr_number,
            created_at=f.created_at,
            resolved_at=f.resolved_at,
        )
        for f in results
    ]


@router.get("/{finding_id}", response_model=FindingResponse)
async def get_finding(
    finding_id: str,
    db: AsyncSession = Depends(get_db),
) -> FindingResponse:
    """Get a single finding by ID."""
    try:
        fid = uuid.UUID(finding_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid finding_id")

    result = await db.execute(select(Finding).where(Finding.id == fid))
    finding = result.scalar_one_or_none()
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    return FindingResponse(
        id=str(finding.id),
        repository_id=str(finding.repository_id),
        file_path=finding.file_path,
        line_start=finding.line_start,
        line_end=finding.line_end,
        category=finding.category,
        severity=finding.severity,
        title=finding.title,
        description=finding.description,
        evidence=finding.evidence,
        suggested_fix=finding.suggested_fix,
        reasoning=finding.reasoning,
        cwe_id=finding.cwe_id,
        confidence=finding.confidence,
        agent_source=finding.agent_source,
        status=finding.status,
        pr_number=finding.pr_number,
        created_at=finding.created_at,
        resolved_at=finding.resolved_at,
    )
