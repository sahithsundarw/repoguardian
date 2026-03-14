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
    EphemeralFinding,
    EphemeralScanStatus,
    PRReviewResult,
    QualityReport,
    SecurityReport,
    SynthesizedReport,
    WebhookEvent,
)
from backend.services.git_service import GitHubDiffFetcher
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
        # ── Ephemeral path: no DB writes, results stored in Redis ──────────────
        if event.is_ephemeral and event.ephemeral_session_id:
            await self._process_ephemeral(event, event.ephemeral_session_id)
            return

        logger.info(
            "[orchestrator] Processing %s event for %s PR#%s",
            event.event_type.value, event.repo_full_name, event.pr_number,
        )

        # ── Step 0: SHA-based scan cache (skip re-analysis of same commit) ─────
        if event.head_sha:
            cache_key = f"scan_cache:{event.repo_full_name}:{event.head_sha}"
            if await self._state.get(cache_key):
                logger.info(
                    "[orchestrator] Cache hit for %s@%s — skipping re-analysis",
                    event.repo_full_name, event.head_sha,
                )
                return

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
            if event.event_type == EventType.FULL_SCAN:
                context = await asyncio.wait_for(
                    self._build_full_scan_context(event, str(repo_id)),
                    timeout=120.0,
                )
            else:
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
        elif event.event_type == EventType.FULL_SCAN:
            try:
                finding_ids = await self._hitl._persist_findings(report, event, db)
                await db.commit()
                logger.info("[orchestrator] Persisted %d findings for full scan", len(finding_ids))
            except Exception as e:
                logger.error("[orchestrator] Failed to persist full scan findings: %s", e)

        # ── Step 7: Health score update (fire-and-forget) ──────────────────────
        try:
            await self._health.update_health_score(
                repo_id=repo_id,
                report=report,
                event_label=f"{event.event_type.value}:{event.pr_number or 'audit'}",
                head_sha=context.head_sha,
                db=db,
            )
        except Exception as e:
            logger.error("[orchestrator] Health aggregator error: %s", e)

        # ── Cache this SHA so duplicate events are skipped ────────────────────
        if event.head_sha:
            from datetime import timedelta
            await self._state.set(
                f"scan_cache:{event.repo_full_name}:{event.head_sha}",
                {"cached": True},
                ttl=timedelta(hours=1),
            )

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
        is_full_scan = event.event_type in (EventType.FULL_SCAN, EventType.PR_MERGE)
        has_manifests = bool(context.dependency_manifests)

        tasks = [
            run_safe(self._pr_review_agent.run(context), "pr_review") if is_pr_event else asyncio.sleep(0),
            run_safe(self._security_agent.run(context), "security_scanner"),
            run_safe(self._quality_agent.run(context), "code_quality") if (is_pr_event or is_full_scan) else asyncio.sleep(0),
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

    # ── Ephemeral scan ─────────────────────────────────────────────────────────

    async def _process_ephemeral(self, event: WebhookEvent, session_id: str) -> None:
        """
        Run a full analysis for an ephemeral scan request.
        Results are stored in Redis with a 1-hr TTL — no DB writes at all.
        """
        from datetime import datetime, timezone, timedelta

        logger.info("[orchestrator] Starting ephemeral scan for %s (session=%s)",
                    event.repo_full_name, session_id)

        async def _store(status_obj: EphemeralScanStatus) -> None:
            await self._state.set(
                f"ephemeral:{session_id}",
                status_obj.model_dump(mode="json"),
                ttl=timedelta(hours=1),
            )

        created_at = datetime.now(timezone.utc)

        # Mark as running — phase 1
        await _store(EphemeralScanStatus(
            session_id=session_id,
            status="running",
            repo_full_name=event.repo_full_name,
            created_at=created_at,
            progress_percent=5,
            current_step="Connecting to repository",
        ))

        try:
            await _store(EphemeralScanStatus(
                session_id=session_id, status="running",
                repo_full_name=event.repo_full_name, created_at=created_at,
                progress_percent=15, current_step="Fetching repository tree",
            ))

            # Build context using synthetic repo_id = session_id (no DB lookup)
            context = await asyncio.wait_for(
                self._build_full_scan_context(event, session_id),
                timeout=120.0,
            )

            await _store(EphemeralScanStatus(
                session_id=session_id, status="running",
                repo_full_name=event.repo_full_name, created_at=created_at,
                progress_percent=35, current_step="Running agent analysis",
            ))

            # Mark all agents as running before dispatch
            has_manifests = bool(context.dependency_manifests)
            agent_statuses_running = {
                "security_scanner": "running",
                "code_quality": "running",
                "dependency_auditor": "running" if has_manifests else "skipped",
                "doc_verifier": "running",
            }
            await _store(EphemeralScanStatus(
                session_id=session_id, status="running",
                repo_full_name=event.repo_full_name, created_at=created_at,
                progress_percent=40, current_step="Security · Quality · Dependencies",
                agent_statuses=agent_statuses_running,
            ))

            # Run specialist agents
            pr_review, security, quality, dependency, doc = \
                await self._run_agents_parallel(context, event)

            # Mark agents complete
            agent_statuses_done = {
                "security_scanner": "complete" if security else "failed",
                "code_quality": "complete" if quality else "failed",
                "dependency_auditor": "complete" if dependency else ("skipped" if not has_manifests else "failed"),
                "doc_verifier": "complete" if doc else "failed",
            }
            await _store(EphemeralScanStatus(
                session_id=session_id, status="running",
                repo_full_name=event.repo_full_name, created_at=created_at,
                progress_percent=80, current_step="Synthesizing results",
                agent_statuses=agent_statuses_done,
            ))

            # Synthesize
            report = self._synthesizer.synthesize(
                context=context,
                pr_review=pr_review,
                security=security,
                quality=quality,
                dependency=dependency,
                doc=doc,
            )

            await _store(EphemeralScanStatus(
                session_id=session_id, status="running",
                repo_full_name=event.repo_full_name, created_at=created_at,
                progress_percent=92, current_step="Finalizing report",
                agent_statuses=agent_statuses_done,
            ))

            # Project findings to ephemeral format (no DB IDs)
            ephemeral_findings = [
                EphemeralFinding(
                    file_path=f.file_path,
                    line_start=f.line_start,
                    category=f.category.value,
                    severity=f.severity.value,
                    title=f.title,
                    description=f.description,
                    evidence=f.evidence,
                    suggested_fix=f.suggested_fix,
                    confidence=f.confidence,
                    agent_source=f.agent_source,
                )
                for f in report.findings
            ]

            await _store(EphemeralScanStatus(
                session_id=session_id,
                status="complete",
                repo_full_name=event.repo_full_name,
                overall_verdict=report.overall_verdict,
                health_score_delta=report.health_score_delta,
                finding_count=len(report.findings),
                findings=ephemeral_findings,
                pr_summary=report.pr_summary,
                created_at=created_at,
                completed_at=datetime.now(timezone.utc),
                progress_percent=100,
                current_step="Done",
                agent_statuses=agent_statuses_done,
            ))

            logger.info("[orchestrator] Ephemeral scan complete for %s: %d findings",
                        event.repo_full_name, len(report.findings))

        except Exception as e:
            logger.error("[orchestrator] Ephemeral scan failed for %s: %s",
                         event.repo_full_name, e)
            await _store(EphemeralScanStatus(
                session_id=session_id,
                status="failed",
                repo_full_name=event.repo_full_name,
                error=str(e),
                created_at=created_at,
                completed_at=datetime.now(timezone.utc),
                progress_percent=0,
                current_step="Failed",
            ))

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

    async def _build_full_scan_context(
        self, event: WebhookEvent, repo_id: str
    ) -> ContextPackage:
        """
        Build a ContextPackage for a full repo scan (no PR diff).
        Fetches the entire file tree, reads code files, and assembles
        a synthetic context so the agents can analyse the whole codebase.
        """
        from backend.models.schemas import (
            DiffHunk, FileContent,
            ChangedSymbol, CallGraphEdge, SimilarChunk,
        )

        owner, repo_name = event.repo_full_name.split("/", 1)
        ref = event.repo_default_branch or "HEAD"
        fetcher = GitHubDiffFetcher(settings.github_token)

        logger.info("[orchestrator] Full scan: fetching file tree for %s@%s", event.repo_full_name, ref)

        # ── Fetch file tree ────────────────────────────────────────────────────
        CODE_EXTENSIONS = {
            ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go",
            ".rb", ".php", ".c", ".cpp", ".cs", ".rs", ".swift", ".kt",
        }
        SKIP_DIRS = {
            "node_modules", ".git", "dist", "build", "vendor",
            "__pycache__", ".venv", "venv", "env", ".next", "coverage",
        }

        try:
            all_files = await fetcher.get_repo_tree(owner, repo_name, ref)
        except Exception as e:
            logger.error("[orchestrator] Failed to fetch repo tree: %s", e)
            all_files = []

        code_files = [
            f for f in all_files
            if any(f.endswith(ext) for ext in CODE_EXTENSIONS)
            and not any(skip in f.split("/") for skip in SKIP_DIRS)
        ][:40]  # cap at 40 files to stay within token budget

        logger.info("[orchestrator] Full scan: %d code files to scan", len(code_files))

        # ── Fetch file contents ────────────────────────────────────────────────
        raw_diff_parts: list[str] = []
        diff_hunks: list[DiffHunk] = []

        async def fetch_one(path: str) -> None:
            content = await fetcher.fetch_file_content(owner, repo_name, path, ref)
            if not content:
                return
            # Represent the file as a synthetic diff hunk (all lines as additions)
            lines = content.splitlines()
            added = [f"+{ln}" for ln in lines]
            hunk_text = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n" \
                        f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(added)
            raw_diff_parts.append(hunk_text)
            diff_hunks.append(DiffHunk(
                file_path=path,
                old_start=0, old_count=0,
                new_start=1, new_count=len(lines),
                lines=added,
                context_lines=[],
                added_lines=added,
                removed_lines=[],
            ))

        await asyncio.gather(*[fetch_one(f) for f in code_files])

        raw_diff = "\n\n".join(raw_diff_parts)

        # ── Fetch manifests ────────────────────────────────────────────────────
        manifest_names = ["requirements.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "pom.xml"]
        manifests: list[FileContent] = []
        for name in manifest_names:
            content = await fetcher.fetch_file_content(owner, repo_name, name, ref)
            if content:
                manifests.append(FileContent(path=name, content=content))

        # ── Repo structure ─────────────────────────────────────────────────────
        dirs: set[str] = set()
        for fp in all_files:
            parts = fp.split("/")
            if len(parts) >= 1:
                dirs.add(parts[0] + "/")
            if len(parts) >= 2:
                dirs.add(f"  {parts[0]}/{parts[1]}" + ("/" if "." not in parts[1] else ""))
        repo_structure = "\n".join(sorted(dirs)[:60])

        return ContextPackage(
            repo_id=repo_id,
            repo_full_name=event.repo_full_name,
            event_type=EventType.FULL_SCAN,
            pr_number=None,
            head_sha=event.head_sha or "",
            raw_diff=raw_diff,
            diff_hunks=diff_hunks,
            changed_files=code_files,
            changed_symbols=[],
            expanded_definitions={},
            call_graph_edges=[],
            callers={},
            callees={},
            relevant_test_files=[],
            semantic_neighbors=[],
            dependency_manifests=manifests,
            documentation_files=[],
            repo_structure=repo_structure,
            total_tokens_used=len(raw_diff) // 4,
        )
