"""
Health Aggregator Agent.

Computes and persists the Repository Health Score after every analysis cycle.

Health Score (0–100):
  = weighted_average(sub_scores) penalised by active findings

Sub-scores:
  code_quality    × 0.25
  security        × 0.30
  dependencies    × 0.20
  documentation   × 0.15
  test_coverage   × 0.10

Each sub-score starts at 100.0 and is reduced by the penalty of active findings
in that category. The score is capped at [0, 100].
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.models.database import (
    Finding,
    FindingCategory,
    FindingStatus,
    HealthGrade,
    HealthRecord,
    Severity,
)
from backend.models.schemas import (
    HealthDashboard,
    HotZoneFile,
    SubScores,
    SynthesizedReport,
    TrendPoint,
)

logger = logging.getLogger(__name__)
settings = get_settings()


# Category → sub-score mapping
_CATEGORY_TO_SUBSCORE = {
    FindingCategory.BUG:           "code_quality",
    FindingCategory.LOGIC:         "code_quality",
    FindingCategory.PERFORMANCE:   "code_quality",
    FindingCategory.STYLE:         "code_quality",
    FindingCategory.CODE_SMELL:    "code_quality",
    FindingCategory.SECURITY:      "security",
    FindingCategory.DEPENDENCY:    "dependencies",
    FindingCategory.DOCUMENTATION: "documentation",
}


class HealthAggregatorAgent:
    """Computes, persists, and retrieves repository health records."""

    name = "health_aggregator"

    async def update_health_score(
        self,
        repo_id: uuid.UUID,
        report: SynthesizedReport,
        event_label: str,
        db: AsyncSession,
    ) -> HealthRecord:
        """
        Compute a new health score from the synthesized report and persist it.
        This runs ASYNCHRONOUSLY after the HITL gateway posts the review.
        """
        # Compute sub-scores from active findings
        sub_scores = self._compute_sub_scores(report)

        # Weighted composite score
        overall = self._weighted_composite(sub_scores)
        grade = self._score_to_grade(overall)

        # Build finding counts
        critical = len(report.critical_findings)
        high = len(report.high_findings)
        medium = len(report.medium_findings)
        low = len(report.low_findings)
        info = len(report.info_findings)

        record = HealthRecord(
            repository_id=repo_id,
            overall_score=round(overall, 1),
            grade=grade,
            score_code_quality=round(sub_scores["code_quality"], 1),
            score_security=round(sub_scores["security"], 1),
            score_dependencies=round(sub_scores["dependencies"], 1),
            score_documentation=round(sub_scores["documentation"], 1),
            score_test_coverage=round(sub_scores["test_coverage"], 1),
            critical_count=critical,
            high_count=high,
            medium_count=medium,
            low_count=low,
            info_count=info,
            trigger_event=event_label,
            trigger_pr_number=report.pr_number,
            metadata={
                "verdict": report.overall_verdict,
                "token_cost": report.total_token_cost,
                "files_reviewed": len(set(
                    f.file_path for f in report.findings if f.file_path
                )),
            },
        )

        db.add(record)
        await db.commit()
        await db.refresh(record)

        logger.info(
            "[health_aggregator] Health score for repo %s: %.1f (%s)",
            repo_id, overall, grade.value,
        )
        return record

    async def get_dashboard(
        self,
        repo_id: uuid.UUID,
        db: AsyncSession,
    ) -> HealthDashboard | None:
        """Build the full dashboard payload for a repository."""
        from backend.models.database import Repository

        # Fetch repo info
        repo_stmt = select(Repository).where(Repository.id == repo_id)
        repo = (await db.execute(repo_stmt)).scalar_one_or_none()
        if not repo:
            return None

        # Latest health record
        latest_stmt = (
            select(HealthRecord)
            .where(HealthRecord.repository_id == repo_id)
            .order_by(HealthRecord.timestamp.desc())
            .limit(1)
        )
        latest = (await db.execute(latest_stmt)).scalar_one_or_none()

        if not latest:
            return HealthDashboard(
                repo_id=str(repo_id),
                repo_full_name=repo.full_name,
                as_of=datetime.now(timezone.utc),
                overall_score=100.0,
                grade=HealthGrade.A,
                sub_scores=SubScores(
                    code_quality=100, security=100, dependencies=100,
                    documentation=100, test_coverage=100,
                ),
                active_findings={},
                hot_zones=[],
                trend_30d=[],
                trend_delta_7d=0.0,
                trend_velocity="STABLE",
                recent_activity=[],
            )

        # 30-day trend
        trend_stmt = (
            select(HealthRecord)
            .where(HealthRecord.repository_id == repo_id)
            .order_by(HealthRecord.timestamp.desc())
            .limit(30)
        )
        trend_records = (await db.execute(trend_stmt)).scalars().all()
        trend_points = [
            TrendPoint(
                timestamp=r.timestamp,
                overall_score=r.overall_score,
                grade=r.grade.value,
            )
            for r in reversed(trend_records)
        ]

        # 7-day delta
        seven_day_delta = 0.0
        if len(trend_records) >= 7:
            seven_day_delta = round(
                trend_records[0].overall_score - trend_records[6].overall_score, 1
            )

        velocity = (
            "IMPROVING" if seven_day_delta > 2
            else "DEGRADING" if seven_day_delta < -2
            else "STABLE"
        )

        # Active findings
        active_stmt = (
            select(Finding.severity, func.count(Finding.id))
            .where(
                Finding.repository_id == repo_id,
                Finding.status == FindingStatus.OPEN,
                Finding.is_suppressed == False,
            )
            .group_by(Finding.severity)
        )
        active_counts = dict((await db.execute(active_stmt)).all())
        active_findings = {
            sev.value: active_counts.get(sev, 0) for sev in Severity
        }

        # Hot zones (files with most findings)
        hot_zones = await self._compute_hot_zones(repo_id, db)

        # Recent activity (last 10 audit log entries)
        from backend.models.database import AuditLog
        activity_stmt = (
            select(AuditLog)
            .where(AuditLog.repository_id == repo_id)
            .order_by(AuditLog.timestamp.desc())
            .limit(10)
        )
        recent_logs = (await db.execute(activity_stmt)).scalars().all()
        recent_activity = [
            {
                "timestamp": log.timestamp.isoformat(),
                "event": log.event_type,
                "actor": log.actor,
            }
            for log in recent_logs
        ]

        return HealthDashboard(
            repo_id=str(repo_id),
            repo_full_name=repo.full_name,
            as_of=latest.timestamp,
            overall_score=latest.overall_score,
            grade=latest.grade,
            sub_scores=SubScores(
                code_quality=latest.score_code_quality,
                security=latest.score_security,
                dependencies=latest.score_dependencies,
                documentation=latest.score_documentation,
                test_coverage=latest.score_test_coverage,
            ),
            active_findings=active_findings,
            hot_zones=hot_zones,
            trend_30d=trend_points,
            trend_delta_7d=seven_day_delta,
            trend_velocity=velocity,
            recent_activity=recent_activity,
        )

    # ── Score computation ──────────────────────────────────────────────────────

    def _compute_sub_scores(self, report: SynthesizedReport) -> dict[str, float]:
        """
        Compute each sub-score by applying per-category penalties.
        """
        scores = {
            "code_quality": 100.0,
            "security":     100.0,
            "dependencies": 100.0,
            "documentation": 100.0,
            "test_coverage": 100.0,
        }

        penalty_map = {
            Severity.CRITICAL: settings.health_penalty_critical,
            Severity.HIGH:     settings.health_penalty_high,
            Severity.MEDIUM:   settings.health_penalty_medium,
            Severity.LOW:      settings.health_penalty_low,
            Severity.INFO:     settings.health_penalty_info,
        }

        for finding in report.findings:
            sub = _CATEGORY_TO_SUBSCORE.get(finding.category, "code_quality")
            penalty = penalty_map.get(finding.severity, 0)
            scores[sub] = max(0.0, scores[sub] - penalty)

        return scores

    def _weighted_composite(self, sub_scores: dict[str, float]) -> float:
        return (
            sub_scores["code_quality"]   * settings.health_weight_code_quality
            + sub_scores["security"]     * settings.health_weight_security
            + sub_scores["dependencies"] * settings.health_weight_dependencies
            + sub_scores["documentation"]* settings.health_weight_documentation
            + sub_scores["test_coverage"]* settings.health_weight_test_coverage
        )

    def _score_to_grade(self, score: float) -> HealthGrade:
        if score >= 90:
            return HealthGrade.A
        elif score >= 75:
            return HealthGrade.B
        elif score >= 60:
            return HealthGrade.C
        elif score >= 40:
            return HealthGrade.D
        else:
            return HealthGrade.F

    async def _compute_hot_zones(
        self, repo_id: uuid.UUID, db: AsyncSession
    ) -> list[HotZoneFile]:
        """Find the files with the most active findings (risk hot zones)."""
        stmt = (
            select(
                Finding.file_path,
                func.count(Finding.id).label("finding_count"),
                func.sum(
                    func.case(
                        (Finding.severity == Severity.CRITICAL, 10),
                        (Finding.severity == Severity.HIGH, 5),
                        (Finding.severity == Severity.MEDIUM, 2),
                        else_=1,
                    )
                ).label("risk_score"),
                func.sum(
                    func.case((Finding.severity == Severity.CRITICAL, 1), else_=0)
                ).label("critical_count"),
                func.sum(
                    func.case((Finding.severity == Severity.HIGH, 1), else_=0)
                ).label("high_count"),
            )
            .where(
                Finding.repository_id == repo_id,
                Finding.status == FindingStatus.OPEN,
                Finding.file_path.isnot(None),
            )
            .group_by(Finding.file_path)
            .order_by(func.sum(
                func.case(
                    (Finding.severity == Severity.CRITICAL, 10),
                    (Finding.severity == Severity.HIGH, 5),
                    (Finding.severity == Severity.MEDIUM, 2),
                    else_=1,
                )
            ).desc())
            .limit(10)
        )
        rows = (await db.execute(stmt)).all()

        return [
            HotZoneFile(
                file_path=row.file_path or "unknown",
                risk_score=float(row.risk_score or 0),
                finding_count=int(row.finding_count or 0),
                critical_count=int(row.critical_count or 0),
                high_count=int(row.high_count or 0),
            )
            for row in rows
        ]
