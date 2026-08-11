"""SQLAlchemy ORM models — the only layer allowed to describe table structure."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """Microsecond-precision UTC timestamp.

    Client-side (not ``func.now()``) so SQLite test databases get the same
    sub-second precision as Postgres — ``list_recent`` ordering stays
    deterministic instead of tying on SQLite's whole-second CURRENT_TIMESTAMP.
    """

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class GameJob(Base):
    """Persistent record of a single game generation job (§5.5 JobState)."""

    __tablename__ = "game_jobs"

    game_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    prompt: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    dev_iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qa_iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # LLM spend for this job, accumulated by app.utils.usage from the usage
    # objects tool servers return. Kept on the job (not in a metrics backend)
    # because the question it answers — "what did this game cost" — is per-job,
    # and because the retry counters it is read alongside already live here.
    token_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # ConceptGate's proposal (idea + research evidence). Persisted because the
    # user has to read it to approve it, and the graph checkpoint is not
    # reachable from the API layer.
    concept: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    game_design: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    feature_prompts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    repo_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifact: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class PipelineTrace(Base):
    """One generation attempt for one feature, with what became of it.

    The pipeline already produces this chain on every run — feature prompt in,
    C# out, compiler verdict, QA verdict — and until now discarded it. Keeping
    it turns each run into training and retrieval material rather than a
    one-off: rows where ``compile_ok`` and ``qa_verdict`` are both good are
    verified (prompt, code) pairs, and an ``attempt=1`` failure paired with the
    ``attempt=2`` success for the same feature is a before/after example.

    Deliberately separate from ``game_jobs``. A job is one row that is updated
    in place as it advances; a trace is append-only and there are many per job,
    so folding them into the job's JSON columns would make the row grow without
    bound and lose the per-attempt grain that makes the data useful.

    Nothing reads this table yet. It is written now because the data cannot be
    reconstructed later — the sources only exist while the run is in flight.
    """

    __tablename__ = "pipeline_traces"
    __table_args__ = (
        # The two access patterns: "this game's history" and "what failed".
        Index("ix_pipeline_traces_game_attempt", "game_id", "attempt"),
        Index("ix_pipeline_traces_compile_ok", "compile_ok"),
    )

    trace_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(String(36), nullable=False)
    feature_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which round of the dev/QA retry loop produced this. 1 on the first pass.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- inputs -----------------------------------------------------------
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # Blueprint summary shared by every script in this game, stored so a later
    # reader can tell what the model was told, not just what it was asked.
    project_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- output -----------------------------------------------------------
    file: Mapped[str] = mapped_column(String(512), nullable=False)
    generated_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- what happened to it (the labels) ---------------------------------
    # ``None`` while the attempt is still in flight; the run may also end before
    # these are known (escalation, crash), and that absence is itself a fact.
    compile_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    compile_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    qa_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    qa_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
