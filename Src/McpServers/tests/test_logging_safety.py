"""Exercise every tool with INFO logging actually enabled.

Why this file exists: ``common/server.py::build()`` calls
``logging.basicConfig(level="INFO")``, but under pytest the root logger
*already* has handlers (pytest installs its own), and ``basicConfig`` is a
no-op in that case. Root therefore stays at WARNING, ``logger.info()`` is
disabled, and ``Logger.makeRecord`` — where ``extra`` is merged — never runs.

The consequence, observed for real: ``extra={"created": ...}`` in
``init_repo`` collided with ``LogRecord.created`` (a reserved attribute
holding the record timestamp) and raised ``KeyError`` inside the logging
call. In production that made ``git_init`` fail outright, returning an empty
``structuredContent``; every other test in this suite still passed because
INFO logging was silently disabled.

So these tests force the level on, which is the only way a reserved-key
collision in any ``extra`` payload becomes visible to the suite.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession
from mcp import Client

from gitmcp.server import mcp as git_mcp
from qa import judge
from qa.server import mcp as qa_mcp

_GAME_DESIGN = {
    "game_id": "g-log",
    "genre": "platformer",
    "core_mechanics": ["jump"],
    "art_style": "pixel art",
    "target_platform": "PC",
    "structure_overview": "one level",
    "created_at": "2026-07-15T00:00:00Z",
}


@pytest.fixture
def info_logging():
    """Enable INFO on the server loggers regardless of pytest's root level.

    Setting the level on the loggers themselves (not root) is what makes
    ``isEnabledFor(INFO)`` true, and therefore what makes ``makeRecord`` run.
    """

    names = ("QaMcpServer", "QaMcpServer.judge", "GitMcpServer", "GitMcpServer.repo")
    previous = {name: logging.getLogger(name).level for name in names}
    for name in names:
        logging.getLogger(name).setLevel(logging.INFO)
    yield
    for name, level in previous.items():
        logging.getLogger(name).setLevel(level)


@asynccontextmanager
async def _session(server) -> AsyncIterator[ClientSession]:
    async with Client(server) as client:
        yield client


# --------------------------------------------------------------------------
# GitMcpServer
# --------------------------------------------------------------------------


async def test_every_git_tool_logs_without_a_reserved_key_collision(info_logging):
    repo_name = "t-logsafe"

    async with _session(git_mcp) as client:
        init = await client.call_tool("git_init", {"repoName": repo_name})
        # The regression: this came back isError with structuredContent None.
        assert init.is_error is False, init.content
        assert init.structured_content == {"repoName": repo_name, "created": True}

        for tool, args in [
            ("git_branch", {"branch": "main", "repoName": repo_name}),
            ("git_pull", {"branch": "main", "repoName": repo_name}),
            ("git_commit", {"branch": "main", "message": "m", "repoName": repo_name}),
            ("git_push", {"branch": "main", "repoName": repo_name}),
            ("git_tag", {"tag": "v0", "repoName": repo_name}),
            ("git_status", {"repoName": repo_name}),
        ]:
            result = await client.call_tool(tool, args)
            assert result.is_error is False, (tool, result.content)
            assert result.structured_content is not None, tool

        # Reuse path logs the same record with newly_created=False.
        again = await client.call_tool("git_init", {"repoName": repo_name})
        assert again.is_error is False
        assert again.structured_content == {"repoName": repo_name, "created": False}


async def test_git_failure_path_logs_without_a_reserved_key_collision(info_logging):
    """``_log_failure`` builds its own ``extra``; it must be safe too."""

    async with _session(git_mcp) as client:
        result = await client.call_tool("git_branch", {"branch": "main", "repoName": "t-nope-log"})

    assert result.is_error is True
    # A KeyError from logging would replace the §03 body with a logging error.
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# QaMcpServer
# --------------------------------------------------------------------------


async def test_every_qa_tool_logs_without_a_reserved_key_collision(info_logging, monkeypatch):
    replies = {
        "establish_qa_policy": {
            "test_cases": [{"case_id": "t-1", "description": "d", "expected_result": "e"}],
            "acceptance_criteria": ["a"],
        },
        "verify_prototype_structure": {"match": True, "missing": []},
        "compare_structure": {"match": True, "missing": []},
        "run_functional_verification": {"result": "PASS"},
        "generate_error_report": {
            "errorReport": {
                "error_type": "runtime",
                "message": "No errors detected.",
                "file": None,
                "line": None,
                "suggested_fix": "None required.",
                "related_feature_id": "",
            }
        },
    }
    current: dict[str, dict] = {"reply": {}}
    usage = {
        "model": "claude-sonnet-5",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    async def fake(
        self,
        instructions: str,
        schema: dict,
        payload: dict,
        *,
        shared_context: dict | None = None,
    ):
        return current["reply"], dict(usage)

    monkeypatch.setattr(judge._Judge, "ask", fake)

    policy = {"test_cases": [], "acceptance_criteria": []}
    calls = [
        ("establish_qa_policy", {"gameDesign": _GAME_DESIGN}),
        ("verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}),
        ("compare_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}),
        (
            "run_functional_verification",
            {"gameDesign": _GAME_DESIGN, "build": "{}", "qaPolicy": policy},
        ),
        ("generate_error_report", {"gameDesign": _GAME_DESIGN, "build": "{}", "logs": "x"}),
    ]

    async with _session(qa_mcp) as client:
        for tool, args in calls:
            current["reply"] = replies[tool]
            result = await client.call_tool(tool, args)
            assert result.is_error is False, (tool, result.content)
            assert result.structured_content is not None, tool


async def test_qa_failure_path_logs_without_a_reserved_key_collision(info_logging):
    async with _session(qa_mcp) as client:
        result = await client.call_tool("establish_qa_policy", {"gameDesign": {}})

    assert result.is_error is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


async def test_qa_llm_success_log_is_safe(info_logging, monkeypatch):
    """``_Judge.ask``'s own success record carries model/timing/size keys."""

    class _Block:
        type = "text"
        text = '{"match": true, "missing": []}'

    class _Usage:
        input_tokens = 7
        output_tokens = 3
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0

    class _Response:
        content = [_Block()]
        stop_reason = "end_turn"
        stop_details = None
        usage = _Usage()

    class _Messages:
        async def create(self, **kwargs):
            return _Response()

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(judge._Judge, "_ensure_client", lambda self: _Client())

    async with _session(qa_mcp) as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.is_error is False, result.content
    assert result.structured_content["match"] is True
    # The usage record is built and logged on this path too.
    assert result.structured_content["usage"]["input_tokens"] == 7
