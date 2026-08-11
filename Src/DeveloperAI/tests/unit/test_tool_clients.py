"""Unit tests for the MCP client layer: result parsing, retry, and error mapping.

Each test stands up a real in-process FastMCP server and drives the production
client against it through the SDK's in-memory transport, so the JSON-RPC
lifecycle (initialize, tools/call, isError) is genuinely exercised.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.server.mcpserver.exceptions import ToolError

from app.mcp.exceptions import ToolCallError, ToolErrorCode
from app.mcp.git_client import GitClient
from app.mcp.strategic_client import StrategicClient
from app.mcp.unity_client import UnityClient
from tests.conftest import mcp_session_factory

_GAME_DESIGN = {
    "game_id": "g1",
    "genre": "platformer",
    "core_mechanics": ["jump"],
    "art_style": "pixel",
    "target_platform": "PC",
    "structure_overview": "one level",
    "created_at": "2026-07-15T00:00:00Z",
}


def _strategic_client(server: FastMCP) -> StrategicClient:
    return StrategicClient(
        server_name="StrategicMcpServer",
        url="memory://strategic",
        timeout_seconds=5,
        max_retries=3,
        session_factory=mcp_session_factory(server),
    )


def _git_client(server: FastMCP) -> GitClient:
    return GitClient(
        server_name="GitMcpServer",
        url="memory://git",
        timeout_seconds=5,
        max_retries=3,
        session_factory=mcp_session_factory(server),
    )


async def test_generate_game_design_parses_structured_result():
    server = FastMCP("StrategicMcpServer")

    @server.tool()
    def generate_game_design(prompt: str) -> dict:
        return {
            "gameDesign": _GAME_DESIGN,
            "featurePrompts": [
                {
                    "feature_id": "f-1",
                    "title": "Jump",
                    "description": "Add jump ability",
                    "priority": "P0",
                    "dependencies": [],
                }
            ],
        }

    design, feature_prompts = await _strategic_client(server).generate_game_design(
        prompt="make a platformer"
    )

    assert design.genre == "platformer"
    assert [fp.feature_id for fp in feature_prompts] == ["f-1"]


async def test_tool_raising_maps_to_mcp_error():
    """A tool that raises comes back as an isError result, not a transport failure."""

    server = FastMCP("StrategicMcpServer")

    @server.tool()
    def generate_game_design(prompt: str) -> dict:
        raise RuntimeError("planning model unavailable")

    with pytest.raises(ToolCallError) as exc_info:
        await _strategic_client(server).generate_game_design(prompt="make a platformer")

    assert exc_info.value.code is ToolErrorCode.MCP_ERROR


async def test_unknown_tool_is_reported_as_an_error():
    """tools/call for a tool the server does not expose must not pass silently."""

    server = FastMCP("GitMcpServer")

    @server.tool()
    def git_status() -> dict:
        return {"clean": True}

    with pytest.raises(ToolCallError):
        await _git_client(server).git_pull(branch="main")


async def test_missing_required_argument_is_reported_as_an_error():
    server = FastMCP("GitMcpServer")

    @server.tool()
    def git_pull(branch: str) -> dict:
        return {"branch": branch}

    with pytest.raises(ToolCallError):
        await _git_client(server).call_tool("git_pull", {})


async def test_git_pull_calls_expected_tool_with_branch_payload():
    server = FastMCP("GitMcpServer")
    seen: list[str] = []

    @server.tool()
    def git_pull(branch: str) -> dict:
        seen.append(branch)
        return {"branch": branch, "updated": False}

    body = await _git_client(server).git_pull(branch="main")

    assert seen == ["main"]
    assert body["branch"] == "main"


async def test_git_init_is_called_with_computed_repo_name():
    server = FastMCP("GitMcpServer")
    seen: list[str] = []

    @server.tool()
    def git_init(repoName: str) -> dict:
        seen.append(repoName)
        return {"repoName": repoName}

    await _git_client(server).git_init(repo_name="platformer-jump-quest-3f9a1c2e")

    assert seen == ["platformer-jump-quest-3f9a1c2e"]


async def test_git_commit_returns_commit_hash():
    server = FastMCP("GitMcpServer")

    @server.tool()
    def git_commit(branch: str, message: str) -> dict:
        return {"commit": "abc123", "message": message}

    commit = await _git_client(server).git_commit(branch="main", message="[AutoGen] test")

    assert commit == "abc123"


async def test_read_timeout_maps_to_timeout_code():
    """ClientSession raises McpError(408) on a read timeout, not an httpx
    timeout — it must still surface as TIMEOUT (2000), per §03 §4."""

    server = FastMCP("GitMcpServer")

    @server.tool()
    async def git_pull(branch: str) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"branch": branch}

    client = GitClient(
        server_name="GitMcpServer",
        url="memory://git",
        timeout_seconds=0.2,
        max_retries=1,
        session_factory=mcp_session_factory(server),
    )

    with pytest.raises(ToolCallError) as exc_info:
        await client.git_pull(branch="main")

    assert exc_info.value.code is ToolErrorCode.TIMEOUT


async def test_read_timeout_is_retried_for_idempotent_tools():
    """A timeout must consume the retry budget, not fail on the first attempt."""

    server = FastMCP("GitMcpServer")
    attempts: list[int] = []

    @server.tool()
    async def git_pull(branch: str) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) < 2:
            await asyncio.sleep(5)
        return {"branch": branch}

    client = GitClient(
        server_name="GitMcpServer",
        url="memory://git",
        timeout_seconds=0.3,
        max_retries=3,
        session_factory=mcp_session_factory(server),
    )

    body = await client.git_pull(branch="main")

    assert body["branch"] == "main"
    assert len(attempts) == 2


async def test_error_code_survives_fastmcp_message_prefix():
    """FastMCP renders a raised tool as "Error executing tool X: <msg>", so a
    JSON error body is not itself valid JSON. The §03 errorCode channel must
    still work — otherwise UNITY_BUILD_ERROR (4000) is unreachable."""

    server = FastMCP("UnityMcpServer")

    @server.tool()
    def build_project(gameId: str) -> dict[str, Any]:
        raise ToolError(json.dumps({"errorCode": 4000, "message": "Unity build failed: CS0103"}))

    client = UnityClient(
        server_name="UnityMcpServer",
        url="memory://unity",
        timeout_seconds=5,
        max_retries=1,
        session_factory=mcp_session_factory(server),
    )

    with pytest.raises(ToolCallError) as exc_info:
        await client.build_project(game_id="g1")

    assert exc_info.value.code is ToolErrorCode.UNITY_BUILD_ERROR
    assert "CS0103" in str(exc_info.value)


async def test_plain_exception_still_maps_to_mcp_error():
    """A server that raises without a JSON body keeps the generic 3000 code."""

    server = FastMCP("UnityMcpServer")

    @server.tool()
    def build_project(gameId: str) -> dict[str, Any]:
        raise RuntimeError("editor crashed")

    client = UnityClient(
        server_name="UnityMcpServer",
        url="memory://unity",
        timeout_seconds=5,
        max_retries=1,
        session_factory=mcp_session_factory(server),
    )

    with pytest.raises(ToolCallError) as exc_info:
        await client.build_project(game_id="g1")

    assert exc_info.value.code is ToolErrorCode.MCP_ERROR


async def test_structured_content_is_used_when_server_declares_return_type():
    """A tool annotated ``-> dict[str, Any]`` populates structuredContent (the
    typed channel); a bare ``-> dict`` only produces JSON text."""

    server = FastMCP("GitMcpServer")

    @server.tool()
    def git_commit(branch: str, message: str) -> dict[str, Any]:
        return {"commit": "abc123", "changed": True}

    commit = await _git_client(server).git_commit(branch="main", message="m")

    assert commit == "abc123"


def _failing_session_factory(attempts: list[int], fail_times: int, server: FastMCP):
    """Session factory whose connection fails ``fail_times`` before succeeding."""

    working = mcp_session_factory(server)

    @asynccontextmanager
    async def _factory():
        attempts.append(1)
        if len(attempts) <= fail_times:
            raise ConnectionError("connection reset before session established")
        async with working() as session:
            yield session

    return _factory


async def test_transport_failure_on_safe_tool_is_retried():
    """An idempotent tool keeps the full retry budget across session setup failures."""

    server = FastMCP("GitMcpServer")

    @server.tool()
    def git_pull(branch: str) -> dict:
        return {"branch": branch}

    attempts: list[int] = []
    client = GitClient(
        server_name="GitMcpServer",
        url="memory://git",
        timeout_seconds=5,
        max_retries=3,
        session_factory=_failing_session_factory(attempts, 2, server),
    )

    body = await client.git_pull(branch="main")

    assert body["branch"] == "main"
    assert len(attempts) == 3


async def test_transport_failure_on_git_commit_is_never_retried():
    """History-mutating Git tools get exactly one attempt: a response lost after
    the server committed must not be replayed into a duplicate commit."""

    server = FastMCP("GitMcpServer")

    @server.tool()
    def git_commit(branch: str, message: str) -> dict:
        return {"commit": "abc123"}

    attempts: list[int] = []
    client = GitClient(
        server_name="GitMcpServer",
        url="memory://git",
        timeout_seconds=5,
        max_retries=3,
        session_factory=_failing_session_factory(attempts, 99, server),
    )

    with pytest.raises(ToolCallError) as exc_info:
        await client.git_commit(branch="main", message="[AutoGen] test")

    assert len(attempts) == 1
    assert exc_info.value.code is ToolErrorCode.MCP_ERROR
