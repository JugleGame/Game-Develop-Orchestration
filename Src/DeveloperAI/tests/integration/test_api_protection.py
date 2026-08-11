"""Integration tests for API-key auth and the create-endpoint rate limit.

Uses the same stub-service pattern as test_games_api; gating configuration
lives on ``app.state`` so each test controls it directly.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_game_service
from app.api.routers.games import router as games_router
from app.models.api import GameCreateResponse
from app.models.schemas import JobStatus
from app.utils.rate_limit import SlidingWindowRateLimiter


class _StubGameService:
    async def create_game(self, prompt: str) -> GameCreateResponse:
        return GameCreateResponse(game_id="game-1", status=JobStatus.PLANNING)

    async def list_games(self, *, limit: int = 50) -> list:
        return []


def _build_app(*, api_key: str | None = None, limiter: SlidingWindowRateLimiter | None = None):
    app = FastAPI()
    app.include_router(games_router)
    app.state.api_key = api_key
    app.state.create_rate_limiter = limiter
    app.dependency_overrides[get_game_service] = lambda: _StubGameService()
    return app


@pytest.fixture
def secured_client() -> TestClient:
    return TestClient(_build_app(api_key="secret"))


def test_request_without_key_is_rejected(secured_client):
    response = secured_client.get("/games")
    assert response.status_code == 401


def test_request_with_wrong_key_is_rejected(secured_client):
    response = secured_client.get("/games", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_request_with_correct_header_passes(secured_client):
    response = secured_client.post("/games", json={"prompt": "a game"}, headers={"X-API-Key": "secret"})
    assert response.status_code == 201


def test_query_param_fallback_for_eventsource_passes(secured_client):
    # EventSource cannot set headers, so ?api_key= must also be accepted.
    response = secured_client.get("/games?api_key=secret")
    assert response.status_code == 200


def test_auth_disabled_when_no_key_configured():
    client = TestClient(_build_app(api_key=None))
    assert client.get("/games").status_code == 200


def test_create_rate_limit_returns_429_over_cap():
    client = TestClient(_build_app(limiter=SlidingWindowRateLimiter(2, 60.0)))

    assert client.post("/games", json={"prompt": "one"}).status_code == 201
    assert client.post("/games", json={"prompt": "two"}).status_code == 201
    assert client.post("/games", json={"prompt": "three"}).status_code == 429


def test_rate_limit_does_not_gate_read_endpoints():
    client = TestClient(_build_app(limiter=SlidingWindowRateLimiter(1, 60.0)))

    assert client.post("/games", json={"prompt": "one"}).status_code == 201
    # Reads stay unlimited even after the create budget is spent.
    assert client.get("/games").status_code == 200
    assert client.get("/games").status_code == 200
