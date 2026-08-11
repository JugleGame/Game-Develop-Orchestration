"""Shared pytest fixtures: a file-backed SQLite job store, and helpers for
driving MCP clients against in-process FastMCP servers (no sockets)."""

import os
import tempfile

# ``Settings`` reads the repository-root .env (app/config/settings.py). A
# developer's local file must not decide what the suite asserts, so opt out
# before anything imports it.
os.environ["GDAI_SKIP_DOTENV"] = "1"

from collections.abc import AsyncIterator, Callable  # noqa: E402
from contextlib import AbstractAsyncContextManager, asynccontextmanager  # noqa: E402

import pytest  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.server.mcpserver import MCPServer as FastMCP  # noqa: E402
from mcp import Client  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models.orm import Base  # noqa: E402


@pytest.fixture
async def session_factory():
    """A fresh, file-backed SQLite DB per test, in WAL mode.

    ``sqlite+aiosqlite:///:memory:`` was tried first and rejected: plain
    ``:memory:`` gives each new connection its own private, empty database —
    GameService genuinely runs concurrent sessions (a background escalation
    task alongside a polling read), and two sessions landing on different
    connections never see each other's writes. ``poolclass=StaticPool``
    (forcing one shared physical connection) was tried next and is worse: two
    genuinely concurrent sessions then share *one* sqlite3 transaction, and a
    commit from one can silently vanish when the other's session closes and
    rolls back. A shared-cache ``:memory:`` URI fixed the visibility problem
    but reintroduced it as an intermittent ``sqlite3.OperationalError:
    database table is locked`` under real load — shared-cache mode raises
    ``SQLITE_LOCKED``, which sqlite3's ``busy_timeout`` does not retry (only
    ``SQLITE_BUSY`` is retried).

    All three are variations on the same root cause: in-memory SQLite has no
    real WAL/MVCC story for concurrent connections. A real file with
    ``journal_mode=WAL`` does — readers and a writer stop blocking each
    other — and it is what production Postgres behaves like. Caught via
    ``test_unexpected_workflow_crash_marks_job_escalated``: the background
    task's write committed with no error, but the test's read never saw it.
    """

    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)  # SQLAlchemy creates it; mkstemp only reserves the name

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory

    await engine.dispose()
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass


def mcp_session_factory(server: FastMCP) -> Callable[[], AbstractAsyncContextManager[ClientSession]]:
    """Build a ``session_factory`` that connects a client to ``server`` in-process.

    Uses the MCP SDK's in-memory transport, so tests exercise the real protocol
    (initialize handshake, tools/call, isError) without binding a port.
    """

    @asynccontextmanager
    async def _factory() -> AsyncIterator[ClientSession]:
        async with Client(server) as session:
            yield session

    return _factory
