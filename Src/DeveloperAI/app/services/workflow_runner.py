"""Drives one LangGraph run and mirrors every node transition into the Job
Store (Postgres) and the Redis pub/sub event stream (§1, §7.4)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.graph.state import Stage
from app.mcp.exceptions import ToolCallError
from app.models.schemas import JobStatus
from app.repository.job_repository import JobRepository
from app.repository.trace_repository import TraceRepository
from app.utils import usage
from app.utils.events import JobEvent, publish_job_event
from app.utils.logging import get_logger, log_extra

logger = get_logger(__name__)


@asynccontextmanager
async def _usage_scope(game_id: str, spent_so_far: dict[str, Any] | None) -> AsyncIterator[None]:
    """Attribute every MCP call made inside to ``game_id`` and log the bill.

    ``spent_so_far`` carries the total already recorded for this job. A job that
    pauses at ApprovalGate resumes as a *second* run, so starting from zero here
    would overwrite what the planning run already spent.
    """

    with usage.track(spent_so_far) as totals:
        try:
            yield
        finally:
            if totals.calls:
                logger.info(
                    "Job LLM usage",
                    extra=log_extra(game_id=game_id, **totals.to_dict()),
                )

_STATE_TO_JOB_FIELDS = (
    "status",
    "current_stage",
    "concept",
    "game_design",
    "feature_prompts",
    "repo_name",
    "last_error",
    "artifact",
    "dev_iteration_count",
    "qa_iteration_count",
)


def _interrupted_stage(payload: Any) -> str:
    """Name the gate the graph paused at.

    LangGraph delivers ``__interrupt__`` as a sequence of ``Interrupt`` objects
    whose ``.value`` is whatever the node passed to ``interrupt()``; both gates
    put their stage there. Falls back to ApprovalGate — the only gate that
    existed before ConceptGate — so an unrecognised shape degrades to the old
    behaviour instead of writing an empty stage.
    """

    entries = payload if isinstance(payload, (list, tuple)) else [payload]
    for entry in entries:
        value = getattr(entry, "value", entry)
        if isinstance(value, dict) and value.get("stage"):
            return str(value["stage"])
    return Stage.APPROVAL_GATE


class WorkflowRunner:
    """Executes the compiled graph for a single job, start-to-pause or pause-to-end."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        session_factory: async_sessionmaker,
        redis_client: redis.Redis,
        max_concurrent_jobs: int = 1,
    ) -> None:
        self._graph = graph
        self._session_factory = session_factory
        self._redis = redis_client
        # Admission control (§7.1): the Unity Editor serves one job at a time,
        # so runs queue on this semaphore instead of hitting it concurrently.
        self._job_slot = asyncio.Semaphore(max_concurrent_jobs)

    async def start(self, game_id: str, prompt: str, *, repo_name: str | None = None) -> None:
        """Run Intake -> ... for ``game_id``.

        ``repo_name`` should be set when re-running an already-deployed game (a
        "revise" request): it makes Planning skip naming a fresh repo and keep
        pushing to the one already associated with this game.
        """

        initial_state: dict[str, Any] = {
            "game_id": game_id,
            "prompt": prompt,
            "dev_iteration_count": 0,
            "qa_iteration_count": 0,
        }
        if repo_name:
            initial_state["repo_name"] = repo_name
        await self._run(game_id, initial_state)

    async def resume(
        self,
        game_id: str,
        *,
        approved: bool,
        feedback: str | None,
        edited_idea: str | None = None,
    ) -> None:
        """Resume whichever gate the job is paused at.

        Both gates read ``approved``/``feedback``; ``edited_idea`` is only
        meaningful to ConceptGate, where it lets the user rewrite the idea
        rather than merely reject it. ApprovalGate ignores it.
        """

        await self._run(
            game_id,
            Command(
                resume={
                    "approved": approved,
                    "feedback": feedback,
                    "edited_idea": edited_idea,
                }
            ),
        )

    async def _run(self, game_id: str, graph_input: Any) -> None:
        # Waits here if another job holds the slot; released on pause/finish,
        # so an ApprovalGate pause never blocks the queue while a human decides.
        # ``usage.track`` scopes LLM spend accounting to this task, so every MCP
        # call made underneath is attributed to this game and no other.
        async with self._job_slot, _usage_scope(game_id, await self._spent_so_far(game_id)):
            config = {"configurable": {"thread_id": game_id}}
            # The attempt CodeGen last announced. Compile and QA verdicts arrive
            # as later partials that do not repeat it, and a stream consumer
            # never sees the full state, so it is carried here. ``None`` until
            # CodeGen runs, which is what keeps a verdict from being attributed
            # to an attempt this run did not produce.
            attempt: int | None = None
            try:
                async for chunk in self._graph.astream(graph_input, config, stream_mode="updates"):
                    if "__interrupt__" in chunk:
                        # There is more than one gate now (ConceptGate and
                        # ApprovalGate), so the stage is read from the payload
                        # the node paused with rather than assumed. Getting
                        # this wrong would tell the UI a concept review is a
                        # design approval and show the wrong screen.
                        stage = _interrupted_stage(chunk["__interrupt__"])
                        await self._persist(
                            game_id,
                            {
                                "status": JobStatus.AWAITING_APPROVAL.value,
                                "current_stage": stage,
                            },
                        )
                        await self._publish(
                            game_id, stage, JobStatus.AWAITING_APPROVAL.value
                        )
                        return
                    for node_name, partial in chunk.items():
                        attempt = await self._trace(game_id, node_name, partial, attempt)
                        fields = {k: v for k, v in partial.items() if k in _STATE_TO_JOB_FIELDS}
                        await self._persist(game_id, fields)
                        await self._publish(game_id, node_name, fields.get("status", ""))
            except ToolCallError as exc:
                logger.error(
                    "Workflow aborted by MCP failure",
                    extra=log_extra(
                        game_id=game_id, server=exc.server, tool=exc.tool, code=exc.code.value
                    ),
                )
                await self._persist(game_id, {"status": JobStatus.ESCALATED.value})
                await self._publish(game_id, Stage.HUMAN_ESCALATION, JobStatus.ESCALATED.value)

    async def _trace(
        self, game_id: str, node_name: str, partial: dict[str, Any], attempt: int | None
    ) -> int | None:
        """Record what this node contributed to the generation record.

        Returns the attempt number to carry into the next partial.

        Tracing is bookkeeping: a failure here must not take down a run that is
        otherwise fine, so every path is best-effort and logged rather than
        raised. The data is still worth capturing eagerly because it cannot be
        rebuilt afterwards — the generated sources only exist in flight.
        """

        try:
            if node_name == Stage.CODE_GEN:
                attempt = int(partial.get("codegen_attempt") or 1)
                scripts = partial.get("created_scripts") or []
                async with self._session_factory() as session:
                    rows = await TraceRepository(session).record_generation(
                        game_id=game_id,
                        attempt=attempt,
                        project_context=partial.get("codegen_project_context") or "",
                        scripts=scripts,
                    )
                logger.info(
                    "Pipeline trace recorded",
                    extra=log_extra(game_id=game_id, attempt=attempt, traces=len(rows)),
                )
                return attempt

            if attempt is None:
                # A verdict with no generation in this run to attach it to.
                return attempt

            if node_name == Stage.COMPILE_CHECK:
                errors = partial.get("compile_errors")
                async with self._session_factory() as session:
                    await TraceRepository(session).record_compile_result(
                        game_id=game_id,
                        attempt=attempt,
                        compile_ok=not errors,
                        compile_errors=errors,
                    )
            elif node_name == Stage.FUNCTIONAL_TEST:
                passed = partial.get("qa_passed")
                if passed is not None:
                    async with self._session_factory() as session:
                        await TraceRepository(session).record_qa_result(
                            game_id=game_id,
                            attempt=attempt,
                            verdict="PASS" if passed else "FAIL",
                            report=partial.get("last_error"),
                        )
        except Exception:  # noqa: BLE001 — bookkeeping must not abort a run
            logger.warning(
                "Pipeline trace not recorded",
                exc_info=True,
                extra=log_extra(game_id=game_id, stage=node_name),
            )
        return attempt

    async def _spent_so_far(self, game_id: str) -> dict[str, Any] | None:
        """Usage already recorded for this job, so a resume continues the tally."""

        async with self._session_factory() as session:
            job = await JobRepository(session).get(game_id)
            return job.token_usage if job is not None else None

    async def _persist(self, game_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return

        # Spend so far is written on every transition rather than only at the
        # end, so a job that crashes or is escalated still leaves an accurate
        # bill behind.
        totals = usage.current()
        if totals is not None:
            fields = {**fields, "token_usage": totals.to_dict(), "cost_usd": totals.cost_usd}

        async with self._session_factory() as session:
            repo = JobRepository(session)
            await repo.update_fields(game_id, **fields)
            # ``at`` is what makes per-stage duration derivable from history
            # alone — the difference between consecutive entries.
            await repo.append_history(
                game_id,
                {
                    "stage": fields.get("current_stage"),
                    "status": fields.get("status"),
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            )

    async def _publish(self, game_id: str, stage: str, status: str) -> None:
        event = JobEvent(game_id=game_id, stage=stage, status=status)
        await publish_job_event(self._redis, event)
        logger.info(
            "Job event published", extra=log_extra(game_id=game_id, stage=stage, status=status)
        )
