"""애니메이션 도구 검증 — 실행 중인 Unity Editor 없이 도는 부분만 (Issue #28).

Unity 안에서 실제로 클립이 만들어지는지는 운영 문서의 수동 절차가 확인한다.
여기서는 두 가지를 고정한다: 잘못된 요청이 Unity 에 **도달하기 전에** 막히는가,
그리고 통과한 요청이 만들어 보내는 C# 이 요청한 내용을 그대로 담는가.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("UNITY_PROJECT_PATH", "C:/nonexistent-unity-project")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from unity import server as unity_server  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "unity-editor"


@pytest.fixture
def recorded(monkeypatch):
    """Unity 로 나가는 C# 을 가로채, 도달 여부와 내용을 함께 본다."""

    calls: list[str] = []

    async def _fake_run(code: str, title: str, timeout: float):
        calls.append(code)
        return {
            "success": True,
            "clip": "Assets/Animations/PlayerRun.anim",
            "frameCount": 2,
            "frameRate": 12.0,
            "loop": True,
            "length": 0.166,
            "controller": "Assets/Animations/Player.controller",
            "states": ["Idle", "Run"],
            "parameters": ["Speed"],
            "transitions": 1,
            "boundToPrefab": True,
            "missing": [],
        }

    monkeypatch.setattr(unity_server, "_run_command", _fake_run)
    return calls


# ---------------------------------------------------------------------------
# 계약
# ---------------------------------------------------------------------------
def test_exposes_the_animation_tools():
    names = {tool.name for tool in unity_server.mcp._tool_manager.list_tools()}

    assert {"create_animation_clip", "create_animator_controller", "inspect_animator"} <= names


@pytest.mark.parametrize(
    ("tool_name", "required"),
    [
        ("create_animation_clip", {"gameId", "clipName", "framePaths"}),
        ("create_animator_controller", {"gameId", "controllerName", "states"}),
        ("inspect_animator", {"gameId", "target"}),
    ],
)
def test_animation_tools_use_camel_case_argument_names(tool_name: str, required: set[str]):
    tool = next(t for t in unity_server.mcp._tool_manager.list_tools() if t.name == tool_name)

    assert required <= set(tool.parameters["properties"])


# ---------------------------------------------------------------------------
# create_animation_clip
# ---------------------------------------------------------------------------
async def test_clip_keeps_the_requested_frame_order(recorded):
    result = await unity_server.create_animation_clip(
        gameId="sanabi",
        clipName="PlayerRun",
        framePaths=["Assets/Generated/run_00.png", "Assets/Generated/run_01.png"],
        framesPerSecond=12.0,
    )

    assert result["clip"] == "Assets/Animations/PlayerRun.anim"
    assert result["frameCount"] == 2
    code = recorded[0]
    assert code.index("run_00.png") < code.index("run_01.png")


async def test_clip_rejects_an_empty_frame_list(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_animation_clip(
            gameId="sanabi", clipName="PlayerRun", framePaths=[]
        )

    assert recorded == [], "빈 요청이 Unity 까지 가면 안 된다"


async def test_clip_rejects_an_impossible_frame_rate(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_animation_clip(
            gameId="sanabi",
            clipName="PlayerRun",
            framePaths=["Assets/Generated/run_00.png"],
            framesPerSecond=0.0,
        )

    assert recorded == []


async def test_clip_reports_frames_that_are_not_sprites(monkeypatch):
    async def _fake_run(code: str, title: str, timeout: float):
        return {"success": False, "missing": ["Assets/Generated/run_00.png"]}

    monkeypatch.setattr(unity_server, "_run_command", _fake_run)

    with pytest.raises(ToolError):
        await unity_server.create_animation_clip(
            gameId="sanabi",
            clipName="PlayerRun",
            framePaths=["Assets/Generated/run_00.png"],
        )


# ---------------------------------------------------------------------------
# create_animator_controller
# ---------------------------------------------------------------------------
async def test_controller_binds_states_parameters_and_prefab(recorded):
    result = await unity_server.create_animator_controller(
        gameId="sanabi",
        controllerName="Player",
        states=[
            {"name": "Idle", "clip": "Assets/Animations/PlayerIdle.anim"},
            {"name": "Run", "clip": "Assets/Animations/PlayerRun.anim"},
        ],
        parameters=[{"name": "Speed", "type": "Float"}],
        transitions=[
            {
                "from": "Idle",
                "to": "Run",
                "conditions": [{"parameter": "Speed", "mode": "Greater", "threshold": 0.1}],
            }
        ],
        defaultState="Idle",
        targetPrefab="Assets/Prefabs/Player.prefab",
    )

    assert result["states"] == ["Idle", "Run"]
    assert result["parameters"] == [{"name": "Speed", "type": "Float"}]
    assert result["boundToPrefab"] is True
    assert "Assets/Prefabs/Player.prefab" in recorded[0]


async def test_controller_rejects_a_condition_on_an_undeclared_parameter(recorded):
    """Unity 는 이런 전환을 조용히 무시한다 — 통과시키면 런타임에서 찾아야 한다."""

    with pytest.raises(ToolError):
        await unity_server.create_animator_controller(
            gameId="sanabi",
            controllerName="Player",
            states=[{"name": "Idle", "clip": ""}, {"name": "Run", "clip": ""}],
            parameters=[{"name": "Speed", "type": "Float"}],
            transitions=[
                {
                    "from": "Idle",
                    "to": "Run",
                    "conditions": [{"parameter": "Grounded", "mode": "If"}],
                }
            ],
        )

    assert recorded == []


async def test_controller_rejects_an_unknown_parameter_type(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_animator_controller(
            gameId="sanabi",
            controllerName="Player",
            states=[{"name": "Idle", "clip": ""}],
            parameters=[{"name": "Speed", "type": "Double"}],
        )

    assert recorded == []


async def test_controller_rejects_a_default_state_that_does_not_exist(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_animator_controller(
            gameId="sanabi",
            controllerName="Player",
            states=[{"name": "Idle", "clip": ""}],
            defaultState="Run",
        )

    assert recorded == []


async def test_controller_rejects_a_transition_to_a_missing_state(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_animator_controller(
            gameId="sanabi",
            controllerName="Player",
            states=[{"name": "Idle", "clip": ""}],
            transitions=[{"from": "Idle", "to": "Run"}],
        )

    assert recorded == []


# ---------------------------------------------------------------------------
# inspect_animator
# ---------------------------------------------------------------------------
async def test_inspect_splits_the_packed_records(monkeypatch):
    async def _fake_run(code: str, title: str, timeout: float):
        return {
            "success": True,
            "target": "Assets/Prefabs/Player.prefab",
            "controller": "Assets/Animations/Player.controller",
            "hasAnimator": True,
            "states": ["Base Layer/Idle:PlayerIdle"],
            "parameters": ["Speed:Float"],
            "clips": ["PlayerIdle:4:0.33"],
        }

    monkeypatch.setattr(unity_server, "_run_command", _fake_run)

    result = await unity_server.inspect_animator(
        gameId="sanabi", target="Assets/Prefabs/Player.prefab"
    )

    assert result["hasController"] is True
    assert result["states"] == [{"state": "Base Layer/Idle", "motion": "PlayerIdle"}]
    assert result["parameters"] == [{"name": "Speed", "type": "Float"}]
    assert result["clips"] == [{"clip": "PlayerIdle", "frames": "4", "length": "0.33"}]


async def test_inspect_reports_an_animator_without_a_controller(monkeypatch):
    """이 검사가 존재하는 이유 자체 — 조용한 무동작을 눈에 보이게 만든다."""

    async def _fake_run(code: str, title: str, timeout: float):
        return {
            "success": False,
            "target": "Assets/Prefabs/Player.prefab",
            "controller": "",
            "hasAnimator": True,
            "states": [],
            "parameters": [],
            "clips": [],
        }

    monkeypatch.setattr(unity_server, "_run_command", _fake_run)

    result = await unity_server.inspect_animator(
        gameId="sanabi", target="Assets/Prefabs/Player.prefab"
    )

    assert result["hasAnimator"] is True
    assert result["hasController"] is False


# ---------------------------------------------------------------------------
# 사람용 에디터 도구
# ---------------------------------------------------------------------------
def test_editor_debug_window_template_ships_with_the_repository():
    source = (TEMPLATE / "AnimationDebugWindow.cs").read_text(encoding="utf-8")

    assert "class AnimationDebugWindow : EditorWindow" in source
    assert "[MenuItem(" in source
    # The three jobs the window exists for.
    assert "SetObjectReferenceCurve" in source, "프레임 폴더에서 클립을 만든다"
    assert "runtimeAnimatorController == null" in source, "조용한 Animator 를 찾아낸다"
    assert "SetTrigger" in source and "Application.isPlaying" in source, "PlayMode 파라미터 조작"
    # It is copied into a Unity project by hand, so it must not be installed here.
    assert not (TEMPLATE.parent.parent / "Assets").exists()


# ---------------------------------------------------------------------------
# create_prefab collider and pivot (Issue #36)
# ---------------------------------------------------------------------------
async def test_prefab_applies_collider_size_and_pivot(recorded):
    result = await unity_server.create_prefab(
        gameId="sanabi",
        prefabName="Player",
        components=["CapsuleCollider2D", "SpriteRenderer"],
        sprite="Assets/Generated/player.png",
        colliderSize=[1.0, 3.3],
        colliderOffset=[0.0, 1.65],
        spritePivot=[0.5, 0.03],
    )

    assert result["colliderSize"] == [1.0, 3.3]
    assert result["spritePivot"] == [0.5, 0.03]
    code = recorded[0]
    assert "bool hasColliderSize = true;" in code
    assert "Vector2(1.0f, 3.3f)" in code
    assert "Vector2(0.5f, 0.03f)" in code


async def test_prefab_without_measurements_keeps_the_old_shape(recorded):
    result = await unity_server.create_prefab(
        gameId="sanabi",
        prefabName="Player",
        components=["CapsuleCollider2D"],
    )

    assert result["colliderSize"] is None
    code = recorded[0]
    assert "bool hasColliderSize = false;" in code
    assert "bool hasPivot = false;" in code


async def test_prefab_rejects_a_collider_size_without_a_collider(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_prefab(
            gameId="sanabi",
            prefabName="Player",
            components=["SpriteRenderer"],
            colliderSize=[1.0, 3.3],
        )

    assert recorded == [], "a mistake this obvious must not reach Unity"


async def test_prefab_rejects_a_malformed_measurement(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_prefab(
            gameId="sanabi",
            prefabName="Player",
            components=["CapsuleCollider2D"],
            colliderSize=[1.0],
        )

    assert recorded == []


async def test_prefab_rejects_a_pivot_without_a_sprite(recorded):
    with pytest.raises(ToolError):
        await unity_server.create_prefab(
            gameId="sanabi",
            prefabName="Player",
            components=["SpriteRenderer"],
            spritePivot=[0.5, 0.03],
        )

    assert recorded == []
