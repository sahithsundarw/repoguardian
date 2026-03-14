"""
Orchestrator Agent.

The central coordinator of the RepoGuardian system.

Workflow for PR events:
  1. Validate the event (rate limits, supported platform, active repo)
  2. Build the Context Package (ContextRetrievalAgent — sequential)
  3. Dispatch all specialist agents in PARALLEL
  4. Collect results (with timeout)
  5. Synthesize results (FeedbackSynthesizerAgent — sequential)
  6. Post to GitHub via HITL Gateway
  7. Update health score asynchronously

For scheduled audits: same flow but without a PR context.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.code_quality import CodeQualityAgent
from backend.agents.context_retrieval import ContextRetrievalAgent
from backend.agents.dependency_auditor import DependencyAuditorAgent
from backend.agents.doc_verifier import DocVerifierAgent
from backend.agents.feedback_synthesizer import FeedbackSynthesizerAgent
from backend.agents.health_aggregator import HealthAggregatorAgent
from backend.agents.hitl_gateway import HITLGatewayAgent
from backend.agents.pr_review import PRReviewAgent
from backend.agents.security_scanner import SecurityScannerAgent
from backend.config import get_settings
from backend.models.database import EventType
from backend.models.schemas import (
    ContextPackage,
    DependencyReport,
    DocumentationReport,
    PRReviewResult,
    QualityReport,
    SecurityReport,
    SynthesizedReport,
    WebhookEvent,
)
from backend.services.github_service import GitHubAPIClient
from backend.services.redis_service import StateStore

logger = logging.getLogger(__name__)
settings = get_settings()


class Orchestrator:
    """
    Top-level coordinator that drives the full analysis pipeline.
    One instance per worker process (stateless between events).
    """

    def __init__(
        self,
        github_client: GitHubAPIClient,
        state_store: StateStore,
    ) -> None:
        # Agents
        self._context_agent = ContextRetrievalAgent()
        self._pr_review_agent = PRReviewAgent()
        self._security_agent = SecurityScannerAgent()
        self._quality_agent = CodeQualityAgent()
        self._dep_agent = DependencyAuditorAgent()
        self._doc_agent = DocVerifierAgent()
        self._synthesizer = FeedbackSynthesizerAgent()
        self._hitl = HITLGatewayAgent(github_client, state_store)
        self._health = HealthAggregatorAgent()

        self._state = state_store

    async def process_event(self, event: WebhookEvent, db: AsyncSession) -> None:
        """
        Main entry point: process one webhook event end-to-end.
        This is called by the background worker for each event.
        """
        logger.info(
            "[orchestrator] Processing %s event for %s PR#%s",
            event.event_type.value, event.repo_full_name, event.pr_number,
        )

        # ── Step 1: Rate limit check ───────────────────────────────────────────
        if not await self._state.check_rate_limit(event.repo_full_name):
            logger.warning(
                "[orchestrator] Rate limit exceeded for %s — skipping",
                event.repo_full_name,
            )
            return

        # ── Step 2: Resolve repo_id from DB ───────────────────────────────────
        repo_id = await self._resolve_repo_id(event.repo_full_name, db)
        if not repo_id:
            logger.error("[orchestrator] Repo %s not registered — skipping", event.repo_full_name)
            return

        # ── Step 3: Context assembly (sequential, blocks all further steps) ───
        try:
            context = await asyncio.wait_for(
                self._context_agent.run(event, str(repo_id)),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error("[orchestrator] Context assembly timed out for %s", event.repo_full_name)
            return
        except Exception as e:
            logger.error("[orchestrator] Context assembly failed: %s", e)
            return

        # ── Step 4: Parallel specialist agent dispatch ─────────────────────────
        pr_review, security, quality, dependency, doc = await self._run_agents_parallel(
            context, event
        )

        # ── Step 5: Synthesis ──────────────────────────────────────────────────
        report = self._synthesizer.synthesize(
            context=context,
            pr_review=pr_review,
            security=security,
            quality=quality,
            dependency=dependency,
            doc=doc,
        )

        logger.info(
            "[orchestrator] Synthesis complete: %d findings, verdict=%s",
            len(report.findings), report.overall_verdict,
        )

        # ── Step 6: HITL Gateway (post to GitHub, persist to DB) ──────────────
        if event.pr_number and event.event_type in (EventType.PR_OPEN, EventType.PR_UPDATE):
            try:
                await self._hitl.post_review(event, report, db)
            except Exception as e:
                logger.error("[orchestrator] HITL gateway error: %s", e)

        # ── Step 7: Health score update (fire-and-forget) ──────────────────────
        try:
            await self._health.update_health_score(
                repo_id=repo_id,
                report=report,
                event_label=f"{event.event_type.value}:{event.pr_number or 'audit'}",
                db=db,
            )
        except Exception as e:
            logger.error("[orchestrator] Health aggregator error: %s", e)

        logger.info(
            "[orchestrator] Pipeline complete for %s PR#%s",
            event.repo_full_name, event.pr_number,
        )

    # ── Parallel agent dispatch ────────────────────────────────────────────────

    async def _run_agents_parallel(
        self,
        context: ContextPackage,
        event: WebhookEvent,
    ) -> tuple[
        PRReviewResult | None,
        SecurityReport | None,
        QualityReport | None,
        DependencyReport | None,
        DocumentationReport | None,
    ]:
        """
        Run all specialist agents concurrently.
        Each agent has an individual timeout; failures are isolated.
        The pipeline continues with partial results if any agent fails.
        """
        timeout = settings.agent_timeout_seconds

        async def run_safe(coro, agent_name: str):
            """Run a coroutine with timeout and error isolation."""
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("[orchestrator] Agent '%s' timed out after %ds", agent_name, timeout)
                return None
            except Exception as e:
                logger.error("[orchestrator] Agent '%s' failed: %s", agent_name, e)
                return None

        # Determine which agents to run based on event type
        is_pr_event = event.event_type in (EventType.PR_OPEN, EventType.PR_UPDATE)
        has_manifests = bool(context.dependency_manifests)

        tasks = [
            run_safe(self._pr_review_agent.run(context), "pr_review") if is_pr_event else asyncio.sleep(0),
            run_safe(self._security_agent.run(context), "security_scanner"),
            run_safe(self._quality_agent.run(context), "code_quality") if is_pr_event else asyncio.sleep(0),
            run_safe(self._dep_agent.run(context), "dependency_auditor") if has_manifests else asyncio.sleep(0),
            run_safe(self._doc_agent.run(context), "doc_verifier"),
        ]

        results = await asyncio.gather(*tasks)

        pr_review  = results[0] if isinstance(results[0], PRReviewResult) else None
        security   = results[1] if isinstance(results[1], SecurityReport) else None
        quality    = results[2] if isinstance(results[2], QualityReport) else None
        dependency = results[3] if isinstance(results[3], DependencyReport) else None
        doc        = results[4] if isinstance(results[4], DocumentationReport) else None

        return pr_review, security, quality, dependency, doc

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _resolve_repo_id(
        self, full_name: str, db: AsyncSession
    ) -> uuid.UUID | None:
        from sqlalchemy import select
        from backend.models.database import Repository

        stmt = select(Repository.id).where(Repository.full_name == full_name)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        return row
