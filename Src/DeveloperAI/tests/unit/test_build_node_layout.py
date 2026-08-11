"""BuildPrototype records the project layout, and QA is shown it.

Why this is worth pinning. A Unity build succeeds whether or not a single
script is attached to a GameObject — the compiler does not care, and neither
the build result nor the playmode log mentions it. So the structure judge was
being asked "is this prototype assembled?" while holding only build metadata,
and it answered the only question it could: mechanic coverage. A prototype
whose scripts were attached to nothing reached QA PASS that way
(``Doc/설계/06_코드생성_아키텍처_진단_260730.md`` §2.1).

Two properties keep that from coming back:

* BuildPrototype asks for the layout and stores it.
* ``build_reference`` puts it in front of QA.

And one property keeps the fix from causing a new failure: a layout inspection
that breaks must not fail the build.
"""

from typing import Any

import pytest

from app.graph.nodes.build import build_build_prototype_node
from app.graph.nodes.qa_common import build_reference
from app.graph.state import Stage

_STATE: dict[str, Any] = {"game_id": "game-a"}

_LAYOUT: dict[str, Any] = {
    "gameId": "game-a",
    "counts": {"scripts": 6, "monoBehaviours": 10, "prefabs": 0, "scenes": 2},
    "ok": False,
    "findings": [
        {
            "rule": "L2",
            "severity": "FAIL",
            "target": "Scripts/Spec001.cs",
            "message": "MonoBehaviour(Spec001) 가 어떤 씬·프리팹에도 붙어 있지 않다.",
        }
    ],
}


class _FakeUnityClient:
    """Records calls. ``layout_error`` makes the inspection raise."""

    def __init__(self, *, layout_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._layout_error = layout_error

    async def build_project(self, *, game_id: str) -> dict[str, Any]:
        self.calls.append("build_project")
        return {"buildId": "b-1", "projectPath": "/unity/AutoGenProject"}

    async def inspect_project_layout(self, *, game_id: str) -> dict[str, Any]:
        self.calls.append("inspect_project_layout")
        if self._layout_error is not None:
            raise self._layout_error
        return _LAYOUT


async def test_build_node_records_the_project_layout():
    client = _FakeUnityClient()
    node = build_build_prototype_node(client)

    update = await node(dict(_STATE))

    assert client.calls == ["build_project", "inspect_project_layout"]
    assert update["current_stage"] == Stage.BUILD_PROTOTYPE
    assert update["build_result"]["buildId"] == "b-1"
    assert update["project_layout"] == _LAYOUT


async def test_layout_failure_does_not_fail_the_build():
    """빌드가 산출물이고 레이아웃 보고는 그에 대한 보고다 — 보고가 깨졌다고
    산출물을 버리지 않는다."""

    client = _FakeUnityClient(layout_error=RuntimeError("Unity project path not set"))
    node = build_build_prototype_node(client)

    update = await node(dict(_STATE))

    assert update["build_result"]["buildId"] == "b-1"
    # None 은 "재보지 않았다" 는 뜻이다. 빈 보고서로 대신 채우면 판정자에게
    # "봤는데 깨끗하다" 는 정반대 사실을 말하는 셈이 된다.
    assert update["project_layout"] is None


def test_build_reference_shows_the_layout_to_qa():
    reference = build_reference(
        {"build_result": {"buildId": "b-1"}, "project_layout": _LAYOUT}
    )

    assert "projectLayout" in reference
    assert "Spec001" in reference, "판정자가 어느 스크립트가 안 붙었는지 볼 수 있어야 한다"


def test_build_reference_omits_the_layout_when_it_was_not_measured():
    reference = build_reference({"build_result": {"buildId": "b-1"}, "project_layout": None})

    assert "projectLayout" not in reference


@pytest.mark.parametrize("missing_key", ["build_result", "project_layout"])
def test_build_reference_tolerates_an_early_state(missing_key: str):
    """QA 이전 단계에서 중단된 상태로도 참조 문자열은 만들어져야 한다."""

    state: dict[str, Any] = {"build_result": {"buildId": "b-1"}, "project_layout": _LAYOUT}
    state.pop(missing_key)

    assert build_reference(state)
