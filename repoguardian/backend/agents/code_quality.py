"""
Code Quality Agent.

Analyses code for maintainability issues, complexity violations,
code smells, and test coverage gaps introduced by the PR.

Uses a combination of:
  - Heuristic metrics computed directly from the diff/AST
    (cyclomatic complexity estimate, method length, duplication)
  - LLM analysis for semantic code smells and design issues
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
from backend.models.schemas import AgentFinding, ContextPackage, QualityMetrics, QualityReport

logger = logging.getLogger(__name__)
settings = get_settings()
_executor = ThreadPoolExecutor(max_workers=4)


_QUALITY_SYSTEM_PROMPT = """You are a software quality engineer analysing code for maintainability issues.
Focus on meaningful code smells and structural problems, NOT stylistic preferences.

RULES:
1. Only flag issues where you can cite the specific problematic code.
2. Every finding needs evidence (quoted code) and a concrete refactoring suggestion.
3. Do NOT flag standard library usage as a smell. Focus on anti-patterns.
4. Assign LOW or INFO severity to subjective style preferences.

Code smell categories:
  LONG_METHOD:       Function > 40 lines of meaningful logic
  GOD_CLASS:         Class with too many unrelated responsibilities
  DUPLICATE_CODE:    Near-identical blocks (> 5 lines) in multiple places
  DEAD_CODE:         Unreachable code, unused variables/imports
  MAGIC_NUMBER:      Unexplained numeric/string literal
  DEEP_NESTING:      > 4 levels of indentation
  FEATURE_ENVY:      Method excessively uses another class's data
  MISSING_COVERAGE:  New code paths with no tests visible
"""


class CodeQualityAgent(BaseAgent):
    name = "code_quality"

    async def run(self, context: ContextPackage) -> QualityReport:
        self.log_info("Starting code quality analysis for %s PR#%s",
                      context.repo_full_name, context.pr_number)

        # Heuristic metrics from diff/AST
        heuristic_findings = self._run_heuristic_analysis(context)
        metrics = self._compute_metrics(context)

        # LLM semantic analysis
        llm_findings = await self._run_llm_analysis(context)

        all_findings = heuristic_findings + llm_findings
        all_findings = self.apply_confidence_threshold(all_findings)

        delta = self._compute_delta_summary(metrics, all_findings)

        return QualityReport(
            agent_source=self.name,
            delta_summary=delta,
            metrics=metrics,
            findings=all_findings,
            total_token_cost=self.total_token_cost,
        )

    # ── Heuristic analysis ─────────────────────────────────────────────────────

    def _run_heuristic_analysis(self, context: ContextPackage) -> list[AgentFinding]:
        findings = []

        # Check method length
        for sym in context.changed_symbols:
            lines = sym.full_source.count("\n") + 1
            if lines > 40:
                findings.append(AgentFinding(
                    agent_source=self.name,
                    file_path=sym.file_path,
                    line_start=sym.start_line,
                    line_end=sym.end_line,
                    category=FindingCategory.CODE_SMELL,
                    severity=Severity.MEDIUM if lines > 80 else Severity.LOW,
                    title=f"Long method: {sym.name} ({lines} lines)",
                    description=(
                        f"`{sym.name}` in `{sym.file_path}` is {lines} lines long. "
                        f"Methods longer than 40 lines are hard to test and understand."
                    ),
                    evidence=f"def {sym.name}(...):  # {lines} lines",
                    suggested_fix=(
                        f"Break `{sym.name}` into smaller focused functions. "
                        f"Extract logical blocks into well-named helper functions."
                    ),
                    reasoning="Method length exceeds 40-line threshold.",
                    confidence=0.88,
                ))

        # Check deep nesting in added lines
        nesting_findings = self._check_deep_nesting(context)
        findings.extend(nesting_findings)

        # Check for magic numbers
        magic_findings = self._check_magic_numbers(context)
        findings.extend(magic_findings)

        # Check for dead code patterns
        dead_code_findings = self._check_dead_code(context)
        findings.extend(dead_code_findings)

        return findings

    def _check_deep_nesting(self, context: ContextPackage) -> list[AgentFinding]:
        """Detect deeply nested code in added lines."""
        findings = []
        for hunk in context.diff_hunks:
            for line in hunk.added_lines:
                indent = len(line) - len(line.lstrip())
                if indent >= 16:  # 4 levels × 4 spaces
                    findings.append(AgentFinding(
                        agent_source=self.name,
                        file_path=hunk.file_path,
                        line_start=hunk.new_start,
                        category=FindingCategory.CODE_SMELL,
                        severity=Severity.LOW,
                        title="Deep nesting detected",
                        description="Code nested more than 4 levels deep is hard to read and test.",
                        evidence=line[:120],
                        suggested_fix="Extract nested logic into helper functions or use early returns/guard clauses.",
                        reasoning=f"Indentation level {indent // 4} detected.",
                        confidence=0.75,
                    ))
                    break  # one finding per hunk
        return findings

    def _check_magic_numbers(self, context: ContextPackage) -> list[AgentFinding]:
        """Find magic numbers (unexplained numeric literals > 1) in added lines."""
        magic_re = re.compile(r"\b([2-9][0-9]{2,}|[1-9][0-9]+\.[0-9]+)\b")
        exclude_re = re.compile(r"(version|__version__|#|status_code|port|timeout|days|hours|minutes|seconds|limit|max|min|size|len|count)")
        findings = []

        for hunk in context.diff_hunks:
            for line in hunk.added_lines:
                if exclude_re.search(line.lower()):
                    continue
                match = magic_re.search(line)
                if match:
                    findings.append(AgentFinding(
                        agent_source=self.name,
                        file_path=hunk.file_path,
                        line_start=hunk.new_start,
                        category=FindingCategory.CODE_SMELL,
                        severity=Severity.INFO,
                        title=f"Magic number: {match.group(1)}",
                        description=f"The value `{match.group(1)}` appears unexplained. Named constants improve readability.",
                        evidence=line.strip()[:100],
                        suggested_fix=f"Replace `{match.group(1)}` with a named constant, e.g. `MAX_RETRY_COUNT = {match.group(1)}`.",
                        reasoning="Numeric literal without clear context found in added code.",
                        confidence=0.65,
                    ))
                    break  # one per hunk

        return findings

    def _check_dead_code(self, context: ContextPackage) -> list[AgentFinding]:
        """Detect obvious dead code patterns."""
        dead_patterns = [
            (re.compile(r"^\s*(return|raise)\s+.*\n(\s*(?!def|class|#).+)"), "Unreachable code after return/raise"),
            (re.compile(r"pass\s*$"), "Empty function body (pass)"),
        ]
        findings = []

        for hunk in context.diff_hunks:
            full_added = "\n".join(hunk.added_lines)
            for pattern, title in dead_patterns:
                if pattern.search(full_added):
                    findings.append(AgentFinding(
                        agent_source=self.name,
                        file_path=hunk.file_path,
                        line_start=hunk.new_start,
                        category=FindingCategory.CODE_SMELL,
                        severity=Severity.LOW,
                        title=title,
                        description=f"{title} detected in added code.",
                        evidence=full_added[:150],
                        suggested_fix="Remove unreachable code or implement the placeholder.",
                        reasoning="Pattern match for dead code indicator.",
                        confidence=0.70,
                    ))
                    break

        return findings

    # ── Metrics ────────────────────────────────────────────────────────────────

    def _compute_metrics(self, context: ContextPackage) -> QualityMetrics:
        """Compute heuristic quality metrics from the diff."""
        added = sum(len(h.added_lines) for h in context.diff_hunks)
        removed = sum(len(h.removed_lines) for h in context.diff_hunks)

        # Estimate cyclomatic complexity from added lines
        complexity_keywords = re.compile(r"\b(if|elif|else|for|while|except|with|and|or)\b")
        complexity_delta = sum(
            len(complexity_keywords.findall(line))
            for hunk in context.diff_hunks
            for line in hunk.added_lines
        )

        return QualityMetrics(
            cyclomatic_complexity_before=None,
            cyclomatic_complexity_after=None,
            cognitive_complexity_before=None,
            cognitive_complexity_after=None,
            duplication_percentage=None,
            test_coverage_before=None,
            test_coverage_after=None,
        )

    def _compute_delta_summary(self, metrics: QualityMetrics, findings: list[AgentFinding]) -> str:
        severity_counts = {s.value: 0 for s in Severity}
        for f in findings:
            severity_counts[f.severity.value] += 1

        parts = []
        if severity_counts["CRITICAL"] or severity_counts["HIGH"]:
            parts.append(f"{severity_counts['CRITICAL']} critical, {severity_counts['HIGH']} high quality issues found")
        if severity_counts["MEDIUM"]:
            parts.append(f"{severity_counts['MEDIUM']} medium issues")
        if not parts:
            parts.append("No significant quality regressions detected")

        return ". ".join(parts) + "."

    # ── LLM analysis ──────────────────────────────────────────────────────────

    async def _run_llm_analysis(self, context: ContextPackage) -> list[AgentFinding]:
        user_message = self._build_quality_prompt(context)
        loop = asyncio.get_event_loop()
        try:
            result: _QualityLLMOutput = await loop.run_in_executor(
                _executor,
                lambda: self.call_llm(
                    system_prompt=_QUALITY_SYSTEM_PROMPT,
                    user_message=user_message,
                    output_schema=_QualityLLMOutput,
                ),
            )
        except Exception as e:
            self.log_error("Quality LLM analysis failed: %s", e=str(e))
            return []

        return [
            AgentFinding(
                agent_source=self.name,
                file_path=f.file_path,
                line_start=f.line_start,
                category=FindingCategory.CODE_SMELL,
                severity=_parse_severity(f.severity),
                title=f.title,
                description=f.description,
                evidence=f.evidence,
                suggested_fix=f.suggested_fix,
                reasoning=f.reasoning,
                confidence=f.confidence,
            )
            for f in result.findings
            if f.evidence
        ]

    def _build_quality_prompt(self, context: ContextPackage) -> str:
        parts = [
            f"## Repository: {context.repo_full_name} — PR #{context.pr_number}",
            "\n## Diff\n```diff\n" + context.raw_diff + "\n```",
        ]
        if context.changed_symbols:
            sym_text = "\n\n".join(
                f"### {sym.name} ({sym.file_path}:{sym.start_line}-{sym.end_line})\n```\n{sym.full_source}\n```"
                for sym in context.changed_symbols[:8]
            )
            parts.append(f"\n## Changed Symbol Definitions\n{sym_text}")
        parts.append(
            "\nIdentify code smells, maintainability issues, and structural problems in the added/modified code. "
            "Focus on issues that would make future maintenance significantly harder."
        )
        return "\n".join(parts)


# ── LLM schema ─────────────────────────────────────────────────────────────────

class _QualityFindingLLM(BaseModel):
    file_path: Optional[str] = None
    line_start: Optional[int] = None
    severity: str = "LOW"
    title: str
    description: str
    evidence: Optional[str] = None
    suggested_fix: Optional[str] = None
    reasoning: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.75)


class _QualityLLMOutput(BaseModel):
    findings: list[_QualityFindingLLM] = []


def _parse_severity(raw: str) -> Severity:
    return {
        "CRITICAL": Severity.CRITICAL, "HIGH": Severity.HIGH,
        "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW, "INFO": Severity.INFO,
    }.get(raw.upper(), Severity.LOW)
