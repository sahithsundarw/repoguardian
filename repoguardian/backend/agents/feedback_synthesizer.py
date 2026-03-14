"""
Feedback Synthesizer Agent.

Merges outputs from all specialist agents into a single coherent report:
  1. Deduplicates overlapping findings (same file+line from multiple agents)
  2. Boosts confidence for multi-agent agreement
  3. Ranks findings by severity + confidence
  4. Generates the final PR comment markdown
  5. Maps findings to inline PR comments
  6. Computes a health score delta estimate

This agent does NOT call the LLM — it is a pure data transformation step.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.config import get_settings
from backend.models.database import Severity
from backend.models.schemas import (
    AgentFinding,
    ContextPackage,
    DependencyReport,
    DocumentationReport,
    PRReviewResult,
    QualityReport,
    SecurityReport,
    SynthesizedReport,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Severity ordering for sorting
_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}


class FeedbackSynthesizerAgent:
    """
    Merges all agent outputs into a single unified SynthesizedReport.
    Pure data transformation — no LLM calls.
    """

    name = "feedback_synthesizer"

    def synthesize(
        self,
        context: ContextPackage,
        pr_review: PRReviewResult | None = None,
        security: SecurityReport | None = None,
        quality: QualityReport | None = None,
        dependency: DependencyReport | None = None,
        doc: DocumentationReport | None = None,
    ) -> SynthesizedReport:
        """
        Combine all agent outputs into a SynthesizedReport.
        """
        logger.info("[synthesizer] Synthesizing results for %s PR#%s",
                    context.repo_full_name, context.pr_number)

        # Collect all raw findings
        all_findings: list[AgentFinding] = []

        if pr_review:
            all_findings.extend(pr_review.findings)
        if security:
            all_findings.extend(security.findings)
            all_findings.extend(security.secrets_detected)
            all_findings.extend(security.supply_chain_risks)
        if quality:
            all_findings.extend(quality.findings)
        if dependency:
            all_findings.extend(dependency.findings)
        if doc:
            all_findings.extend(doc.findings)

        # Deduplicate
        deduplicated, suppressed_count = self._deduplicate(all_findings)

        # Sort by severity, then confidence
        sorted_findings = sorted(
            deduplicated,
            key=lambda f: (_SEVERITY_ORDER.get(f.severity, 5), -f.confidence),
        )

        # Group by severity
        by_severity: dict[str, list[AgentFinding]] = {
            "CRITICAL": [], "HIGH": [], "MEDIUM": [], "LOW": [], "INFO": [],
        }
        for f in sorted_findings:
            by_severity[f.severity.value].append(f)

        # Determine overall verdict
        verdict = self._determine_verdict(sorted_findings)

        # Compute health score delta (negative = things got worse)
        health_delta = self._compute_health_delta(sorted_findings)

        # Build PR summary and inline comments
        positive_obs = pr_review.positive_observations if pr_review else []
        pr_comment = self._render_pr_comment(
            context, sorted_findings, by_severity, verdict, positive_obs
        )
        inline_comments = self._build_inline_comments(sorted_findings)

        # Total token cost
        total_tokens = sum([
            pr_review.total_token_cost if pr_review else 0,
            security.total_token_cost if security else 0,
            quality.total_token_cost if quality else 0,
            dependency.total_token_cost if dependency else 0,
            doc.total_token_cost if doc else 0,
        ])

        return SynthesizedReport(
            repo_full_name=context.repo_full_name,
            pr_number=context.pr_number,
            pr_summary=self._build_executive_summary(context, sorted_findings, verdict),
            overall_verdict=verdict,
            health_score_delta=health_delta,
            findings=sorted_findings,
            critical_findings=by_severity["CRITICAL"],
            high_findings=by_severity["HIGH"],
            medium_findings=by_severity["MEDIUM"],
            low_findings=by_severity["LOW"],
            info_findings=by_severity["INFO"],
            positive_observations=positive_obs,
            suppressed_count=suppressed_count,
            pr_comment_markdown=pr_comment,
            inline_comments=inline_comments,
            sub_reports={
                "pr_review": pr_review.model_dump() if pr_review else None,
                "security": security.model_dump() if security else None,
                "quality": quality.model_dump() if quality else None,
                "dependency": dependency.model_dump() if dependency else None,
                "doc": doc.model_dump() if doc else None,
            },
            total_token_cost=total_tokens,
        )

    # ── Deduplication ──────────────────────────────────────────────────────────

    def _deduplicate(
        self, findings: list[AgentFinding]
    ) -> tuple[list[AgentFinding], int]:
        """
        Merge findings that overlap on the same file + line range.
        When findings overlap:
          - Keep the one with higher confidence
          - Boost confidence by 0.05 if multi-agent agreement
          - Mark multi_agent_agreement = True
        """
        if not findings:
            return [], 0

        suppressed = 0
        merged: list[AgentFinding] = []
        seen: dict[str, int] = {}  # key → index in merged

        for finding in findings:
            key = self._dedup_key(finding)

            if key in seen:
                existing = merged[seen[key]]
                # Multi-agent agreement detected
                if finding.agent_source != existing.agent_source:
                    # Keep higher confidence one and boost it
                    if finding.confidence > existing.confidence:
                        finding.multi_agent_agreement = True
                        finding.confidence = min(1.0, finding.confidence + 0.05)
                        merged[seen[key]] = finding
                    else:
                        existing.multi_agent_agreement = True
                        existing.confidence = min(1.0, existing.confidence + 0.05)
                suppressed += 1
            else:
                seen[key] = len(merged)
                merged.append(finding)

        logger.info("[synthesizer] %d findings → %d after dedup (%d suppressed)",
                    len(findings), len(merged), suppressed)
        return merged, suppressed

    def _dedup_key(self, f: AgentFinding) -> str:
        """
        Create a deduplication key.
        Findings on the same file within ±3 lines are considered duplicates.
        """
        if f.file_path and f.line_start:
            # Round to nearest 3 to merge nearby-line findings
            bucketed_line = (f.line_start // 3) * 3
            return f"{f.file_path}:{bucketed_line}:{f.category.value}"
        return f"{f.title[:50]}:{f.category.value}"

    # ── Verdict ────────────────────────────────────────────────────────────────

    def _determine_verdict(self, findings: list[AgentFinding]) -> str:
        if any(f.severity == Severity.CRITICAL for f in findings):
            return "REQUEST_CHANGES"
        if sum(1 for f in findings if f.severity == Severity.HIGH) >= 2:
            return "REQUEST_CHANGES"
        if any(f.severity in (Severity.HIGH, Severity.MEDIUM) for f in findings):
            return "NEEDS_DISCUSSION"
        return "APPROVE"

    # ── Health delta ───────────────────────────────────────────────────────────

    def _compute_health_delta(self, findings: list[AgentFinding]) -> float:
        """Estimate the health score impact of these findings (negative = worse)."""
        penalty = sum(
            settings.health_penalty_critical if f.severity == Severity.CRITICAL
            else settings.health_penalty_high if f.severity == Severity.HIGH
            else settings.health_penalty_medium if f.severity == Severity.MEDIUM
            else settings.health_penalty_low if f.severity == Severity.LOW
            else settings.health_penalty_info
            for f in findings
        )
        return -round(penalty, 1)

    # ── PR comment rendering ───────────────────────────────────────────────────

    def _render_pr_comment(
        self,
        context: ContextPackage,
        findings: list[AgentFinding],
        by_severity: dict[str, list[AgentFinding]],
        verdict: str,
        positive_obs: list[str],
    ) -> str:
        """Render the main PR summary comment in GitHub-flavored markdown."""
        lines = []

        # Header
        verdict_emoji = {"APPROVE": "✅", "REQUEST_CHANGES": "🔴", "NEEDS_DISCUSSION": "🟡"}
        lines.append(f"## 🤖 RepoGuardian AI Code Review")
        lines.append(f"")
        lines.append(f"**Repository:** `{context.repo_full_name}` | **PR #{context.pr_number}**")
        lines.append(f"")
        lines.append(f"**Verdict:** {verdict_emoji.get(verdict, '🔵')} {verdict.replace('_', ' ')}")
        lines.append(f"")

        # Finding counts
        crit = len(by_severity["CRITICAL"])
        high = len(by_severity["HIGH"])
        med = len(by_severity["MEDIUM"])
        low = len(by_severity["LOW"])
        info = len(by_severity["INFO"])

        counts = []
        if crit: counts.append(f"🔴 **{crit} Critical**")
        if high: counts.append(f"🟠 **{high} High**")
        if med:  counts.append(f"🟡 {med} Medium")
        if low:  counts.append(f"🟢 {low} Low")
        if info: counts.append(f"ℹ️ {info} Info")

        if counts:
            lines.append("**Findings:** " + "  |  ".join(counts))
        else:
            lines.append("**Findings:** ✨ No significant issues detected")
        lines.append("")

        # Key findings (critical + high only in summary)
        key_findings = by_severity["CRITICAL"] + by_severity["HIGH"]
        if key_findings:
            lines.append("### ⚠️ Key Issues Requiring Attention")
            lines.append("")
            for f in key_findings[:5]:
                severity_badge = f"**[{f.severity.value}]**"
                file_ref = f"`{f.file_path}:{f.line_start}`" if f.file_path else ""
                lines.append(f"- {severity_badge} **{f.title}** {file_ref}")
                lines.append(f"  > {f.description[:200]}")
                lines.append(f"  > 🔧 {f.suggested_fix[:200] if f.suggested_fix else 'See inline comment'}")
                lines.append(f"  > 🔑 Finding ID: `{f.finding_id[:8]}`  |  Confidence: {int(f.confidence * 100)}%")
                lines.append("")

        # Positive observations
        if positive_obs:
            lines.append("### ✅ Positive Observations")
            lines.append("")
            for obs in positive_obs[:3]:
                lines.append(f"- {obs}")
            lines.append("")

        # HITL instructions
        lines.append("---")
        lines.append("### How to Respond to These Findings")
        lines.append("")
        lines.append("Use these commands in a new comment:")
        lines.append("| Command | Action |")
        lines.append("|---------|--------|")
        lines.append("| `/ai-approve <finding-id>` | Accept the suggestion |")
        lines.append("| `/ai-reject <finding-id> false-positive` | Mark as false positive |")
        lines.append("| `/ai-snooze <finding-id> 7d` | Snooze for 7 days |")
        lines.append("| `/ai-explain <finding-id>` | Request deeper explanation |")
        lines.append("")
        lines.append("*All inline comments have a finding ID in the footer.*")
        lines.append("")
        lines.append(
            f"<sub>🤖 Generated by RepoGuardian • "
            f"[View Dashboard](http://localhost:3000/repo/{context.repo_id}) • "
            f"[Full Report](http://localhost:3000/repo/{context.repo_id}/pr/{context.pr_number})</sub>"
        )

        return "\n".join(lines)

    def _build_inline_comments(
        self, findings: list[AgentFinding]
    ) -> list[dict[str, Any]]:
        """Build the list of inline PR comment dicts for findings with file+line."""
        inline = []
        for f in findings:
            if not f.file_path or not f.line_start:
                continue

            severity_emoji = {
                "CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢", "INFO": "ℹ️"
            }.get(f.severity.value, "🔵")

            body_parts = [
                f"{severity_emoji} **[{f.severity.value}] {f.title}**",
                f"",
                f"**Category:** {f.category.value}  |  **Confidence:** {int(f.confidence * 100)}%",
                f"",
                f"**Description:**",
                f"{f.description}",
            ]

            if f.evidence:
                body_parts += ["", "**Evidence:**", f"```\n{f.evidence[:300]}\n```"]

            if f.suggested_fix:
                body_parts += ["", "**Suggested Fix:**", f"```\n{f.suggested_fix[:400]}\n```"]

            if f.reasoning:
                body_parts += ["", f"<details><summary>Reasoning</summary>\n\n{f.reasoning[:500]}\n\n</details>"]

            if f.cwe_id:
                body_parts.append(f"")
                body_parts.append(f"**References:** [{f.cwe_id}](https://cwe.mitre.org/data/definitions/{f.cwe_id.replace('CWE-', '')}.html)")

            body_parts += [
                "",
                "---",
                f"Finding ID: `{f.finding_id[:8]}`  |  Agent: `{f.agent_source}`",
            ]

            inline.append({
                "path": f.file_path,
                "line": f.line_start,
                "body": "\n".join(body_parts),
                "side": "RIGHT",
            })

        return inline

    def _build_executive_summary(
        self,
        context: ContextPackage,
        findings: list[AgentFinding],
        verdict: str,
    ) -> str:
        crit = sum(1 for f in findings if f.severity == Severity.CRITICAL)
        high = sum(1 for f in findings if f.severity == Severity.HIGH)
        if crit or high:
            return (
                f"PR #{context.pr_number} has {crit} critical and {high} high-severity issues. "
                f"Verdict: {verdict}."
            )
        if findings:
            return (
                f"PR #{context.pr_number} has {len(findings)} minor issues. "
                f"Verdict: {verdict}."
            )
        return f"PR #{context.pr_number} looks clean. Verdict: {verdict}."
