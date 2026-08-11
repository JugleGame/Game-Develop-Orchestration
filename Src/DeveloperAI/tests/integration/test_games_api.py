"""Integration tests for the §6 HTTP API, isolated from real Postgres/Redis/MCP
infrastructure via a stub GameService."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_game_service
from app.api.routers.games import router as games_router
from app.models.api import (
    ApprovalResponse,
    CancelResponse,
    GameCreateResponse,
    GameStatusResponse,
    GameSummaryResponse,
)
from app.models.schemas import JobStatus
from app.services.exceptions import GameJobNotFoundError, InvalidJobStateError


class _StubGameService:
    def __init__(self) -> None:
        self.created_prompts: list[str] = []
        self.revise_calls: list[tuple[str, str]] = []

    async def create_game(self, prompt: str) -> GameCreateResponse:
        self.created_prompts.append(prompt)
        return GameCreateResponse(game_id="game-1", status=JobStatus.PLANNING)

    async def list_games(self, *, limit: int = 50) -> list[GameSummaryResponse]:
        return [
            GameSummaryResponse(
                game_id="game-1",
                prompt="a puzzle game",
                status=JobStatus.DONE,
                current_stage="Deployment",
                genre="puzzle",
                repo_name="puzzle-a-puzzle-game-game-1",
                iteration_count=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        ]

    async def revise_game(self, game_id: str, prompt: str) -> GameCreateResponse:
        self.revise_calls.append((game_id, prompt))
        if game_id != "game-1":
            raise GameJobNotFoundError(game_id)
        return GameCreateResponse(game_id=game_id, status=JobStatus.PLANNING)

    async def get_status(self, game_id: str) -> GameStatusResponse:
        if game_id != "game-1":
            raise GameJobNotFoundError(game_id)
        return GameStatusResponse(
            game_id=game_id,
            status=JobStatus.PLANNING,
            current_stage="Planning",
            iteration_count=0,
            game_design=None,
            last_error=None,
            updated_at=datetime.now(timezone.utc),
        )

    async def approve(
        self,
        game_id: str,
        *,
        approved: bool,
        feedback: str | None,
        edited_idea: str | None = None,
    ) -> ApprovalResponse:
        if game_id != "game-1":
            raise GameJobNotFoundError(game_id)
        raise InvalidJobStateError(
            game_id, JobStatus.AWAITING_APPROVAL.value, JobStatus.PLANNING.value
        )

    async def get_artifact(self, game_id: str):
        raise InvalidJobStateError(game_id, JobStatus.DONE.value, JobStatus.PLANNING.value)

    async def cancel(self, game_id: str) -> CancelResponse:
        return CancelResponse(game_id=game_id, status=JobStatus.CANCELLED)


def _build_app(stub: _StubGameService) -> FastAPI:
    app = FastAPI()
    app.include_router(games_router)

    from fastapi.responses import JSONResponse

    @app.exception_handler(GameJobNotFoundError)
    async def handle_not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InvalidJobStateError)
    async def handle_invalid_state(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    app.dependency_overrides[get_game_service] = lambda: stub
    return app


@pytest.fixture
def stub() -> _StubGameService:
    return _StubGameService()


@pytest.fixture
def client(stub: _StubGameService) -> TestClient:
    return TestClient(_build_app(stub))


def test_create_game_returns_201(client: TestClient, stub: _StubGameService):
    response = client.post("/games", json={"prompt": "a puzzle game"})
    assert response.status_code == 201
    assert response.json()["status"] == "planning"
    assert stub.created_prompts == ["a puzzle game"]


def test_create_game_requires_prompt(client: TestClient):
    response = client.post("/games", json={"prompt": ""})
    assert response.status_code == 422


def test_get_status_found(client: TestClient):
    response = client.get("/games/game-1/status")
    assert response.status_code == 200
    assert response.json()["current_stage"] == "Planning"


def test_get_status_not_found_maps_to_404(client: TestClient):
    response = client.get("/games/missing/status")
    assert response.status_code == 404


def test_approve_invalid_state_maps_to_409(client: TestClient):
    response = client.post("/games/game-1/approve", json={"approved": True})
    assert response.status_code == 409


def test_get_artifact_invalid_state_maps_to_409(client: TestClient):
    response = client.get("/games/game-1/artifact")
    assert response.status_code == 409


def test_cancel_game(client: TestClient):
    response = client.post("/games/game-1/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_list_games_returns_history(client: TestClient):
    response = client.get("/games")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["repo_name"] == "puzzle-a-puzzle-game-game-1"


def test_revise_game_starts_new_run(client: TestClient, stub: _StubGameService):
    response = client.post("/games/game-1/revise", json={"prompt": "add a hint button"})
    assert response.status_code == 200
    assert response.json()["status"] == "planning"
    assert stub.revise_calls == [("game-1", "add a hint button")]


def test_revise_game_not_found_maps_to_404(client: TestClient):
    response = client.post("/games/missing/revise", json={"prompt": "add a hint button"})
    assert response.status_code == 404
