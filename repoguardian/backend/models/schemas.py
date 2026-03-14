"""
Pydantic schemas used for:
  - FastAPI request/response bodies
  - Agent input/output contracts
  - Internal data transfer objects
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.models.database import (
    EventType,
    FindingCategory,
    FindingStatus,
    HealthGrade,
    HITLAction,
    Platform,
    Severity,
)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          WEBHOOK / EVENT SCHEMAS                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


class WebhookEvent(BaseModel):
    """Normalised event pushed onto the Redis event stream."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType
    platform: Platform
    repo_full_name: str          # "owner/repo"
    repo_clone_url: str
    repo_default_branch: str
    pr_number: int | None = None
    pr_title: str | None = None
    pr_description: str | None = None
    pr_author: str | None = None
    base_sha: str | None = None  # base commit of the PR
    head_sha: str | None = None  # head commit of the PR
    base_branch: str | None = None
    head_branch: str | None = None
    diff_url: str | None = None  # GitHub API URL for the diff
    installation_id: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=datetime.utcnow)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                          CONTEXT RETRIEVAL SCHEMAS                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


class DiffHunk(BaseModel):
    """Single hunk within a unified diff."""
    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]           # '+'/'-'/' ' prefixed lines
    context_lines: list[str]   # only ' ' lines (unchanged context)
    added_lines: list[str]     # '+' lines
    removed_lines: list[str]   # '-' lines


class ChangedSymbol(BaseModel):
    """A function, class, or method modified by the PR."""
    name: str
    kind: Literal["function", "class", "method", "variable"]
    file_path: str
    start_line: int
    end_line: int
    full_source: str     # full definition extracted by AST parser


class CallGraphEdge(BaseModel):
    caller_symbol: str
    caller_file: str
    callee_symbol: str
    callee_file: str


class SimilarChunk(BaseModel):
    """A semantically similar code chunk from the vector store."""
    file_path: str
    start_line: int
    end_line: int
    source: str
    similarity_score: float


class FileContent(BaseModel):
    path: str
    content: str
    token_count: int = 0


class ContextPackage(BaseModel):
    """
    The assembled context sent to every specialist agent.
    Built by ContextRetrievalAgent; immutable after creation.
    """
    repo_id: str
    repo_full_name: str
    event_type: EventType
    pr_number: int | None = None
    pr_title: str | None = None
    pr_description: str | None = None
    pr_author: str | None = None

    # Priority 1 – always present
    raw_diff: str
    diff_hunks: list[DiffHunk]
    changed_files: list[str]

    # Priority 2 – AST-expanded definitions
    changed_symbols: list[ChangedSymbol]
    expanded_definitions: dict[str, str]   # symbol_name → full source

    # Priority 3 – call graph (1 hop)
    call_graph_edges: list[CallGraphEdge]
    callers: dict[str, list[str]]          # symbol → [caller sources]
    callees: dict[str, list[str]]          # symbol → [callee sources]

    # Priority 4 – test files
    relevant_test_files: list[FileContent]

    # Priority 5 – semantic neighbours
    semantic_neighbors: list[SimilarChunk]

    # Priority 6 – manifests & docs
    dependency_manifests: list[FileContent]
    documentation_files: list[FileContent]
    repo_structure: str    # directory tree (depth 2)

    # Budget accounting
    total_tokens_used: int = 0
    budget_remaining: int = 0


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                           AGENT OUTPUT SCHEMAS                             ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


class AgentFinding(BaseModel):
    """
    A single issue found by any specialist agent.
    This is the canonical unit that flows through the system.
    """
    finding_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_source: str            # e.g. "pr_review", "security_scanner"

    # Location
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    # Classification
    category: FindingCategory
    severity: Severity
    title: str
    description: str
    evidence: str | None = None         # quoted problematic code
    suggested_fix: str | None = None
    reasoning: str | None = None        # chain-of-thought explanation

    # References
    cwe_id: str | None = None
    cve_id: str | None = None
    owasp_category: str | None = None
    cvss_score: float | None = None

    # Reliability
    confidence: float = Field(ge=0.0, le=1.0)
    multi_agent_agreement: bool = False


class ReviewVerdict(str):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    NEEDS_DISCUSSION = "NEEDS_DISCUSSION"


class PRReviewResult(BaseModel):
    """Output of PRReviewAgent."""
    agent_source: str = "pr_review"
    summary: str
    overall_verdict: str  # ReviewVerdict
    findings: list[AgentFinding]
    positive_observations: list[str]
    test_coverage_assessment: str
    architectural_concerns: str
    total_token_cost: int = 0


class SecurityReport(BaseModel):
    """Output of SecurityScannerAgent."""
    agent_source: str = "security_scanner"
    risk_level: Severity
    findings: list[AgentFinding]
    secrets_detected: list[AgentFinding]
    supply_chain_risks: list[AgentFinding]
    total_token_cost: int = 0


class QualityMetrics(BaseModel):
    cyclomatic_complexity_before: float | None = None
    cyclomatic_complexity_after: float | None = None
    cognitive_complexity_before: float | None = None
    cognitive_complexity_after: float | None = None
    duplication_percentage: float | None = None
    test_coverage_before: float | None = None
    test_coverage_after: float | None = None


class QualityReport(BaseModel):
    """Output of CodeQualityAgent."""
    agent_source: str = "code_quality"
    delta_summary: str
    metrics: QualityMetrics
    findings: list[AgentFinding]
    total_token_cost: int = 0


class VulnerablePackage(BaseModel):
    name: str
    installed_version: str
    vulnerable_range: str
    cve_id: str
    severity: Severity
    fix_version: str | None = None
    cvss_score: float | None = None


class DependencyReport(BaseModel):
    """Output of DependencyAuditorAgent."""
    agent_source: str = "dependency_auditor"
    vulnerable_packages: list[VulnerablePackage]
    outdated_packages: list[dict[str, Any]]
    license_issues: list[dict[str, Any]]
    findings: list[AgentFinding]
    total_token_cost: int = 0


class DocumentationReport(BaseModel):
    """Output of DocVerifierAgent."""
    agent_source: str = "doc_verifier"
    coverage_score: float      # 0–100
    missing_docstrings: list[str]
    stale_documentation: list[str]
    changelog_gap: str | None
    findings: list[AgentFinding]
    total_token_cost: int = 0


# ── Synthesized Output ─────────────────────────────────────────────────────────


class SynthesizedReport(BaseModel):
    """
    Merged, deduplicated output from all agents.
    Produced by FeedbackSynthesizerAgent.
    """
    repo_full_name: str
    pr_number: int | None
    pr_summary: str          # executive markdown summary
    overall_verdict: str
    health_score_delta: float

    findings: list[AgentFinding]   # deduplicated, priority-sorted

    # Grouped for PR comment rendering
    critical_findings: list[AgentFinding] = []
    high_findings: list[AgentFinding] = []
    medium_findings: list[AgentFinding] = []
    low_findings: list[AgentFinding] = []
    info_findings: list[AgentFinding] = []

    positive_observations: list[str]
    suppressed_count: int = 0  # findings dropped below confidence threshold

    # Formatted output
    pr_comment_markdown: str   # the full PR comment body
    inline_comments: list[dict[str, Any]]  # [{path, line, body}, ...]

    sub_reports: dict[str, Any] = {}  # raw agent outputs keyed by agent_source
    total_token_cost: int = 0


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                         HEALTH DASHBOARD SCHEMAS                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


class SubScores(BaseModel):
    code_quality: float
    security: float
    dependencies: float
    documentation: float
    test_coverage: float


class TrendPoint(BaseModel):
    timestamp: datetime
    overall_score: float
    grade: str


class HotZoneFile(BaseModel):
    file_path: str
    risk_score: float
    finding_count: int
    critical_count: int
    high_count: int


class HealthDashboard(BaseModel):
    """Full payload returned by GET /api/health/{repo_id}."""
    repo_id: str
    repo_full_name: str
    as_of: datetime
    overall_score: float
    grade: HealthGrade

    sub_scores: SubScores

    active_findings: dict[str, int]   # severity → count
    hot_zones: list[HotZoneFile]
    trend_30d: list[TrendPoint]
    trend_delta_7d: float
    trend_velocity: Literal["IMPROVING", "STABLE", "DEGRADING"]

    recent_activity: list[dict[str, Any]]


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                           API REQUEST / RESPONSE                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝


class RepositoryCreate(BaseModel):
    platform: Platform
    owner: str
    name: str
    clone_url: str
    default_branch: str = "main"
    config: dict[str, Any] = Field(default_factory=dict)


class RepositoryResponse(BaseModel):
    id: str
    platform: Platform
    full_name: str
    clone_url: str
    default_branch: str
    primary_language: str | None
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class FindingResponse(BaseModel):
    id: str
    repository_id: str
    file_path: str | None
    line_start: int | None
    line_end: int | None
    category: FindingCategory
    severity: Severity
    title: str
    description: str
    evidence: str | None
    suggested_fix: str | None
    reasoning: str | None
    cwe_id: str | None
    confidence: float
    agent_source: str
    status: FindingStatus
    pr_number: int | None
    created_at: datetime
    resolved_at: datetime | None

    class Config:
        from_attributes = True


class HITLActionRequest(BaseModel):
    action: Literal["approve", "reject", "snooze", "explain"]
    reason_code: str | None = None
    snooze_days: int | None = None
    comment: str | None = None


class HITLActionResponse(BaseModel):
    finding_id: str
    action: str
    actor: str
    timestamp: datetime
    message: str


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
