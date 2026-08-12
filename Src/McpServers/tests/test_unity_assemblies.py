"""어셈블리 분할(4단계)이 **컴파일을 깨뜨릴 수 있는 분할을 만들지 않는가.**

이 단계가 마지막으로 밀린 이유가 그대로 이 파일의 주제다. 어셈블리를 잘못 나누면
순환 참조로 컴파일이 통째로 막히는데, 그 증상은 "느려진다"가 아니라 "게임이 안
만들어진다"라서 되돌리기가 비싸다
(``docs/contracts.md``의 Unity 구조 계약).

그래서 검사할 것은 "순환을 잘 잡아내는가"가 아니라 **"순환이 될 수 있는 것을
애초에 후보에서 빼는가"** 다. 아래 세 가지가 깨지면 생성된 게임이 컴파일되지 않는다.

1. 서로 물린 카테고리를 나누지 않는다 (``.asmdef`` 는 폴더 하나만 덮으므로 합칠
   수가 없고, 합칠 수 없으면 나눌 수도 없다).
2. ``Assembly-CSharp`` 에 남는 것에 의존하는 카테고리를 나누지 않는다
   (``.asmdef`` 어셈블리는 ``Assembly-CSharp`` 를 참조할 수 없다).
3. ``using`` 이 가리키는 패키지 어셈블리를 참조에 싣는다 (``Assembly-CSharp`` 가
   공짜로 주던 참조라, 떼어내는 순간 명시하지 않으면 컴파일되지 않는다).

프로젝트를 tmp_path 에 텍스트로 지어 판정한다 — Unity Editor 없이 도는 검사다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_layout import RULE_ASSEMBLY_CYCLE, Severity, analyze_project
from unity.assemblies import PACKAGE_ASSEMBLIES, plan_assemblies

_META = "fileFormatVersion: 2\nguid: {guid}\n"


def _script(project: Path, relative: str, source: str, guid: str) -> None:
    """``Assets/<relative>`` 에 ``.cs`` 와 그 ``.cs.meta`` 를 함께 놓는다."""

    path = project / "Assets" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.with_suffix(".cs.meta").write_text(_META.format(guid=guid), encoding="utf-8")


def _behaviour(name: str, body: str = "", usings: str = "") -> str:
    return (
        f"using UnityEngine;\n{usings}\n"
        "namespace Game.Gameplay\n{\n"
        f"    public sealed class {name} : MonoBehaviour\n"
        "    {\n"
        f"{body}\n"
        "    }\n}\n"
    )


def _plan(project: Path):
    return plan_assemblies(project, root_namespace="Game.Gameplay")


def _named(layout) -> dict[str, object]:
    return {item.category: item for item in layout.assemblies}


# ---------------------------------------------------------------------------
# 나눌 수 있는 경우 — 방향이 한쪽인 의존
# ---------------------------------------------------------------------------
def test_acyclic_categories_split_and_reference_downstream(tmp_path: Path) -> None:
    """``Systems`` → ``Data`` 처럼 한쪽 방향이면 둘 다 떨어져 나온다."""

    _script(tmp_path, "Scripts/Data/LootTable.cs", _behaviour("LootTable"), "a" * 32)
    _script(
        tmp_path,
        "Scripts/Systems/LootSpawner.cs",
        _behaviour("LootSpawner", "        private LootTable _table;"),
        "b" * 32,
    )

    plans = _named(_plan(tmp_path))

    assert set(plans) == {"Data", "Systems"}
    assert plans["Systems"].references == ("Game.Data",)
    assert plans["Data"].references == ()
    assert plans["Systems"].path == "Assets/Scripts/Systems/Game.Systems.asmdef"


def test_emitted_asmdef_is_valid_json_with_references_merged(tmp_path: Path) -> None:
    """``.asmdef`` 본문에 카테고리 참조와 패키지 참조가 함께 실린다."""

    _script(tmp_path, "Scripts/Data/LootTable.cs", _behaviour("LootTable"), "a" * 32)
    _script(
        tmp_path,
        "Scripts/Player/PlayerInput.cs",
        _behaviour(
            "PlayerInput",
            "        private LootTable _table;",
            usings="using UnityEngine.InputSystem;",
        ),
        "b" * 32,
    )

    plan = _named(_plan(tmp_path))["Player"]
    body = json.loads(plan.to_json("Game.Gameplay"))

    assert body["name"] == "Game.Player"
    assert body["rootNamespace"] == "Game.Gameplay"
    assert body["references"] == ["Game.Data", "Unity.InputSystem"]
    assert body["includePlatforms"] == []


# ---------------------------------------------------------------------------
# 나누면 안 되는 경우 — 이 셋이 이 단계의 알맹이다
# ---------------------------------------------------------------------------
def test_mutually_referencing_categories_are_never_split(tmp_path: Path) -> None:
    """서로 참조하는 두 폴더는 통째로 남는다.

    한 ``.asmdef`` 는 폴더 하나와 그 하위만 덮으므로 형제 폴더 둘을 한 어셈블리로
    합칠 수 없다. 합칠 수 없으면 나눌 수도 없다 — 나누면 그게 순환 참조다.
    """

    _script(
        tmp_path,
        "Scripts/World/ChunkLoader.cs",
        _behaviour("ChunkLoader", "        private PlayerBody _body;"),
        "a" * 32,
    )
    _script(
        tmp_path,
        "Scripts/Player/PlayerBody.cs",
        _behaviour("PlayerBody", "        private ChunkLoader _loader;"),
        "b" * 32,
    )

    layout = _plan(tmp_path)

    assert layout.assemblies == ()
    assert {item.category for item in layout.skipped} == {"World", "Player"}
    assert "서로 참조한다" in next(
        item.reason for item in layout.skipped if item.category == "World"
    )


def test_indirect_cycle_is_also_left_alone(tmp_path: Path) -> None:
    """A → B → C → A 처럼 돌아가는 것도 잡아야 한다. 직접 참조만 보면 놓친다."""

    _script(
        tmp_path, "Scripts/A/Alpha.cs", _behaviour("Alpha", "        private Beta _b;"), "a" * 32
    )
    _script(
        tmp_path, "Scripts/B/Beta.cs", _behaviour("Beta", "        private Gamma _g;"), "b" * 32
    )
    _script(
        tmp_path, "Scripts/C/Gamma.cs", _behaviour("Gamma", "        private Alpha _a;"), "c" * 32
    )

    layout = _plan(tmp_path)

    assert layout.assemblies == ()
    assert {item.category for item in layout.skipped} == {"A", "B", "C"}


def test_category_depending_on_assembly_csharp_is_left_alone(tmp_path: Path) -> None:
    """``Assets/Scripts`` 최상위 파일에 의존하면 떼어낼 수 없다.

    ``.asmdef`` 어셈블리는 ``Assembly-CSharp`` 를 참조할 수 없다 — 방향이 한쪽
    뿐이다. 최상위 파일은 덮을 폴더가 없어 거기 남으므로, 그것을 쓰는 카테고리도
    함께 남아야 한다.
    """

    _script(tmp_path, "Scripts/GameLog.cs", _behaviour("GameLog"), "a" * 32)
    _script(
        tmp_path,
        "Scripts/World/ChunkLoader.cs",
        _behaviour("ChunkLoader", "        private GameLog _log;"),
        "b" * 32,
    )

    layout = _plan(tmp_path)

    assert layout.assemblies == ()
    reason = next(item.reason for item in layout.skipped if item.category == "World")
    assert "Assembly-CSharp" in reason


def test_transitive_dependency_on_assembly_csharp_also_blocks(tmp_path: Path) -> None:
    """막힌 카테고리에 의존하는 카테고리도 함께 막혀야 한다 — 한 칸만 보면 샌다."""

    _script(tmp_path, "Scripts/GameLog.cs", _behaviour("GameLog"), "a" * 32)
    _script(
        tmp_path,
        "Scripts/World/ChunkLoader.cs",
        _behaviour("ChunkLoader", "        private GameLog _log;"),
        "b" * 32,
    )
    _script(
        tmp_path,
        "Scripts/Systems/WorldClock.cs",
        _behaviour("WorldClock", "        private ChunkLoader _loader;"),
        "c" * 32,
    )

    layout = _plan(tmp_path)

    assert layout.assemblies == ()
    assert "Systems" in {item.category for item in layout.skipped}


# ---------------------------------------------------------------------------
# 패키지 참조 — 놓치면 그 자리에서 컴파일이 깨진다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        ("UnityEngine.InputSystem", "Unity.InputSystem"),
        ("UnityEngine.InputSystem.Controls", "Unity.InputSystem"),
        ("TMPro", "Unity.TextMeshPro"),
        ("UnityEngine.UI", "UnityEngine.UI"),
    ],
)
def test_package_using_becomes_a_reference(tmp_path: Path, namespace: str, expected: str) -> None:
    _script(
        tmp_path,
        "Scripts/UI/HudView.cs",
        _behaviour("HudView", usings=f"using {namespace};"),
        "a" * 32,
    )

    assert _named(_plan(tmp_path))["UI"].packages == (expected,)


def test_unityengine_uielements_is_not_mistaken_for_unityengine_ui(tmp_path: Path) -> None:
    """접두사 대조가 경계를 요구하지 않으면 ``UIElements`` 가 ``UI`` 로 잘못 잡힌다."""

    _script(
        tmp_path,
        "Scripts/UI/HudView.cs",
        _behaviour("HudView", usings="using UnityEngine.UIElements;"),
        "a" * 32,
    )

    assert _named(_plan(tmp_path))["UI"].packages == ()


def test_package_map_values_are_not_namespaces(tmp_path: Path) -> None:
    """값은 어셈블리 이름이어야 한다. 네임스페이스를 적으면 참조가 안 풀린다."""

    assert "UnityEngine.InputSystem" not in PACKAGE_ASSEMBLIES.values()


# ---------------------------------------------------------------------------
# 주석·문자열은 참조가 아니다
# ---------------------------------------------------------------------------
def test_type_name_inside_a_comment_is_not_a_reference(tmp_path: Path) -> None:
    """주석에 적힌 타입 이름 때문에 어셈블리가 덜 쪼개지면 안 된다."""

    _script(tmp_path, "Scripts/Data/LootTable.cs", _behaviour("LootTable"), "a" * 32)
    _script(
        tmp_path,
        "Scripts/UI/HudView.cs",
        _behaviour(
            "HudView", "        // LootTable 은 여기서 쓰지 않는다\n        private int _n;"
        ),
        "b" * 32,
    )

    assert _named(_plan(tmp_path))["UI"].references == ()


# ---------------------------------------------------------------------------
# L7 — 이미 놓인 .asmdef 의 순환
# ---------------------------------------------------------------------------
def _asmdef(project: Path, relative: str, name: str, references: list[str]) -> None:
    path = project / "Assets" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": name, "references": references}), encoding="utf-8")


def test_layout_check_flags_a_cyclic_asmdef_pair(tmp_path: Path) -> None:
    """손으로 고친 ``.asmdef`` 가 순환이면 빌드 전에 잡아야 한다."""

    _asmdef(tmp_path, "Scripts/World/Game.World.asmdef", "Game.World", ["Game.Player"])
    _asmdef(tmp_path, "Scripts/Player/Game.Player.asmdef", "Game.Player", ["Game.World"])

    findings = [
        item for item in analyze_project(tmp_path).findings if item.rule == RULE_ASSEMBLY_CYCLE
    ]

    assert len(findings) == 1
    assert findings[0].severity is Severity.FAIL
    assert findings[0].target == "Game.Player ↔ Game.World"


def test_layout_check_ignores_package_references(tmp_path: Path) -> None:
    """패키지 어셈블리는 우리 것을 참조하지 않으므로 순환의 한쪽이 될 수 없다."""

    _asmdef(
        tmp_path,
        "Scripts/World/Game.World.asmdef",
        "Game.World",
        ["Unity.InputSystem", "Unity.TextMeshPro"],
    )

    assert [
        item for item in analyze_project(tmp_path).findings if item.rule == RULE_ASSEMBLY_CYCLE
    ] == []


def test_plan_output_never_trips_the_cycle_check(tmp_path: Path) -> None:
    """계획이 만든 것에는 순환이 있을 수 없다 — 이 단계의 안전 주장 그 자체다."""

    _script(tmp_path, "Scripts/Data/LootTable.cs", _behaviour("LootTable"), "a" * 32)
    _script(
        tmp_path,
        "Scripts/Systems/LootSpawner.cs",
        _behaviour("LootSpawner", "        private LootTable _t;"),
        "b" * 32,
    )
    _script(
        tmp_path,
        "Scripts/UI/HudView.cs",
        _behaviour("HudView", "        private LootSpawner _s;\n        private LootTable _t;"),
        "c" * 32,
    )

    layout = _plan(tmp_path)
    for plan in layout.assemblies:
        target = tmp_path / plan.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(plan.to_json(layout.root_namespace), encoding="utf-8")

    assert len(layout.assemblies) == 3
    assert [
        item for item in analyze_project(tmp_path).findings if item.rule == RULE_ASSEMBLY_CYCLE
    ] == []
