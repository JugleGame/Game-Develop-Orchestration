"""Pydantic data contracts exchanged between DeveloperAI and MCP agents.

Field names and shapes follow ``02_시스템_설계명세서`` §5 and
``03_MCP_Interface_Specification``.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TargetPlatform(StrEnum):
    PC = "PC"
    MOBILE = "Mobile"
    WEBGL = "WebGL"


class FeaturePriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class ErrorType(StrEnum):
    COMPILE = "compile"
    RUNTIME = "runtime"
    LOGIC = "logic"
    STRUCTURE_MISMATCH = "structure_mismatch"


class JobStatus(StrEnum):
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    DEVELOPING = "developing"
    QA_REVIEW = "qa_review"
    DEPLOYING = "deploying"
    DONE = "done"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class GameDesignDocument(BaseModel):
    """§5.1 — produced by StrategicMcpServer, consumed by Orchestrator/QA."""

    game_id: str
    genre: str
    core_mechanics: list[str]
    art_style: str
    target_platform: TargetPlatform
    structure_overview: str
    created_at: datetime


class FeatureImplementationPrompt(BaseModel):
    """§5.2 — produced by StrategicMcpServer, consumed by Developer AI.

    ``assets_needed`` is additive to §5.2 (see ``05_계약_변경_제안서`` §4.4). It
    carries the spec's ``unityHints.assetsNeeded`` verbatim so AssetGen can ask
    for one asset per named item instead of feeding it the whole ``description``
    and letting keyword order decide. Defaults to empty, so a planner that does
    not fill it keeps the previous behaviour.
    """

    feature_id: str
    title: str
    description: str
    priority: FeaturePriority
    dependencies: list[str] = Field(default_factory=list)
    assets_needed: list[str] = Field(default_factory=list)


class QATestCase(BaseModel):
    case_id: str
    description: str
    expected_result: str


class QAPolicy(BaseModel):
    """§5.3 — produced by QaMcpServer."""

    test_cases: list[QATestCase]
    acceptance_criteria: list[str]


class CompileError(BaseModel):
    """Raw compiler diagnostic returned by UnityMcpServer's ``get_compile_errors``."""

    file: str
    line: int | None = None
    message: str


class ExecutionErrorReport(BaseModel):
    """§5.4 — produced by QaMcpServer, consumed by Developer AI on QA failure."""

    error_type: ErrorType
    message: str
    file: str | None = None
    line: int | None = None
    suggested_fix: str
    related_feature_id: str


class JobState(BaseModel):
    """§5.5 — Orchestrator-internal representation of a game generation job."""

    game_id: str
    status: JobStatus
    iteration_count: int = 0
    current_stage: str
    history: list[dict] = Field(default_factory=list)
