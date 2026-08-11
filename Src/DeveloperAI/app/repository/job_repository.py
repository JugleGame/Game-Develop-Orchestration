"""Persistence for game generation jobs. No business logic lives here."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import desc

from app.models.orm import GameJob
from app.models.schemas import JobStatus
from app.services.exceptions import GameJobNotFoundError


class JobRepository:
    """CRUD access to the ``game_jobs`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, game_id: str, prompt: str) -> GameJob:
        job = GameJob(
            game_id=game_id,
            prompt=prompt,
            status=JobStatus.PLANNING.value,
            current_stage="Intake",
            history=[],
        )
        self._session.add(job)
        await self._session.commit()
        await self._session.refresh(job)
        return job

    async def get(self, game_id: str) -> GameJob | None:
        return await self._session.get(GameJob, game_id)

    async def get_or_raise(self, game_id: str) -> GameJob:
        job = await self.get(game_id)
        if job is None:
            raise GameJobNotFoundError(game_id)
        return job

    async def list_by_status(self, status: JobStatus) -> list[GameJob]:
        result = await self._session.execute(select(GameJob).where(GameJob.status == status.value))
        return list(result.scalars().all())

    async def list_recent(self, *, limit: int) -> list[GameJob]:
        """Most recently updated jobs first — backs the history view (§6)."""

        result = await self._session.execute(
            select(GameJob).order_by(desc(GameJob.updated_at)).limit(limit)
        )
        return list(result.scalars().all())

    async def update_fields(self, game_id: str, **fields: Any) -> GameJob:
        job = await self.get_or_raise(game_id)
        for key, value in fields.items():
            setattr(job, key, value)
        await self._session.commit()
        await self._session.refresh(job)
        return job

    async def append_history(self, game_id: str, entry: dict) -> GameJob:
        job = await self.get_or_raise(game_id)
        job.history = [*job.history, entry]
        await self._session.commit()
        await self._session.refresh(job)
        return job
