"""Run the real DeveloperAI app without Postgres, Redis, or Docker.

Production wiring (``app/main.py``) is untouched. This harness builds the same
GameService / WorkflowRunner / graph, but replaces exactly three dependencies:

======================  ===========================  ==========================
Dependency              Production                   Here
======================  ===========================  ==========================
Job store               Postgres (asyncpg)           SQLite file
LangGraph checkpointer  ``AsyncPostgresSaver``       ``InMemorySaver``
Progress events         Redis pub/sub                in-process stub
======================  ===========================  ==========================

Everything above those seams — routers, GameService, WorkflowRunner, the graph,
the MCP clients — is the production code path.

Because the checkpointer is in-memory, a restart loses in-flight runs; that is
the one behaviour this harness does *not* reproduce. Use it to exercise the
pipeline and the API, not to test crash recovery.

Usage::

    python scripts/mock_mcp_servers.py &     # five stand-in tool servers
    python scripts/dev_server.py             # http://127.0.0.1:8000

State (SQLite file, JSON log) lands in ``.devserver/``, which is git-ignored.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

# Two harnesses can run at once (see DEVSERVER_PORT at the bottom), and startup
# deletes the SQLite file — so the state is keyed by port. Sharing one file would
# mean the second launch wipes the first one's jobs out from under it.
PORT = os.getenv("DEVSERVER_PORT", "8000")
_SUFFIX = "" if PORT == "8000" else f"-{PORT}"

STATE_DIR = Path(__file__).resolve().parents[1] / ".devserver"
DB_PATH = STATE_DIR / f"devserver{_SUFFIX}.sqlite3"
LOG_PATH = STATE_DIR / "logs" / f"developer_ai{_SUFFIX}.log"

os.environ.setdefault("LOG_FILE", str(LOG_PATH))
os.environ.setdefault("LOG_LEVEL", "INFO")
os.environ.setdefault("STRATEGIC_MCP_URL", "http://127.0.0.1:9101/mcp")
os.environ.setdefault("UNITY_MCP_URL", "http://127.0.0.1:9102/mcp")
os.environ.setdefault("QA_MCP_URL", "http://127.0.0.1:9103/mcp")
os.environ.setdefault("ASSET_MCP_URL", "http://127.0.0.1:9104/mcp")
os.environ.setdefault("GIT_MCP_URL", "http://127.0.0.1:9105/mcp")

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.routers.games import router as games_router  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.graph.graph import build_graph  # noqa: E402
from app.mcp.clients import build_tool_clients  # noqa: E402
from app.models.orm import Base  # noqa: E402
from app.services.exceptions import GameJobNotFoundError, InvalidJobStateError  # noqa: E402
from app.services.game_service import GameService  # noqa: E402
from app.services.workflow_runner import WorkflowRunner  # noqa: E402
from app.utils.logging import configure_logging, get_logger  # noqa: E402

logger = get_logger("devserver")


class _StubPubSub:
    """Just the surface ``app/utils/events.py`` uses."""

    def __init__(self, hub: "_StubRedis") -> None:
        self._hub = hub
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._channels: set[str] = set()

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._hub.subscribers[channel].append(self._queue)

    async def listen(self) -> AsyncIterator[dict]:
        yield {"type": "subscribe", "data": 1}
        while True:
            yield {"type": "message", "data": await self._queue.get()}

    async def unsubscribe(self, channel: str) -> None:
        self._channels.discard(channel)
        queues = self._hub.subscribers.get(channel, [])
        if self._queue in queues:
            queues.remove(self._queue)

    async def aclose(self) -> None:
        for channel in list(self._channels):
            await self.unsubscribe(channel)


class _StubRedis:
    """In-process stand-in for Redis pub/sub."""

    def __init__(self) -> None:
        self.subscribers: dict[str, list[asyncio.Queue[str]]] = defaultdict(list)

    async def publish(self, channel: str, payload: str) -> None:
        for queue in list(self.subscribers.get(channel, [])):
            queue.put_nowait(payload)

    def pubsub(self) -> _StubPubSub:
        return _StubPubSub(self)

    async def aclose(self) -> None:
        self.subscribers.clear()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()

    app.state.api_key = None
    app.state.create_rate_limiter = None

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    tool_clients = build_tool_clients(settings)
    redis_client = _StubRedis()

    graph = build_graph(
        tool_clients,
        max_iterations=settings.development_qa_max_iterations,
        planning_max_iterations=settings.planning_max_iterations,
        concept_max_iterations=settings.concept_max_iterations,
        checkpointer=InMemorySaver(),
    )
    runner = WorkflowRunner(
        graph, session_factory, redis_client, max_concurrent_jobs=settings.max_concurrent_jobs
    )
    app.state.game_service = GameService(
        session_factory=session_factory, redis_client=redis_client, runner=runner
    )

    logger.info("devserver started", extra={"extra_fields": {"db": str(DB_PATH)}})
    try:
        yield
    finally:
        await tool_clients.aclose()
        await engine.dispose()


app = FastAPI(title="DeveloperAI (dev harness)", lifespan=lifespan)
# Without this the Web front-end cannot talk to the harness at all: the browser
# sends a CORS preflight, FastAPI has no OPTIONS route, and every request dies as
# 405 before it reaches a handler. Production (``app/main.py``) has always had
# this middleware; the harness did not, which quietly made "see the front-end run
# end-to-end locally" — this file's stated purpose — impossible.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(games_router)


@app.exception_handler(GameJobNotFoundError)
async def _not_found(request: Request, exc: GameJobNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvalidJobStateError)
async def _bad_state(request: Request, exc: InvalidJobStateError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


if __name__ == "__main__":
    DB_PATH.unlink(missing_ok=True)
    # DEVSERVER_PORT lets this run beside an already-bound 8000 (e.g. a second
    # harness pointed at mock servers on offset ports). The Web front-end reads
    # the same address from VITE_API_BASE_URL.
    uvicorn.run(app, host="127.0.0.1", port=int(PORT), log_level="warning")
