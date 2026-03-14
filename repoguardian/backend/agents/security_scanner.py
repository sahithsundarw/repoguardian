"""
Security Scanner Agent.

Specialised agent for detecting security vulnerabilities in PR diffs.
Covers OWASP Top 10, CWE classifications, secrets detection, and
supply chain risks.

Uses a two-pass approach:
  Pass 1: Fast deterministic pattern matching (regex-based, no LLM)
  Pass 2: Deep semantic analysis via Claude for complex vulnerabilities

This ensures we catch common, unambiguous issues (hardcoded secrets, raw
SQL strings) with near-zero false positives, while using the LLM only
for nuanced issues that require understanding code flow.
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
from backend.models.schemas import AgentFinding, ContextPackage, SecurityReport

logger = logging.getLogger(__name__)
settings = get_settings()
_executor = ThreadPoolExecutor(max_workers=4)


# ── Deterministic patterns (Pass 1) ───────────────────────────────────────────

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|secret|api_key|apikey|token|auth_token)\s*=\s*['\"][^'\"]{4,}['\"]"), "Hardcoded credential", "CWE-798"),
    (re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"][^'\"]{20,}['\"]"), "AWS secret key exposure", "CWE-798"),
    (re.compile(r"(?i)private_key\s*=\s*['\"][^'\"]{10,}['\"]"), "Private key in source code", "CWE-321"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key ID", "CWE-798"),
    (re.compile(r"(?i)-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----"), "Private key block", "CWE-321"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36}"), "GitHub personal access token", "CWE-798"),
]

_INJECTION_PATTERNS = [
    (re.compile(r"(?i)(execute|query|cursor\.execute)\s*\(\s*f?['\"].*%[s|d].*['\"]|f?['\"].*\{.*\}.*['\"]"), "Potential SQL injection", "CWE-89"),
    (re.compile(r"(?i)subprocess\.(run|call|Popen)\s*\(.*shell\s*=\s*True"), "Shell injection via shell=True", "CWE-78"),
    (re.compile(r"(?i)eval\s*\(.*input|eval\s*\(.*request|eval\s*\(.*params"), "Unsafe eval() with user input", "CWE-94"),
    (re.compile(r"(?i)os\.system\s*\(.*request|os\.system\s*\(.*input|os\.system\s*\(.*param"), "Command injection via os.system", "CWE-78"),
    (re.compile(r"(?i)pickle\.loads?\s*\(.*request|pickle\.loads?\s*\(.*data"), "Unsafe deserialization (pickle)", "CWE-502"),
]

_CRYPTO_PATTERNS = [
    (re.compile(r"(?i)hashlib\.(md5|sha1)\s*\("), "Weak hash algorithm for security context", "CWE-327"),
    (re.compile(r"(?i)DES\.|ECB|AES\.new\([^,]+,\s*AES\.MODE_ECB"), "Weak/insecure encryption mode", "CWE-327"),
    (re.compile(r"(?i)random\.(random|randint|choice)\s*\(.*(?:token|secret|key|nonce|salt)"), "Cryptographically weak random for security value", "CWE-338"),
]

_PATH_PATTERNS = [
    (re.compile(r"(?i)open\s*\(\s*.*request|open\s*\(\s*.*input|open\s*\(\s*.*param"), "Potential path traversal (unvalidated file open)", "CWE-22"),
]

_ALL_PATTERNS = _SECRET_PATTERNS + _INJECTION_PATTERNS + _CRYPTO_PATTERNS + _PATH_PATTERNS


# ── System prompt ──────────────────────────────────────────────────────────────

_SECURITY_SYSTEM_PROMPT = """You are a security engineer specialising in application security (AppSec).
Analyse the provided code diff for security vulnerabilities.

CRITICAL RULES:
1. Only report vulnerabilities where you can quote the EXACT vulnerable code.
2. For each finding, provide CWE ID, OWASP category, and a concrete remediation.
3. Do NOT report theoretical or highly speculative vulnerabilities without evidence.
4. Security confidence: be conservative — use 0.9+ only for unambiguous vulnerabilities.
5. Focus on: injection flaws, auth/session issues, sensitive data exposure,
   insecure deserialization, broken access control, cryptographic weaknesses.

Rate severity as:
  CRITICAL: Direct exploitation possible, high impact (RCE, auth bypass, major data breach)
  HIGH:     Significant risk, exploitable with some effort
  MEDIUM:   Potential risk requiring specific conditions
  LOW:      Defensive depth, informational security hardening
"""


class SecurityScannerAgent(BaseAgent):
    """
    Two-pass security scanner:
    1. Fast regex pattern matching for known-bad patterns (zero LLM cost)
    2. Deep LLM semantic analysis for complex vulnerabilities
    """

    name = "security_scanner"

    async def run(self, context: ContextPackage) -> SecurityReport:
        self.log_info("Starting security scan for %s PR#%s",
                      context.repo_full_name, context.pr_number)

        # Pass 1: Deterministic pattern matching
        pattern_findings = self._run_pattern_scan(context)
        self.log_info("Pattern scan found %d issues", len(pattern_findings))

        # Pass 2: LLM semantic analysis
        llm_findings = await self._run_llm_scan(context)
        self.log_info("LLM scan found %d issues", len(llm_findings))

        # Combine, deduplicate, filter
        all_findings = pattern_findings + llm_findings
        all_findings = self.apply_confidence_threshold(all_findings)

        # Separate secrets from general findings
        secrets = [f for f in all_findings if "secret" in f.title.lower() or
                   "credential" in f.title.lower() or "key" in f.title.lower() or
                   f.cwe_id in ("CWE-798", "CWE-321")]
        security_findings = [f for f in all_findings if f not in secrets]

        # Determine overall risk level
        if any(f.severity == Severity.CRITICAL for f in all_findings):
            risk_level = Severity.CRITICAL
        elif any(f.severity == Severity.HIGH for f in all_findings):
            risk_level = Severity.HIGH
        elif any(f.severity == Severity.MEDIUM for f in all_findings):
            risk_level = Severity.MEDIUM
        elif all_findings:
            risk_level = Severity.LOW
        else:
            risk_level = Severity.INFO

        return SecurityReport(
            agent_source=self.name,
            risk_level=risk_level,
            findings=security_findings,
            secrets_detected=secrets,
            supply_chain_risks=[],  # Handled by DependencyAuditorAgent
            total_token_cost=self.total_token_cost,
        )

    # ── Pass 1: Pattern matching ───────────────────────────────────────────────

    def _run_pattern_scan(self, context: ContextPackage) -> list[AgentFinding]:
        """
        Scan the raw diff for known-bad patterns using regex.
        Very high confidence (0.92) since these are deterministic matches.
        """
        findings: list[AgentFinding] = []
        added_lines = self._extract_added_lines(context.raw_diff)

        for line_num, line in added_lines:
            for pattern, title, cwe_id in _ALL_PATTERNS:
                match = pattern.search(line)
                if match:
                    # Determine which file this line belongs to
                    file_path = self._find_file_for_line(context, line_num)
                    owasp = _CWE_TO_OWASP.get(cwe_id, "")

                    findings.append(AgentFinding(
                        agent_source=self.name,
                        file_path=file_path,
                        line_start=line_num,
                        line_end=line_num,
                        category=FindingCategory.SECURITY,
                        severity=_CWE_TO_SEVERITY.get(cwe_id, Severity.HIGH),
                        title=title,
                        description=f"Detected pattern matching {cwe_id}: {title}",
                        evidence=line.strip(),
                        suggested_fix=_CWE_TO_FIX.get(cwe_id, "See OWASP remediation guidance."),
                        reasoning=f"Regex pattern match for {cwe_id} on added line.",
                        cwe_id=cwe_id,
                        owasp_category=owasp,
                        confidence=0.92,
                    ))
                    break  # one finding per line

        return findings

    # ── Pass 2: LLM semantic analysis ─────────────────────────────────────────

    async def _run_llm_scan(self, context: ContextPackage) -> list[AgentFinding]:
        """Deep LLM-based security analysis for complex vulnerabilities."""
        user_message = self._build_security_prompt(context)

        loop = asyncio.get_event_loop()
        try:
            result: _SecurityLLMOutput = await loop.run_in_executor(
                _executor,
                lambda: self.call_llm(
                    system_prompt=_SECURITY_SYSTEM_PROMPT,
                    user_message=user_message,
                    output_schema=_SecurityLLMOutput,
                ),
            )
        except Exception as e:
            self.log_error("Security LLM scan failed: %s", e=str(e))
            return []

        findings = []
        for f in result.findings:
            findings.append(AgentFinding(
                agent_source=self.name,
                file_path=f.file_path,
                line_start=f.line_start,
                line_end=f.line_end,
                category=FindingCategory.SECURITY,
                severity=_parse_severity(f.severity),
                title=f.title,
                description=f.description,
                evidence=f.evidence,
                suggested_fix=f.suggested_fix,
                reasoning=f.reasoning,
                cwe_id=f.cwe_id,
                owasp_category=f.owasp_category,
                cvss_score=f.cvss_score,
                confidence=f.confidence,
            ))

        # Filter: require evidence for LLM findings
        return [f for f in findings if f.evidence and len(f.evidence.strip()) > 5]

    def _build_security_prompt(self, context: ContextPackage) -> str:
        parts = [
            f"## Repository: {context.repo_full_name}",
            f"## PR #{context.pr_number}: {context.pr_title or 'Untitled'}",
            "\n## Diff to Analyse\n```diff\n" + context.raw_diff + "\n```",
        ]
        if context.expanded_definitions:
            defs = "\n\n".join(
                f"```\n{src}\n```" for src in list(context.expanded_definitions.values())[:5]
            )
            parts.append(f"\n## Full Function Definitions\n{defs}")
        parts.append(
            "\nAnalyse the above diff for security vulnerabilities. "
            "Focus especially on data flow from user inputs to sinks (SQL, command, file, HTML)."
        )
        return "\n".join(parts)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _extract_added_lines(self, diff: str) -> list[tuple[int, str]]:
        """Return (line_number, line_content) tuples for all added lines in the diff."""
        result = []
        current_new_line = 0
        hunk_header = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

        for line in diff.splitlines():
            m = hunk_header.match(line)
            if m:
                current_new_line = int(m.group(1)) - 1
                continue
            if line.startswith("+") and not line.startswith("+++"):
                current_new_line += 1
                result.append((current_new_line, line[1:]))
            elif line.startswith(" "):
                current_new_line += 1
            # '-' lines don't increment new line counter

        return result

    def _find_file_for_line(self, context: ContextPackage, line_num: int) -> Optional[str]:
        """Find which file a given line number corresponds to in the diff."""
        for hunk in context.diff_hunks:
            if hunk.new_start <= line_num <= hunk.new_start + hunk.new_count:
                return hunk.file_path
        return context.changed_files[0] if context.changed_files else None


# ── LLM output schema ──────────────────────────────────────────────────────────

class _SecurityFindingLLM(BaseModel):
    file_path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    severity: str
    title: str
    description: str
    evidence: Optional[str] = None
    suggested_fix: Optional[str] = None
    reasoning: Optional[str] = None
    cwe_id: Optional[str] = None
    owasp_category: Optional[str] = None
    cvss_score: Optional[float] = None
    confidence: float = Field(ge=0.0, le=1.0)


class _SecurityLLMOutput(BaseModel):
    findings: list[_SecurityFindingLLM] = []


# ── Lookup tables ──────────────────────────────────────────────────────────────

_CWE_TO_SEVERITY: dict[str, Severity] = {
    "CWE-798": Severity.CRITICAL, "CWE-321": Severity.CRITICAL,
    "CWE-89":  Severity.HIGH,     "CWE-78":  Severity.CRITICAL,
    "CWE-94":  Severity.HIGH,     "CWE-502": Severity.HIGH,
    "CWE-327": Severity.MEDIUM,   "CWE-338": Severity.MEDIUM,
    "CWE-22":  Severity.HIGH,
}

_CWE_TO_OWASP: dict[str, str] = {
    "CWE-89":  "A03:2021 - Injection",
    "CWE-78":  "A03:2021 - Injection",
    "CWE-94":  "A03:2021 - Injection",
    "CWE-502": "A08:2021 - Software and Data Integrity Failures",
    "CWE-798": "A07:2021 - Identification and Authentication Failures",
    "CWE-321": "A02:2021 - Cryptographic Failures",
    "CWE-327": "A02:2021 - Cryptographic Failures",
    "CWE-338": "A02:2021 - Cryptographic Failures",
    "CWE-22":  "A01:2021 - Broken Access Control",
}

_CWE_TO_FIX: dict[str, str] = {
    "CWE-89":  "Use parameterised queries / prepared statements. Never interpolate user input into SQL.",
    "CWE-78":  "Avoid shell=True. Pass arguments as a list. Validate and sanitise all user-controlled input.",
    "CWE-94":  "Never call eval() on user-controlled input. Use ast.literal_eval() for safe expression parsing.",
    "CWE-502": "Never unpickle untrusted data. Use JSON or msgpack with schema validation instead.",
    "CWE-798": "Remove the credential from source. Use environment variables or a secrets manager (Vault, AWS SSM).",
    "CWE-321": "Remove private keys from source code. Load from secure storage at runtime.",
    "CWE-327": "Use SHA-256 or stronger for hashing. Use AES-GCM for encryption.",
    "CWE-338": "Use secrets.token_hex() or os.urandom() for cryptographic randomness.",
    "CWE-22":  "Validate and normalise file paths. Use os.path.realpath() and verify the resolved path is within the allowed directory.",
}


def _parse_severity(raw: str) -> Severity:
    return {
        "CRITICAL": Severity.CRITICAL, "HIGH": Severity.HIGH,
        "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW, "INFO": Severity.INFO,
    }.get(raw.upper(), Severity.HIGH)
