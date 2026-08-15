"""Named Unity test execution (Issue #34).

Unity is not started here. What is pinned: the filter reaches the runner, a run that
matches nothing is not reported as a pass, a missing reporter is named as such, and a
run that never finishes is a timeout rather than a silent success.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("UNITY_PROJECT_PATH", "C:/nonexistent-unity-project")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from unity import server as unity_server  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(unity_server, "_TEST_POLL_SECONDS", 0.0)


def _runner(monkeypatch, polls: list[dict], calls: list[str] | None = None):
    """Answer the start command, then hand back the queued poll results."""

    remaining = list(polls)

    async def _fake_run(code: str, title: str, timeout: float):
        if calls is not None:
            calls.append(code)
        if "EditorPrefs.SetString" in code and "TestRunnerApi" in code:
            return {"success": True, "mode": "PlayMode", "requested": 1}
        return remaining.pop(0) if remaining else {"success": True, "status": "running"}

    monkeypatch.setattr(unity_server, "_run_command", _fake_run)


async def test_named_run_reports_each_test(monkeypatch):
    calls: list[str] = []
    _runner(
        monkeypatch,
        [
            {"success": True, "status": "running", "count": 0, "results": []},
            {
                "success": True,
                "status": "completed",
                "count": 2,
                "results": [
                    {"name": "Test_Player_NoDoubleJump", "status": "Passed", "message": ""},
                    {"name": "Test_Player_MoveAndJump", "status": "Passed", "message": ""},
                ],
            },
        ],
        calls,
    )

    result = await unity_server.run_named_tests(
        gameId="sanabi",
        testNames=["Test_Player_NoDoubleJump", "Test_Player_MoveAndJump"],
        mode="PlayMode",
    )

    assert result["passed"] is True
    assert result["testCount"] == 2
    assert result["failedCount"] == 0
    assert "Test_Player_NoDoubleJump" in calls[0], "the filter must reach the runner"
    assert "TestMode.PlayMode" in calls[0]


async def test_named_run_accepts_unity_full_test_names(monkeypatch):
    calls: list[str] = []
    full_name = "Game.Gameplay.Tests.GameFlowPlayModeTests.Test_Flow_SingleActiveState"
    _runner(
        monkeypatch,
        [
            {
                "success": True,
                "status": "completed",
                "count": 1,
                "results": [{"name": full_name, "status": "Passed", "message": ""}],
            }
        ],
        calls,
    )

    result = await unity_server.run_named_tests(
        gameId="sanabi",
        testNames=[full_name],
        mode="PlayMode",
    )

    assert result["passed"] is True
    assert full_name in calls[0]


async def test_a_failing_test_is_reported_with_its_message(monkeypatch):
    _runner(
        monkeypatch,
        [
            {
                "success": True,
                "status": "completed",
                "count": 1,
                "results": [
                    {
                        "name": "Test_Player_NoDoubleJump",
                        "status": "Failed",
                        "message": "Expected: 1 jump  But was: 4",
                    }
                ],
            }
        ],
    )

    result = await unity_server.run_named_tests(
        gameId="sanabi", testNames=["Test_Player_NoDoubleJump"]
    )

    assert result["passed"] is False
    assert result["failedCount"] == 1
    assert "Expected: 1 jump" in result["failures"][0]["message"]


async def test_a_filter_that_matches_nothing_is_not_a_pass(monkeypatch):
    """Otherwise an acceptance criterion gets ticked with no test behind it."""

    _runner(monkeypatch, [{"success": True, "status": "completed", "count": 0, "results": []}])

    with pytest.raises(ToolError):
        await unity_server.run_named_tests(gameId="sanabi", testNames=["Test_Does_Not_Exist"])


async def test_a_missing_reporter_is_named(monkeypatch):
    _runner(monkeypatch, [{"success": True, "status": "absent", "count": 0, "results": []}])

    with pytest.raises(ToolError):
        await unity_server.run_named_tests(gameId="sanabi", testNames=["Test_Player_MoveAndJump"])


async def test_a_run_that_never_finishes_is_a_timeout(monkeypatch):
    _runner(monkeypatch, [{"success": True, "status": "running", "count": 0, "results": []}] * 5)

    result = await unity_server.run_named_tests(
        gameId="sanabi", testNames=["Test_Player_MoveAndJump"], timeoutSeconds=0.05
    )

    assert result["status"] == "timeout"
    assert result["passed"] is False


async def test_mode_must_be_one_unity_accepts(monkeypatch):
    _runner(monkeypatch, [])

    with pytest.raises(ToolError):
        await unity_server.run_named_tests(gameId="sanabi", mode="Whenever")


async def test_test_names_are_checked_before_they_reach_the_source(monkeypatch):
    calls: list[str] = []
    _runner(monkeypatch, [], calls)

    with pytest.raises(ToolError):
        await unity_server.run_named_tests(gameId="sanabi", testNames=['"; Bad()'])

    assert calls == []
