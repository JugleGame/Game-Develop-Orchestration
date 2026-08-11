"""Runtime configuration for the DeveloperAI orchestrator.

Values are sourced from environment variables (see the repository-root
``.env.example``). That one file also configures the MCP tool servers, which
read it through ``Src/McpServers/common/env.py`` — a new machine fills in a
single file rather than one per distribution.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# settings.py -> config -> app -> DeveloperAI -> Src -> repository root
REPO_ROOT = Path(__file__).resolve().parents[4]

# ``GDAI_SKIP_DOTENV`` detaches the process from the shared file, which is how
# the test suite keeps a developer's local .env from deciding what it asserts.
# The MCP servers honour the same switch (``McpServers/common/env.py``).
_ENV_FILES: tuple[Path | str, ...] = (
    () if os.getenv("GDAI_SKIP_DOTENV") else (REPO_ROOT / ".env", ".env")
)


class Settings(BaseSettings):
    """Application-wide settings, loaded once and cached."""

    # The repository-root file is the shared one; a ``.env`` beside the
    # working directory still wins, which keeps per-checkout overrides and the
    # previous behavior intact (later files take priority).
    model_config = SettingsConfigDict(env_file=_ENV_FILES, extra="ignore")

    database_url: str = "postgresql+asyncpg://developer_ai:developer_ai@localhost:5432/developer_ai"
    redis_url: str = "redis://localhost:6379/0"

    # MCP Streamable HTTP endpoints (§03). The path is the server's single
    # JSON-RPC endpoint, not a per-tool route — tools are dispatched by name
    # inside the request body.
    strategic_mcp_url: str = "http://localhost:9101/mcp"
    unity_mcp_url: str = "http://localhost:9102/mcp"
    qa_mcp_url: str = "http://localhost:9103/mcp"
    asset_mcp_url: str = "http://localhost:9104/mcp"
    git_mcp_url: str = "http://localhost:9105/mcp"

    strategic_mcp_timeout_seconds: float = 120.0
    unity_mcp_timeout_seconds: float = 300.0
    qa_mcp_timeout_seconds: float = 180.0
    asset_mcp_timeout_seconds: float = 180.0
    git_mcp_timeout_seconds: float = 60.0

    mcp_max_retries: int = 3

    development_qa_max_iterations: int = 5

    # Max ApprovalGate rejections before escalating to a human (§3.3 spirit:
    # every retry loop needs an exit condition).
    planning_max_iterations: int = 5

    # Max ConceptGate revise rounds. Lower than the others because these
    # rounds are cheap but purely human-paced: past a few, the idea needs a
    # conversation, not another proposal.
    concept_max_iterations: int = 3

    # Unity Editor can serve one job at a time (§7.1): admit jobs serially by
    # default; raise only when the Unity layer gains an editor instance pool.
    max_concurrent_jobs: int = 1

    log_level: str = "INFO"

    # Mirror the JSON log stream to this file in addition to stdout. Unset
    # keeps stdout-only (correct for containers, where the runtime collects
    # it); set it when running bare so a crashed session leaves a record, and
    # so a log shipper has a file to tail.
    log_file: str | None = None
    log_file_max_bytes: int = 10_000_000
    log_file_backup_count: int = 3

    cors_allow_origins: list[str] = ["http://localhost:5173"]

    # Shared secret required on every request via the X-API-Key header (or
    # ?api_key= for EventSource, which cannot set headers). POST /games is an
    # open LLM-spend trigger without it. None disables auth — local dev only.
    api_key: str | None = None

    # Sliding-window cap on the job-creating endpoints (POST /games and
    # /revise) per client IP per minute. 0 disables. In-process only: back it
    # with a shared store (Redis) before running multiple replicas.
    rate_limit_create_per_minute: int = 10

    @field_validator("api_key", "log_file", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat ``API_KEY=`` in a ``.env`` as "no key", not as the empty key.

        ``.env.example`` lists both variables with an empty value so they are
        discoverable. Without this, copying the file verbatim would leave
        ``api_key == ""``, which is not ``None`` — and ``require_api_key``
        would then reject every request that did not send an empty key.
        """

        return None if isinstance(value, str) and not value.strip() else value

    @property
    def checkpointer_database_url(self) -> str:
        """``database_url`` for the psycopg-based LangGraph checkpointer.

        SQLAlchemy needs the ``postgresql+asyncpg://`` dialect prefix, but
        ``AsyncPostgresSaver`` speaks plain ``postgresql://`` (psycopg).
        """

        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached ``Settings`` instance."""

    return Settings()
