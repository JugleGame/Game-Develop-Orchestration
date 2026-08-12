"""조립 3종이 만드는 C# 이 ``Unity_RunCommand`` 의 계약을 지키는가.

**왜 문자열 검사인가.** 이 C# 이 맞는지 확인하는 유일한 진짜 방법은 Unity Editor
를 띄우는 것인데, CI 에는 Editor 가 없다. 그렇다고 검사를 포기하면 이 저장소가
이미 두 번 겪은 실패가 그대로 재발한다 — ``build_project`` 의 C# 이 클래스명
하나(``AutoGenBuild`` vs ``CommandScript``) 때문에 **한 번도 동작하지 않았고**,
증상이 로그도 없는 "No logs available" 이라 원인을 찾는 데 오래 걸렸다
(``docs/backlog.md``의 Unity 실동작 항목).

그 실패들은 전부 **소스 텍스트만 보면 알 수 있는 것들**이다. Unity 가 이 코드를
컴파일 이전에 텍스트로 검사해 거부하기 때문이다. 그래서 여기서도 텍스트로 본다.

빌드 명령도 같은 계약을 쓰므로 함께 고정한다 — 백로그의 「`build_project` 를
고쳤다 — 계약을 지키는지 보는 검사가 필요하다」 항목이 요구한 것이 이 테스트다.
"""

from __future__ import annotations

import os
import re

import pytest

os.environ.setdefault("UNITY_PROJECT_PATH", "C:/nonexistent-unity-project")

from unity import assembly  # noqa: E402
from unity import server as unity_server  # noqa: E402
from unity.assembly import AssemblyError  # noqa: E402

_SCENE_OBJECTS = [
    {"name": "WorldRoot", "parent": "", "components": ["ChunkLoader"], "prefab": ""},
    {
        "name": "Grid",
        "parent": "WorldRoot",
        "components": ["Grid", "Tilemap"],
        "prefab": "",
    },
    {
        "name": "Player",
        "parent": "",
        "components": [],
        "prefab": "Assets/Prefabs/Player.prefab",
    },
]


def _every_command() -> dict[str, str]:
    """세 조립 명령 + 빌드 명령. 계약은 넷 모두에 똑같이 적용된다."""

    return {
        "create_prefab": assembly.prefab_command(
            "Enemy", "Assets/Prefabs/Enemy.prefab", ["EnemyBrain", "Rigidbody2D"], ""
        ),
        "compose_scene": assembly.scene_command("Assets/Scenes/Main.unity", _SCENE_OBJECTS),
        "bind_reference_prefab": assembly.bind_command(
            "Assets/Prefabs/Enemy.prefab", "EnemyBrain", "portrait", "Assets/Generated/e.png", ""
        ),
        "bind_reference_scene": assembly.bind_command(
            "WorldRoot/Grid", "", "tileSprite", "Assets/Generated/t.png", "Assets/Scenes/Main.unity"
        ),
        "build_project": unity_server._BUILD_CSHARP.replace(
            "__TARGET__", "StandaloneWindows64"
        ).replace("__OUTPUT__", "Builds/g/game.exe"),
    }


# ---------------------------------------------------------------------------
# Unity_RunCommand 계약
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(_every_command()))
def test_command_declares_the_class_unity_requires(name: str):
    """``internal class CommandScript : IRunCommand`` 가 아니면 로그 없이 죽는다."""

    source = _every_command()[name]

    for rule in assembly.RUN_COMMAND_RULES:
        assert rule in source, f"{name} 이 '{rule}' 를 지키지 않는다"


@pytest.mark.parametrize("name", sorted(_every_command()))
def test_command_avoids_statically_blocked_tokens(name: str):
    """소스에 글자로 있기만 해도 Unity 가 거부하는 것들."""

    source = _every_command()[name]

    for token in assembly.FORBIDDEN_SOURCE_TOKENS:
        assert token not in source, f"{name} 에 정적 차단 대상 '{token}' 이 있다"


@pytest.mark.parametrize("name", sorted(_every_command()))
def test_editor_types_are_globally_qualified(name: str):
    """Unity 가 이 코드를 ``Unity.AI...Editor`` 로 감싸므로 ``global::`` 이 필요하다.

    이것도 두 번 걸렸다 — ``CompilationPipeline``·``Image`` 처럼 ``Unity.*`` 와
    이름이 겹치는 타입이 엉뚱한 것으로 해석됐다.
    """

    source = _every_command()[name]

    for match in re.finditer(r"UnityEditor\.", source):
        prefix = source[max(0, match.start() - 8) : match.start()]
        assert prefix.endswith(
            "global::"
        ), f"{name} 의 {match.start()} 위치 UnityEditor 참조에 global:: 이 없다"


def test_result_marker_is_logged_by_every_assembly_command():
    """표식이 없으면 서버가 결과를 못 읽어 성공도 실패로 보고된다."""

    for name, source in _every_command().items():
        if name == "build_project":
            continue
        assert f'result.Log("{assembly.RESULT_MARKER}' in source


# ---------------------------------------------------------------------------
# 인자 검사 — 값이 C# 소스에 박히기 전에 걸러진다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hostile",
    [
        'Enemy"; result.Log("x',  # 문자열 리터럴 탈출
        "Enemy\\",  # 역슬래시로 닫는 따옴표 잡아먹기
        "Enemy;Boss",  # compose_scene 의 컴포넌트 구분자
        "",
    ],
)
def test_names_that_could_escape_the_literal_are_rejected(hostile: str):
    """인자는 C# 소스에 그대로 박힌다 — 좁은 문자 집합만 통과시킨다."""

    with pytest.raises(AssemblyError):
        assembly.require_name(hostile, "prefabName")


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "C:/Users/x/Enemy.prefab",
        'Assets/Prefabs/E".prefab',
        "Assets\\Prefabs\\..\\x.prefab",
    ],
)
def test_paths_outside_the_project_are_rejected(path: str):
    with pytest.raises(AssemblyError):
        assembly.require_asset_path(path, "prefabPath")


def test_backslash_paths_are_accepted_after_normalisation():
    """Windows 에서 온 경로는 구분자만 바꿔 받는다."""

    assert (
        assembly.require_asset_path("Assets\\Prefabs\\Enemy.prefab", "prefabPath", ".prefab")
        == "Assets/Prefabs/Enemy.prefab"
    )


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("enemyPrefab", ("", "enemyPrefab")),
        ("EnemySpawner.enemyPrefab", ("EnemySpawner", "enemyPrefab")),
    ],
)
def test_field_may_or_may_not_name_its_component(field: str, expected: tuple[str, str]):
    assert assembly.split_field(field) == expected


# ---------------------------------------------------------------------------
# 계층 순서 — 부모가 자식보다 먼저 만들어져야 SetParent 가 성립한다
# ---------------------------------------------------------------------------
def test_children_are_ordered_after_their_parents():
    """설계 검증은 부모의 **존재**만 본다. 순서는 조립이 정한다."""

    shuffled = [
        {"name": "Grid", "parent": "WorldRoot", "components": [], "prefab": ""},
        {"name": "WorldRoot", "parent": "", "components": [], "prefab": ""},
    ]

    ordered = [entry["name"] for entry in assembly.order_objects(shuffled)]

    assert ordered.index("WorldRoot") < ordered.index("Grid")


def test_cyclic_hierarchy_is_rejected():
    cyclic = [
        {"name": "A", "parent": "B", "components": [], "prefab": ""},
        {"name": "B", "parent": "A", "components": [], "prefab": ""},
    ]

    with pytest.raises(AssemblyError):
        assembly.order_objects(cyclic)


# ---------------------------------------------------------------------------
# 설계안이 그대로 인자가 되는가
# ---------------------------------------------------------------------------
def test_scene_command_carries_every_planned_object():
    source = assembly.scene_command("Assets/Scenes/Main.unity", _SCENE_OBJECTS)

    for entry in _SCENE_OBJECTS:
        assert f'"{entry["name"]}"' in source
    # 프리팹 인스턴스는 새로 만들지 않고 프리팹에서 찍어낸다.
    assert "InstantiatePrefab" in source
    # 빌드 설정 등록 — 활성 씬이 없으면 build_project 가 그 자리에서 실패한다.
    assert "EditorBuildSettings" in source


def test_prefab_command_wires_the_sprite_when_one_is_planned():
    with_sprite = assembly.prefab_command(
        "Enemy", "Assets/Prefabs/Enemy.prefab", ["EnemyBrain"], "Assets/Generated/e.png"
    )

    assert "SpriteRenderer" in with_sprite
    assert "Assets/Generated/e.png" in with_sprite


# ---------------------------------------------------------------------------
# Agent-first MCP 도구 경계
# ---------------------------------------------------------------------------
def test_assembly_tools_are_exposed_with_camel_case_arguments():
    """오케스트레이터는 camelCase 로 보낸다. snake_case 면 -32602 로 튕긴다."""

    tools = {tool.name: tool for tool in unity_server.mcp._tool_manager.list_tools()}

    assert {"create_prefab", "compose_scene", "bind_reference"} <= set(tools)
    assert {"gameId", "prefabName"} <= set(tools["create_prefab"].parameters["properties"])
    assert {"gameId", "sceneName"} <= set(tools["compose_scene"].parameters["properties"])
    assert {"gameId", "target", "field", "value"} <= set(
        tools["bind_reference"].parameters["properties"]
    )


def test_command_result_is_read_from_the_same_place_as_the_build_result():
    """조립과 빌드가 같은 방식으로 결과를 싣는다 — 찾는 코드도 하나여야 한다."""

    payload = {
        "data": {
            "executionLogs": f'{assembly.RESULT_MARKER} {{"success":true,"prefab":"Assets/Prefabs/E.prefab"}}'
        }
    }

    assert unity_server._extract_command_result(payload)["success"] is True
    assert unity_server._extract_build_result is unity_server._extract_command_result
