"""Request/response DTOs for the DeveloperAI HTTP API (§6)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.schemas import (
    ExecutionErrorReport,
    FeatureImplementationPrompt,
    GameDesignDocument,
    JobStatus,
)


class GameCreateRequest(BaseModel):
    prompt: str = Field(min_length=1)


class GameCreateResponse(BaseModel):
    game_id: str
    status: JobStatus


class GameSummaryResponse(BaseModel):
    """One row of the game history list (§6 ``GET /games``)."""

    game_id: str
    prompt: str
    status: JobStatus
    current_stage: str
    genre: str | None = None
    repo_name: str | None = None
    iteration_count: int
    created_at: datetime
    updated_at: datetime


class GameStatusResponse(BaseModel):
    game_id: str
    status: JobStatus
    current_stage: str
    iteration_count: int
    # Present while paused at ConceptGate: the idea and the research evidence
    # the user is being asked to approve. Kept as a free-form mapping because
    # the evidence shape is StrategicMcpServer's, not a §5 contract model.
    concept: dict[str, Any] | None = None
    game_design: GameDesignDocument | None = None
    feature_prompts: list[FeatureImplementationPrompt] | None = None
    last_error: ExecutionErrorReport | None = None
    # PixelLab images this job consumed from the account's monthly quota. The
    # quota is what runs out, so it is the number a user has to be able to see
    # while a run is in flight — a job that burns it is otherwise silent.
    images_generated: int = 0
    updated_at: datetime


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str | None = None
    # ConceptGate only: rewrite the idea instead of only rejecting it, so a
    # revise round proposes on the user's wording. Ignored by ApprovalGate.
    edited_idea: str | None = None


class ApprovalResponse(BaseModel):
    game_id: str
    status: JobStatus


class ArtifactResponse(BaseModel):
    game_id: str
    repo_name: str
    repository_url: str | None = None
    commit_hash: str
    tag: str
    build_download_url: str | None = None


class CancelResponse(BaseModel):
    game_id: str
    status: JobStatus
