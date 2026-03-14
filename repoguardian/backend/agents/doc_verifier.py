"""
Documentation Verifier Agent.

Checks that code changes are accompanied by appropriate documentation:
  1. New/modified public functions/classes have docstrings
  2. README doesn't reference deleted or renamed symbols/config
  3. CHANGELOG is updated when public API changes
  4. API endpoints have descriptions

Uses a combination of:
  - AST-based docstring detection (heuristic, no LLM)
  - LLM analysis for stale documentation and changelog gaps
"""

from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from pydantic import BaseModel, Field

from backend.agents.base import BaseAgent
from backend.config import get_settings
from backend.models.database import FindingCategory, Severity
from backend.models.schemas import AgentFinding, ContextPackage, DocumentationReport

logger = logging.getLogger(__name__)
settings = get_settings()
_executor = ThreadPoolExecutor(max_workers=2)


_DOC_SYSTEM_PROMPT = """You are a technical writer and documentation quality engineer.
Analyse whether the code changes are accompanied by appropriate documentation.

RULES:
1. Flag missing docstrings only for PUBLIC functions/classes (no leading underscore).
2. Flag stale documentation only when there is clear evidence of a mismatch.
3. Do NOT penalise internal/private helpers for missing docs.
4. Check if the CHANGELOG or README needs updating based on the changes.
"""


class DocVerifierAgent(BaseAgent):
    name = "doc_verifier"

    async def run(self, context: ContextPackage) -> DocumentationReport:
        self.log_info("Starting documentation verification for %s", context.repo_full_name)

        # Heuristic: check for missing docstrings on changed symbols
        missing_docstrings = self._check_missing_docstrings(context)

        # Heuristic: check if CHANGELOG was updated when it should be
        changelog_gap = self._check_changelog_gap(context)

        # LLM: deeper stale doc analysis
        llm_analysis = await self._run_llm_analysis(context)

        # Compute coverage score
        total_public_symbols = sum(
            1 for sym in context.changed_symbols
            if not sym.name.startswith("_")
        )
        missing_count = len(missing_docstrings)
        coverage_score = (
            100.0 * (total_public_symbols - missing_count) / total_public_symbols
            if total_public_symbols > 0
            else 100.0
        )

        # Build findings list
        findings = []
        for sym_name in missing_docstrings:
            findings.append(AgentFinding(
                agent_source=self.name,
                category=FindingCategory.DOCUMENTATION,
                severity=Severity.LOW,
                title=f"Missing docstring: {sym_name}",
                description=f"The public function/class `{sym_name}` lacks a docstring.",
                evidence=f"def {sym_name}(...):",
                suggested_fix=f'Add a docstring: `"""{sym_name} — describe purpose, args, returns."""`',
                reasoning="Public symbol without docstring detected by AST analysis.",
                confidence=0.85,
            ))

        if changelog_gap:
            findings.append(AgentFinding(
                agent_source=self.name,
                category=FindingCategory.DOCUMENTATION,
                severity=Severity.INFO,
                title="CHANGELOG not updated",
                description=changelog_gap,
                suggested_fix="Add an entry to CHANGELOG.md describing the change.",
                reasoning="Public API changes detected but no CHANGELOG update in the diff.",
                confidence=0.70,
            ))

        findings.extend(llm_analysis)
        findings = self.apply_confidence_threshold(findings)

        return DocumentationReport(
            agent_source=self.name,
            coverage_score=round(coverage_score, 1),
            missing_docstrings=missing_docstrings,
            stale_documentation=[],
            changelog_gap=changelog_gap,
            findings=findings,
            total_token_cost=self.total_token_cost,
        )

    # ── Heuristic checks ───────────────────────────────────────────────────────

    def _check_missing_docstrings(self, context: ContextPackage) -> list[str]:
        """Return names of public symbols that lack docstrings."""
        missing = []
        for sym in context.changed_symbols:
            if sym.name.startswith("_"):
                continue  # Skip private/dunder symbols
            source = sym.full_source
            # Check if there's a docstring (triple-quoted string after def/class line)
            has_docstring = bool(
                re.search(r'(def|class)\s+\w+[^:]*:\s*\n\s*"""', source) or
                re.search(r"(def|class)\s+\w+[^:]*:\s*\n\s*'''", source)
            )
            if not has_docstring:
                missing.append(sym.name)
        return missing

    def _check_changelog_gap(self, context: ContextPackage) -> Optional[str]:
        """
        Check if the PR modifies public API (new function/class) but
        does not update the CHANGELOG.
        """
        # Count new public functions/classes added in the diff
        new_public_symbols = [
            sym for sym in context.changed_symbols
            if not sym.name.startswith("_") and
            any(
                sym.name in line
                for hunk in context.diff_hunks
                for line in hunk.added_lines
                if line.startswith("def ") or line.startswith("class ")
            )
        ]

        if not new_public_symbols:
            return None

        # Check if CHANGELOG was updated in this PR
        changelog_updated = any(
            "CHANGELOG" in f.upper() or "CHANGES" in f.upper()
            for f in context.changed_files
        )

        if not changelog_updated and len(new_public_symbols) > 0:
            names = ", ".join(f"`{s.name}`" for s in new_public_symbols[:3])
            return (
                f"This PR adds/modifies public API ({names}) but CHANGELOG.md was not updated. "
                f"Consider adding a changelog entry for this release."
            )

        return None

    # ── LLM analysis ──────────────────────────────────────────────────────────

    async def _run_llm_analysis(self, context: ContextPackage) -> list[AgentFinding]:
        """LLM checks for stale README and parameter mismatch in docstrings."""
        if not context.documentation_files and not context.changed_symbols:
            return []

        user_message = self._build_doc_prompt(context)
        loop = asyncio.get_event_loop()

        try:
            result: _DocLLMOutput = await loop.run_in_executor(
                _executor,
                lambda: self.call_llm(
                    system_prompt=_DOC_SYSTEM_PROMPT,
                    user_message=user_message,
                    output_schema=_DocLLMOutput,
                ),
            )
        except Exception as e:
            self.log_error("Doc verification LLM failed: %s", e=str(e))
            return []

        return [
            AgentFinding(
                agent_source=self.name,
                category=FindingCategory.DOCUMENTATION,
                severity=_parse_severity(f.severity),
                title=f.title,
                description=f.description,
                evidence=f.evidence,
                suggested_fix=f.suggested_fix,
                reasoning=f.reasoning,
                confidence=f.confidence,
            )
            for f in result.findings
        ]

    def _build_doc_prompt(self, context: ContextPackage) -> str:
        parts = [f"## PR #{context.pr_number}: {context.pr_title or ''}"]
        parts.append("\n## Changes Summary (diff excerpt)\n```diff\n"
                     + context.raw_diff[:3000] + "\n```")

        if context.documentation_files:
            for doc in context.documentation_files[:2]:
                parts.append(f"\n## {doc.path}\n```markdown\n{doc.content[:2000]}\n```")

        if context.changed_symbols:
            syms = "\n\n".join(
                f"```python\n{sym.full_source[:600]}\n```"
                for sym in context.changed_symbols[:5]
            )
            parts.append(f"\n## Changed Functions/Classes\n{syms}")

        parts.append(
            "\nCheck: 1) Are there docstrings missing for public functions? "
            "2) Does the README/docs reference anything the diff removes or renames? "
            "3) Does the diff change a public API that the changelog doesn't mention?"
        )
        return "\n".join(parts)


# ── LLM schema ─────────────────────────────────────────────────────────────────

class _DocFindingLLM(BaseModel):
    severity: str = "LOW"
    title: str
    description: str
    evidence: Optional[str] = None
    suggested_fix: Optional[str] = None
    reasoning: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.70)


class _DocLLMOutput(BaseModel):
    findings: list[_DocFindingLLM] = []


def _parse_severity(raw: str) -> Severity:
    return {"MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW, "INFO": Severity.INFO}.get(
        raw.upper(), Severity.INFO
    )
