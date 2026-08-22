"""콘솔에서 오류를 걷어오는 경로가 **오류를 놓치지 않는가.**

실측으로 드러난 결함이다 (2026-07-31). ``Unity_ReadConsole`` 에
``Types: ["Error"]`` 를 주면 **컴파일 오류가 0건으로 온다** — 이 브리지가 컴파일
오류를 ``Type: "Log"`` 로 실어 보내기 때문이다. 같은 순간 ``Types: ["All"]`` 로
읽으면 그 오류가 그대로 나온다.

``get_compile_errors`` 와 ``run_playmode_test`` 가 둘 다 그 필터를 쓰고 있었으므로,
**컴파일이 깨진 프로젝트가 "오류 0건"으로 보이고 플레이모드가 ``passed: true`` 를
냈다.** 재시도 루프는 고칠 것이 없다고 판단하고 QA 는 근거 없이 통과시킨다 —
``docs/contracts.md``가 금지하는 "증거 없이 합격을 선언하는" 경로가 이것이다.

아래 페이로드는 지어낸 것이 아니라 그 순간 Unity 가 실제로 돌려준 응답이다.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("UNITY_PROJECT_PATH", "C:/nonexistent-unity-project")

from unity import server as unity_server  # noqa: E402

#: 실측값. ``Type`` 이 "Log" 인데 내용은 컴파일 오류다 — 이 어긋남이 결함의 핵심.
_MISTYPED_COMPILE_ERROR = {
    "Message": (
        "Assets\\Scripts\\Systems\\SmokeClock.cs(2,19): error CS0234: The type or "
        "namespace name 'InputSystem' does not exist in the namespace 'UnityEngine' "
        "(are you missing an assembly reference?)\n"
    ),
    "Type": "Log",
    "File": "Assets\\Scripts\\Systems\\SmokeClock.cs",
    "Line": 2,
    "StackTrace": None,
}


@pytest.fixture
def fake_console(monkeypatch: pytest.MonkeyPatch):
    """``_call_unity`` 를 가로채 콘솔 응답을 흉내 낸다. 보낸 인자도 받아 둔다."""

    sent: dict[str, Any] = {}

    def install(entries: list[dict[str, Any]]) -> dict[str, Any]:
        async def _fake(tool: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
            sent["tool"] = tool
            sent["arguments"] = arguments
            return {"data": entries}

        monkeypatch.setattr(unity_server, "_call_unity", _fake)
        return sent

    return install


@pytest.mark.asyncio
async def test_console_is_read_with_all_types_not_error(fake_console) -> None:
    """``Types: ["Error"]`` 로 읽으면 컴파일 오류를 못 본다. 전부 읽어야 한다."""

    sent = fake_console([])
    await unity_server._read_error_console()

    assert sent["arguments"]["Types"] == ["All"]


@pytest.mark.asyncio
async def test_mistyped_compile_error_is_still_collected(fake_console) -> None:
    """``Type`` 이 "Log" 라도 메시지가 컴파일 오류면 오류다."""

    fake_console([_MISTYPED_COMPILE_ERROR])
    found = await unity_server._read_error_console()

    assert len(found) == 1


@pytest.mark.asyncio
async def test_get_compile_errors_reports_the_mistyped_error(fake_console) -> None:
    """도구 반환까지 이어지는지 본다 — 여기가 비면 재시도 루프가 눈을 감는다."""

    fake_console([_MISTYPED_COMPILE_ERROR])
    body = await unity_server.get_compile_errors(gameId="g1")

    assert len(body["errors"]) == 1
    assert body["errors"][0]["file"] == "Assets\\Scripts\\Systems\\SmokeClock.cs"
    assert body["errors"][0]["line"] == 2
    assert "CS0234" in body["errors"][0]["message"]


@pytest.mark.asyncio
async def test_compile_errors_default_to_a_bounded_sample(fake_console) -> None:
    fake_console([dict(_MISTYPED_COMPILE_ERROR, Line=index) for index in range(8)])

    compact = await unity_server.get_compile_errors(gameId="g1")
    detailed = await unity_server.get_compile_errors(gameId="g1", detail=True)

    assert compact["errorCount"] == 8
    assert len(compact["errors"]) == 5
    assert compact["errorsTruncated"] is True
    assert len(detailed["errors"]) == 8
    assert detailed["errorsTruncated"] is False


@pytest.mark.asyncio
async def test_playmode_does_not_pass_when_console_holds_an_error(fake_console) -> None:
    """오류가 있는데 ``passed: true`` 가 나오면 QA 가 근거 없이 통과한다."""

    fake_console([_MISTYPED_COMPILE_ERROR])
    errors = await unity_server._read_error_console()

    assert errors, "오류를 못 걷으면 run_playmode_test 가 passed=True 를 낸다"


@pytest.mark.asyncio
async def test_ordinary_logs_are_not_mistaken_for_errors(fake_console) -> None:
    """평범한 로그까지 오류로 세면 멀쩡한 빌드가 FAIL 이 된다."""

    fake_console(
        [
            {"Type": "Log", "Message": "Loaded 12 chunks"},
            {"Type": "Warning", "Message": "Shader fallback used"},
            # 낱말만으로 세면 이것까지 오류가 된다. 예외는 콜론이 따라붙는다.
            {"Type": "Log", "Message": "No Exception occurred during load"},
        ]
    )

    assert await unity_server._read_error_console() == []


@pytest.mark.asyncio
async def test_properly_typed_errors_are_still_collected(fake_console) -> None:
    """``Type`` 이 제대로 오는 경우도 그대로 걷어야 한다 — 브리지가 고쳐질 수 있다."""

    fake_console(
        [
            {"Type": "Error", "Message": "something broke"},
            {"Type": "Exception", "Message": "NullReferenceException: object is null"},
        ]
    )

    assert len(await unity_server._read_error_console()) == 2


@pytest.mark.asyncio
async def test_runtime_exception_logged_as_log_is_collected(fake_console) -> None:
    """런타임 예외도 ``Type`` 이 어긋난 채 올 수 있다."""

    fake_console([{"Type": "Log", "Message": "NullReferenceException: Object reference not set"}])

    assert len(await unity_server._read_error_console()) == 1
