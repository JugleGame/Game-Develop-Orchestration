"""Unit tests for JobRepository (§CLAUDE.md: every Service/Repository unit-tested)."""

import pytest

from app.models.schemas import JobStatus
from app.repository.job_repository import JobRepository
from app.services.exceptions import GameJobNotFoundError


async def test_create_and_get(session_factory):
    async with session_factory() as session:
        repo = JobRepository(session)
        job = await repo.create(game_id="game-1", prompt="a platformer")

    assert job.status == JobStatus.PLANNING.value
    assert job.current_stage == "Intake"

    async with session_factory() as session:
        fetched = await JobRepository(session).get("game-1")
    assert fetched is not None
    assert fetched.prompt == "a platformer"


async def test_get_or_raise_missing(session_factory):
    async with session_factory() as session:
        with pytest.raises(GameJobNotFoundError):
            await JobRepository(session).get_or_raise("missing")


async def test_update_fields(session_factory):
    async with session_factory() as session:
        repo = JobRepository(session)
        await repo.create(game_id="game-2", prompt="a puzzle game")
        updated = await repo.update_fields(
            "game-2", status=JobStatus.DEVELOPING.value, current_stage="CodeGen"
        )

    assert updated.status == JobStatus.DEVELOPING.value
    assert updated.current_stage == "CodeGen"


async def test_append_history(session_factory):
    async with session_factory() as session:
        repo = JobRepository(session)
        await repo.create(game_id="game-3", prompt="a racing game")
        await repo.append_history("game-3", {"stage": "Intake", "status": "planning"})
        job = await repo.append_history("game-3", {"stage": "Planning", "status": "planning"})

    assert job.history == [
        {"stage": "Intake", "status": "planning"},
        {"stage": "Planning", "status": "planning"},
    ]
