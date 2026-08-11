"""Business logic for game generation jobs. The only layer allowed to hold it."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.api import (
    ApprovalResponse,
    ArtifactResponse,
    CancelResponse,
    GameCreateResponse,
    GameStatusResponse,
    GameSummaryResponse,
)
from app.models.schemas import (
    ExecutionErrorReport,
    FeatureImplementationPrompt,
    GameDesignDocument,
    JobStatus,
)
from app.graph.state import Stage
from app.repository.job_repository import JobRepository
from app.services.exceptions import InvalidJobStateError
from app.services.workflow_runner import WorkflowRunner
from app.utils.events import JobEvent, publish_job_event, subscribe_job_events
from app.utils.logging import get_logger, log_extra

logger = get_logger(__name__)


class GameService:
    """Implements the §6 API operations on top of the Job Store and the graph runner."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        redis_client: redis.Redis,
        runner: WorkflowRunner,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._runner = runner
        self._tasks: dict[str, asyncio.Task] = {}
        # asyncio.create_task() schedules a task but the loop only holds a
        # *weak* reference to it — with nothing else referencing the task
        # object, it can be garbage-collected mid-flight, silently killing it
        # with no exception logged anywhere (a documented asyncio pitfall).
        # _mark_failed's task, created inside _on_done below, had exactly
        # this bug: it never ran, so a crashed workflow's job stayed at its
        # last status forever with no escalation. This set holds the strong
        # reference until the task finishes.
        self._background_tasks: set[asyncio.Task] = set()

    async def create_game(self, prompt: str) -> GameCreateResponse:
        game_id = str(uuid.uuid4())
        async with self._session_factory() as session:
            repo = JobRepository(session)
            await repo.create(game_id=game_id, prompt=prompt)

        self._launch(game_id, self._runner.start(game_id, prompt))
        return GameCreateResponse(game_id=game_id, status=JobStatus.PLANNING)

    async def get_status(self, game_id: str) -> GameStatusResponse:
        async with self._session_factory() as session:
            job = await JobRepository(session).get_or_raise(game_id)

        return GameStatusResponse(
            game_id=job.game_id,
            status=JobStatus(job.status),
            current_stage=job.current_stage,
            iteration_count=job.dev_iteration_count + job.qa_iteration_count,
            concept=job.concept,
            game_design=(
                GameDesignDocument.model_validate(job.game_design) if job.game_design else None
            ),
            feature_prompts=(
                [FeatureImplementationPrompt.model_validate(fp) for fp in job.feature_prompts]
                if job.feature_prompts
                else None
            ),
            last_error=(
                ExecutionErrorReport.model_validate(job.last_error) if job.last_error else None
            ),
            images_generated=int((job.token_usage or {}).get("images_generated") or 0),
            updated_at=job.updated_at,
        )

    async def list_games(self, *, limit: int = 50) -> list[GameSummaryResponse]:
        async with self._session_factory() as session:
            jobs = await JobRepository(session).list_recent(limit=limit)

        return [
            GameSummaryResponse(
                game_id=job.game_id,
                prompt=job.prompt,
                status=JobStatus(job.status),
                current_stage=job.current_stage,
                genre=(job.game_design or {}).get("genre"),
                repo_name=job.repo_name,
                iteration_count=job.dev_iteration_count + job.qa_iteration_count,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            for job in jobs
        ]

    async def revise_game(self, game_id: str, prompt: str) -> GameCreateResponse:
        """Re-run the pipeline for an already-shipped game with a new prompt
        (e.g. "add a double-jump ability"), reusing its existing Git repo."""

        async with self._session_factory() as session:
            job = await JobRepository(session).get_or_raise(game_id)

        if job.status != JobStatus.DONE.value:
            raise InvalidJobStateError(game_id, JobStatus.DONE.value, job.status)

        self._launch(game_id, self._runner.start(game_id, prompt, repo_name=job.repo_name))
        return GameCreateResponse(game_id=game_id, status=JobStatus.PLANNING)

    async def approve(
        self,
        game_id: str,
        *,
        approved: bool,
        feedback: str | None,
        edited_idea: str | None = None,
    ) -> ApprovalResponse:
        """Resume the gate this job is paused at (ConceptGate or ApprovalGate).

        Both gates pause with ``AWAITING_APPROVAL``; ``current_stage`` is what
        distinguishes them, and the caller does not have to: the same decision
        shape resumes either one.
        """

        async with self._session_factory() as session:
            job = await JobRepository(session).get_or_raise(game_id)

        if job.status != JobStatus.AWAITING_APPROVAL.value:
            raise InvalidJobStateError(game_id, JobStatus.AWAITING_APPROVAL.value, job.status)

        self._launch(
            game_id,
            self._runner.resume(
                game_id, approved=approved, feedback=feedback, edited_idea=edited_idea
            ),
        )

        # Approving the concept moves on to Planning, not to development —
        # the blueprint has not been written yet.
        if job.current_stage == Stage.CONCEPT_GATE:
            next_status = JobStatus.PLANNING
        else:
            next_status = JobStatus.DEVELOPING if approved else JobStatus.PLANNING
        return ApprovalResponse(game_id=game_id, status=next_status)

    async def get_artifact(self, game_id: str) -> ArtifactResponse:
        async with self._session_factory() as session:
            job = await JobRepository(session).get_or_raise(game_id)

        if job.status != JobStatus.DONE.value or not job.artifact:
            raise InvalidJobStateError(game_id, JobStatus.DONE.value, job.status)

        artifact = job.artifact
        return ArtifactResponse(
            game_id=game_id,
            repo_name=artifact["repo_name"],
            repository_url=artifact.get("repository_url"),
            commit_hash=artifact["commit_hash"],
            tag=artifact["tag"],
            build_download_url=artifact.get("build_download_url"),
        )

    async def cancel(self, game_id: str) -> CancelResponse:
        async with self._session_factory() as session:
            repo = JobRepository(session)
            await repo.get_or_raise(game_id)
            task = self._tasks.pop(game_id, None)
            if task is not None and not task.done():
                task.cancel()
            await repo.update_fields(game_id, status=JobStatus.CANCELLED.value)

        return CancelResponse(game_id=game_id, status=JobStatus.CANCELLED)

    async def stream_events(self, game_id: str) -> AsyncIterator[str]:
        async with self._session_factory() as session:
            await JobRepository(session).get_or_raise(game_id)

        async for payload in subscribe_job_events(self._redis, game_id):
            yield payload

    def _launch(self, game_id: str, coro: Coroutine[Any, Any, None]) -> None:
        """Start a fire-and-forget workflow task with crash surveillance attached."""

        task = asyncio.create_task(coro)
        self._tasks[game_id] = task
        self._watch(game_id, task)

    def _watch(self, game_id: str, task: asyncio.Task) -> None:
        """Ensure an unexpected crash is logged and surfaced, never silent.

        ``WorkflowRunner`` already handles ``ToolCallError``; this is the
        safety net for everything else (validation errors, DB failures, bugs)
        that would otherwise leave the job stuck at its last status forever.
        """

        def _on_done(t: asyncio.Task) -> None:
            self._tasks.pop(game_id, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "Workflow task crashed unexpectedly",
                    extra=log_extra(game_id=game_id, error=repr(exc)),
                )
                mark_failed_task = asyncio.get_running_loop().create_task(
                    self._mark_failed(game_id)
                )
                self._background_tasks.add(mark_failed_task)
                mark_failed_task.add_done_callback(self._background_tasks.discard)

        task.add_done_callback(_on_done)

    async def _mark_failed(self, game_id: str) -> None:
        """Persist + publish the escalated status after an unexpected crash."""

        try:
            async with self._session_factory() as session:
                await JobRepository(session).update_fields(
                    game_id, status=JobStatus.ESCALATED.value
                )
            await publish_job_event(
                self._redis,
                JobEvent(
                    game_id=game_id,
                    stage=Stage.HUMAN_ESCALATION,
                    status=JobStatus.ESCALATED.value,
                ),
            )
        except Exception:  # noqa: BLE001 — last-resort path; swallowing would hide it
            logger.exception(
                "Failed to record crashed workflow", extra=log_extra(game_id=game_id)
            )
