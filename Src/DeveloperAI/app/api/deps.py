"""FastAPI dependency providers. No business logic — wiring and request gating only.

Both gates read their configuration from ``app.state`` (set in ``main.lifespan``);
apps that never set it (unit-test apps, local demos without an API key) get the
disabled behavior automatically.
"""

from fastapi import HTTPException, Request

from app.services.game_service import GameService


def get_game_service(request: Request) -> GameService:
    return request.app.state.game_service


async def require_api_key(request: Request) -> None:
    """Reject the request unless it carries the configured API key.

    Accepted via the ``X-API-Key`` header, or ``?api_key=`` as a fallback for
    ``EventSource`` (the browser SSE client cannot set request headers).
    Disabled when no key is configured.
    """

    expected = getattr(request.app.state, "api_key", None)
    if expected is None:
        return
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def enforce_create_rate_limit(request: Request) -> None:
    """Cap job-creating requests per client IP (they trigger LLM spend)."""

    limiter = getattr(request.app.state, "create_rate_limiter", None)
    if limiter is None:
        return
    client_ip = request.client.host if request.client else "unknown"
    if not limiter.allow(client_ip):
        raise HTTPException(
            status_code=429, detail="Too many game creation requests; retry later"
        )
