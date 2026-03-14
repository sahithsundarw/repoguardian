"""
SQLAlchemy ORM models for RepoGuardian.

Tables:
  repositories     – registered repos and their config
  health_records   – time-series of composite health scores
  findings         – every issue detected by any agent
  hitl_states      – per-finding approval/rejection workflow state
  audit_logs       – immutable append-only event trace
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from backend.config import get_settings

settings = get_settings()

# ── Engine & Session ───────────────────────────────────────────────────────────

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    echo=settings.debug,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db():
    """FastAPI dependency: yields an async DB session."""
    async with AsyncSessionLocal() as session:
        yield session


# ── Base ───────────────────────────────────────────────────────────────────────


class Base(AsyncAttrs, DeclarativeBase):
    pass


# ── Enums ──────────────────────────────────────────────────────────────────────


class Platform(str, enum.Enum):
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"


class EventType(str, enum.Enum):
    PR_OPEN = "pr_open"
    PR_UPDATE = "pr_update"
    PUSH_TO_MAIN = "push_to_main"
    SCHEDULED_AUDIT = "scheduled_audit"
    PR_MERGE = "pr_merge"


class Severity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class FindingCategory(str, enum.Enum):
    BUG = "BUG"
    SECURITY = "SECURITY"
    PERFORMANCE = "PERFORMANCE"
    STYLE = "STYLE"
    LOGIC = "LOGIC"
    DOCUMENTATION = "DOCUMENTATION"
    DEPENDENCY = "DEPENDENCY"
    CODE_SMELL = "CODE_SMELL"


class FindingStatus(str, enum.Enum):
    OPEN = "open"
    APPROVED = "approved"     # Developer accepted the suggestion
    REJECTED = "rejected"     # Developer rejected / false positive
    SNOOZED = "snoozed"
    EXPIRED = "expired"       # HITL timeout passed
    AUTO_RESOLVED = "auto_resolved"  # Resolved on PR merge


class HITLAction(str, enum.Enum):
    POSTED = "posted"
    APPROVED = "approved"
    REJECTED = "rejected"
    SNOOZED = "snoozed"
    EXPIRED = "expired"
    EXPLAINED = "explained"


class HealthGrade(str, enum.Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


# ── Repository ─────────────────────────────────────────────────────────────────


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    platform: Mapped[Platform] = mapped_column(Enum(Platform), nullable=False)
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # full_name = "owner/name"
    full_name: Mapped[str] = mapped_column(String(512), nullable=False)
    clone_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    primary_language: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Stored configuration (HITL rules, token budget overrides, etc.)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("platform", "full_name", name="uq_repo_platform_name"),)

    # Relationships
    health_records: Mapped[list[HealthRecord]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )
    findings: Mapped[list[Finding]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Repository {self.full_name}>"


# ── Health Record ──────────────────────────────────────────────────────────────


class HealthRecord(Base):
    __tablename__ = "health_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    # Composite score
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    grade: Mapped[HealthGrade] = mapped_column(Enum(HealthGrade), nullable=False)

    # Sub-scores (each 0–100)
    score_code_quality: Mapped[float] = mapped_column(Float, default=100.0)
    score_security: Mapped[float] = mapped_column(Float, default=100.0)
    score_dependencies: Mapped[float] = mapped_column(Float, default=100.0)
    score_documentation: Mapped[float] = mapped_column(Float, default=100.0)
    score_test_coverage: Mapped[float] = mapped_column(Float, default=100.0)

    # Finding counts that produced this score
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    medium_count: Mapped[int] = mapped_column(Integer, default=0)
    low_count: Mapped[int] = mapped_column(Integer, default=0)
    info_count: Mapped[int] = mapped_column(Integer, default=0)

    # The PR or audit that triggered this record
    trigger_event: Mapped[str | None] = mapped_column(String(256))
    trigger_pr_number: Mapped[int | None] = mapped_column(Integer)

    # JSON: top risk files, recent improvements
    extra_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    repository: Mapped[Repository] = relationship(back_populates="health_records")


# ── Finding ────────────────────────────────────────────────────────────────────


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )

    # Location
    file_path: Mapped[str | None] = mapped_column(String(1024))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)

    # Classification
    category: Mapped[FindingCategory] = mapped_column(Enum(FindingCategory), nullable=False)
    severity: Mapped[Severity] = mapped_column(Enum(Severity), nullable=False, index=True)

    # Content
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)         # quoted code
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)        # LLM chain of thought

    # CWE / CVE references
    cwe_id: Mapped[str | None] = mapped_column(String(32))
    cve_id: Mapped[str | None] = mapped_column(String(32))
    owasp_category: Mapped[str | None] = mapped_column(String(128))

    # Reliability
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    agent_source: Mapped[str] = mapped_column(String(64), nullable=False)  # which agent found it
    multi_agent_agreement: Mapped[bool] = mapped_column(Boolean, default=False)

    # Lifecycle
    status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus), default=FindingStatus.OPEN, nullable=False, index=True
    )
    pr_number: Mapped[int | None] = mapped_column(Integer, index=True)
    pr_comment_id: Mapped[str | None] = mapped_column(String(64))  # GitHub comment ID

    # Suppression
    is_suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    suppressed_reason: Mapped[str | None] = mapped_column(String(256))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repository: Mapped[Repository] = relationship(back_populates="findings")
    hitl_states: Mapped[list[HITLState]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Finding [{self.severity}] {self.title[:60]}>"


# ── HITL State ─────────────────────────────────────────────────────────────────


class HITLState(Base):
    __tablename__ = "hitl_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[HITLAction] = mapped_column(Enum(HITLAction), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)  # "ai-system" or username
    reason_code: Mapped[str | None] = mapped_column(String(64))      # e.g. "false-positive"
    comment: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    finding: Mapped[Finding] = relationship(back_populates="hitl_states")


# ── Audit Log ──────────────────────────────────────────────────────────────────


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[BigInteger] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    finding_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_name: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)  # structured context

    repository: Mapped[Repository | None] = relationship(back_populates="audit_logs")


# ── DB Initialization ──────────────────────────────────────────────────────────


async def init_db() -> None:
    """Create all tables.  Use Alembic for production migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db() -> None:
    """Drop all tables. Tests only."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
