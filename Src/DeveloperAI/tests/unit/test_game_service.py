"""Unit tests for GameService business logic, with a stubbed WorkflowRunner."""

import asyncio

import pytest

from app.models.schemas import JobStatus
from app.repository.job_repository import JobRepository
from app.services.exceptions import GameJobNotFoundError, InvalidJobStateError
from app.services.game_service import GameService


class _StubWorkflowRunner:
    def __init__(self) -> None:
        self.start_calls: list[tuple[str, str, str | None]] = []
        self.resume_calls: list[tuple[str, bool, str | None]] = []

    async def start(self, game_id: str, prompt: str, *, repo_name: str | None = None) -> None:
        self.start_calls.append((game_id, prompt, repo_name))

    async def resume(
        self,
        game_id: str,
        *,
        approved: bool,
        feedback: str | None,
        edited_idea: str | None = None,
    ) -> None:
        # ``edited_idea`` is ConceptGate's; ApprovalGate ignores it. Recorded
        # so a test can assert it reaches the runner unchanged.
        self.resume_calls.append((game_id, approved, feedback, edited_idea))


class _CrashingWorkflowRunner:
    """Simulates a bug outside the ToolCallError path (which _run handles)."""

    async def start(self, game_id: str, prompt: str, *, repo_name: str | None = None) -> None:
        raise RuntimeError("unexpected bug")


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, data))


@pytest.fixture
def service(session_factory):
    return GameService(
        session_factory=session_factory, redis_client=None, runner=_StubWorkflowRunner()
    )


async def test_create_game_persists_and_starts_workflow(service):
    response = await service.create_game("a tower defense game")

    assert response.status == JobStatus.PLANNING
    await asyncio.sleep(0)  # let the scheduled task run
    assert service._runner.start_calls == [(response.game_id, "a tower defense game", None)]


async def test_get_status_unknown_game_raises(service):
    with pytest.raises(GameJobNotFoundError):
        await service.get_status("does-not-exist")


async def test_get_status_surfaces_feature_prompts(service, session_factory):
    response = await service.create_game("a rpg")
    async with session_factory() as session:
        await JobRepository(session).update_fields(
            response.game_id,
            feature_prompts=[
                {
                    "feature_id": "f-1",
                    "title": "Core movement",
                    "description": "Add jump",
                    "priority": "P0",
                    "dependencies": [],
                }
            ],
        )

    status = await service.get_status(response.game_id)
    assert status.feature_prompts is not None
    assert status.feature_prompts[0].feature_id == "f-1"


async def test_get_status_reports_images_consumed_from_the_pixellab_quota(
    service, session_factory
):
    """The count lives inside ``token_usage``; the API has to lift it out.

    A job with no asset work must read 0 rather than null — the dashboard
    shows the line only when something was actually consumed.
    """

    response = await service.create_game("a rpg")
    assert (await service.get_status(response.game_id)).images_generated == 0

    async with session_factory() as session:
        await JobRepository(session).update_fields(
            response.game_id, token_usage={"cost_usd": 1.5, "images_generated": 7}
        )

    assert (await service.get_status(response.game_id)).images_generated == 7


async def test_approve_requires_awaiting_approval_status(service):
    response = await service.create_game("a rpg")
    with pytest.raises(InvalidJobStateError):
        await service.approve(response.game_id, approved=True, feedback=None)


async def test_approve_success_schedules_resume(service, session_factory):
    response = await service.create_game("a rpg")
    async with session_factory() as session:
        await JobRepository(session).update_fields(
            response.game_id, status=JobStatus.AWAITING_APPROVAL.value
        )

    approval = await service.approve(response.game_id, approved=True, feedback="looks good")
    assert approval.status == JobStatus.DEVELOPING
    await asyncio.sleep(0)
    assert service._runner.resume_calls == [(response.game_id, True, "looks good", None)]


async def test_get_artifact_requires_done_status(service):
    response = await service.create_game("a shooter")
    with pytest.raises(InvalidJobStateError):
        await service.get_artifact(response.game_id)


async def test_get_artifact_returns_payload_when_done(service, session_factory):
    response = await service.create_game("a shooter")
    async with session_factory() as session:
        await JobRepository(session).update_fields(
            response.game_id,
            status=JobStatus.DONE.value,
            artifact={
                "repo_name": "shooter-a-shooter-abc12345",
                "commit_hash": "abc123",
                "tag": "v1",
                "repository_url": "https://example.test/repo",
            },
        )

    artifact = await service.get_artifact(response.game_id)
    assert artifact.repo_name == "shooter-a-shooter-abc12345"
    assert artifact.commit_hash == "abc123"
    assert artifact.tag == "v1"


async def test_cancel_marks_job_cancelled(service):
    response = await service.create_game("a card game")
    cancelled = await service.cancel(response.game_id)
    assert cancelled.status == JobStatus.CANCELLED

    status = await service.get_status(response.game_id)
    assert status.status == JobStatus.CANCELLED


async def test_list_games_returns_most_recent_first(service, session_factory):
    first = await service.create_game("a platformer")
    # Populate first's summary fields *before* creating second: update_fields
    # bumps updated_at, and second must stay the most recently touched job.
    async with session_factory() as session:
        await JobRepository(session).update_fields(
            first.game_id, game_design={"genre": "platformer"}, repo_name="platformer-x-1234"
        )
    # This machine's clock resolution is ~15.6ms (measured: time.get_clock_info
    # ("time").resolution == 0.015625 on Windows) — two datetime.now() calls
    # this close together tie almost every time (measured: >99.9% of
    # back-to-back calls), which made ordering below flaky under a busy full
    # suite run. The two writes must land in different ticks for `updated_at`
    # to actually differ.
    await asyncio.sleep(0.02)
    second = await service.create_game("a shooter")

    games = await service.list_games()
    game_ids = [g.game_id for g in games]
    assert game_ids.index(second.game_id) < game_ids.index(first.game_id)

    first_summary = next(g for g in games if g.game_id == first.game_id)
    assert first_summary.genre == "platformer"
    assert first_summary.repo_name == "platformer-x-1234"


async def test_revise_game_requires_done_status(service):
    response = await service.create_game("a puzzle game")
    with pytest.raises(InvalidJobStateError):
        await service.revise_game(response.game_id, "add a hint button")


async def test_revise_game_reuses_existing_repo_name(service, session_factory):
    response = await service.create_game("a puzzle game")
    async with session_factory() as session:
        await JobRepository(session).update_fields(
            response.game_id,
            status=JobStatus.DONE.value,
            repo_name="puzzle-a-puzzle-game-abcd1234",
        )

    revised = await service.revise_game(response.game_id, "add a hint button")
    assert revised.status == JobStatus.PLANNING
    await asyncio.sleep(0)
    assert service._runner.start_calls[-1] == (
        response.game_id,
        "add a hint button",
        "puzzle-a-puzzle-game-abcd1234",
    )


async def test_unexpected_workflow_crash_marks_job_escalated(session_factory):
    """A crash outside the ToolCallError path must not die silently: the job
    is marked escalated and an event is published so the UI stops spinning."""

    redis = _FakeRedis()
    service = GameService(
        session_factory=session_factory, redis_client=redis, runner=_CrashingWorkflowRunner()
    )

    response = await service.create_game("a racing game")

    # Wait for the workflow task itself to crash first...
    task = service._tasks.get(response.game_id)
    if task is not None:
        await asyncio.wait({task})
    # ...then poll (generous budget: the done-callback schedules _mark_failed
    # as a separate task) until *both* the DB write and the redis publish
    # have landed. _mark_failed does them as two sequential awaits on its own
    # task, so a poll iteration can observe the DB already updated while the
    # publish call — a separate task from this loop's perspective — hasn't
    # completed yet. Waiting on only the DB status is a race.
    for _ in range(500):
        await asyncio.sleep(0.01)
        async with session_factory() as session:
            job = await JobRepository(session).get_or_raise(response.game_id)
        if job.status == JobStatus.ESCALATED.value and redis.published:
            break

    assert job.status == JobStatus.ESCALATED.value
    assert redis.published, "an escalation event must be published"
