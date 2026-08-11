"""Persistence for pipeline traces. No business logic lives here.

A trace is written in two moves, because the facts arrive at different times:
CodeGen knows the prompt and the source, and only CompileCheck/FunctionalTest
later know whether either was any good. So ``record_generation`` inserts the
rows for an attempt and the two ``record_*_result`` methods fill in the verdict
for that same attempt.
"""

from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import desc

from app.models.orm import PipelineTrace


class TraceRepository:
    """CRUD access to the ``pipeline_traces`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_generation(
        self,
        *,
        game_id: str,
        attempt: int,
        project_context: str,
        scripts: list[dict[str, Any]],
    ) -> list[PipelineTrace]:
        """Insert one row per script produced by this attempt.

        Scripts carried over unchanged from a previous attempt are skipped: they
        already have a row from the attempt that generated them, and inserting
        them again would count one generation several times and inflate any
        success rate computed from this table.
        """

        rows = [
            PipelineTrace(
                game_id=game_id,
                feature_id=script.get("feature_id") or "",
                attempt=attempt,
                prompt=script.get("prompt") or "",
                project_context=project_context or None,
                file=script.get("file") or "",
                generated_source=script.get("contents") or None,
            )
            for script in scripts
            if script.get("generated")
        ]
        if not rows:
            return []

        self._session.add_all(rows)
        await self._session.commit()
        return rows

    async def record_compile_result(
        self,
        *,
        game_id: str,
        attempt: int,
        compile_ok: bool,
        compile_errors: list[dict[str, Any]] | None,
    ) -> int:
        """Label every row of ``attempt``. Returns how many were labelled."""

        result = await self._session.execute(
            update(PipelineTrace)
            .where(PipelineTrace.game_id == game_id, PipelineTrace.attempt == attempt)
            .values(compile_ok=compile_ok, compile_errors=compile_errors or None)
        )
        await self._session.commit()
        return int(result.rowcount or 0)

    async def record_qa_result(
        self,
        *,
        game_id: str,
        attempt: int,
        verdict: str,
        report: dict[str, Any] | None,
    ) -> int:
        result = await self._session.execute(
            update(PipelineTrace)
            .where(PipelineTrace.game_id == game_id, PipelineTrace.attempt == attempt)
            .values(qa_verdict=verdict, qa_report=report or None)
        )
        await self._session.commit()
        return int(result.rowcount or 0)

    async def list_for_game(self, game_id: str) -> list[PipelineTrace]:
        result = await self._session.execute(
            select(PipelineTrace)
            .where(PipelineTrace.game_id == game_id)
            .order_by(PipelineTrace.attempt, PipelineTrace.trace_id)
        )
        return list(result.scalars().all())

    async def list_recent(self, *, limit: int) -> list[PipelineTrace]:
        result = await self._session.execute(
            select(PipelineTrace).order_by(desc(PipelineTrace.trace_id)).limit(limit)
        )
        return list(result.scalars().all())

    async def list_verified_pairs(self) -> list[PipelineTrace]:
        """Rows that compiled and passed QA — the verified (prompt, code) pairs
        described in ``PipelineTrace``'s docstring and ``CLAUDE.md``."""

        result = await self._session.execute(
            select(PipelineTrace)
            .where(PipelineTrace.compile_ok.is_(True), PipelineTrace.qa_verdict == "PASS")
            .order_by(PipelineTrace.game_id, PipelineTrace.feature_id, PipelineTrace.attempt)
        )
        return list(result.scalars().all())

    async def list_before_after_pairs(self) -> list[tuple[PipelineTrace, PipelineTrace]]:
        """An ``attempt=1`` failure paired with the ``attempt=2`` success for the
        same ``(game_id, feature_id)`` — the before/after example described in
        ``PipelineTrace``'s docstring. Only that exact attempt pairing counts;
        a feature that took three or more attempts is not matched here.
        """

        failed = await self._session.execute(
            select(PipelineTrace).where(
                PipelineTrace.attempt == 1,
                or_(PipelineTrace.compile_ok.is_(False), PipelineTrace.qa_verdict == "FAIL"),
            )
        )
        failures = list(failed.scalars().all())
        if not failures:
            return []

        succeeded = await self._session.execute(
            select(PipelineTrace).where(
                PipelineTrace.attempt == 2,
                PipelineTrace.compile_ok.is_(True),
                PipelineTrace.qa_verdict == "PASS",
            )
        )
        successes_by_key = {
            (row.game_id, row.feature_id): row for row in succeeded.scalars().all()
        }

        pairs = []
        for failure in failures:
            success = successes_by_key.get((failure.game_id, failure.feature_id))
            if success is not None:
                pairs.append((failure, success))
        return pairs
