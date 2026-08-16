"""구조 검사기 검증 — 규칙마다 "잡는 경우"와 "안 잡는 경우"를 함께 고정한다.

한쪽만 두면 검사기는 두 가지로 망가질 수 있고 둘 다 조용하다: 아무것도 안 잡으면
게이트가 사라지고, 뭐든 다 잡으면 사람이 꺼 버린다. 그래서 각 규칙에
**위반 케이스와 정상 케이스를 짝으로** 둔다.

실물 대조는 ``test_catches_the_shipped_sample`` 이 맡는다 —
``docs/contracts.md``의 구조 규칙이 겨냥하는 결함이
정말 FAIL 로 나오는지 보는, 이 검사기의 존재 이유다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_layout import (
    RULE_ANIMATOR_WITHOUT_CONTROLLER,
    RULE_DOCUMENT_NAMED_TYPE,
    RULE_FILENAME_MISMATCH,
    RULE_FLAT_SCRIPT_ROOT,
    RULE_RUNTIME_BOOTSTRAP,
    RULE_UNATTACHED_BEHAVIOUR,
    RULE_UNUSED_SPRITE,
    ProjectLayoutError,
    Severity,
    analyze_project,
    parse_types,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_SAMPLE = REPO_ROOT / "git_output" / "work" / "gacha-pet-dungeon-roguelike-001"


# ---------------------------------------------------------------------------
# 가짜 Unity 프로젝트를 만드는 도구
# ---------------------------------------------------------------------------
def _guid(seed: int) -> str:
    return f"{seed:032x}"


class ProjectBuilder:
    """테스트가 읽기 쉬운 최소 Unity 프로젝트를 만든다."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.assets = root / "Assets"
        self.assets.mkdir(parents=True)
        self._next_guid = 1
        self._scene_bodies: dict[str, list[str]] = {}

    def script(self, relative: str, source: str) -> str:
        """``.cs`` 와 그 ``.cs.meta`` 를 함께 쓴다. guid 를 돌려준다."""

        path = self.assets / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        guid = _guid(self._next_guid)
        self._next_guid += 1
        path.with_suffix(".cs.meta").write_text(
            f"fileFormatVersion: 2\nguid: {guid}\n", encoding="utf-8"
        )
        return guid

    def sprite(self, relative: str) -> str:
        path = self.assets / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        guid = _guid(self._next_guid)
        self._next_guid += 1
        path.with_suffix(path.suffix + ".meta").write_text(
            f"fileFormatVersion: 2\nguid: {guid}\n", encoding="utf-8"
        )
        return guid

    def attach(self, scene: str, script_guid: str) -> None:
        """씬(또는 프리팹)에 그 스크립트가 붙었다고 기록한다."""

        self._scene_bodies.setdefault(scene, []).append(
            f"  m_Script: {{fileID: 11500000, guid: {script_guid}, type: 3}}"
        )

    def reference(self, scene: str, asset_guid: str) -> None:
        """스크립트 참조가 아닌 일반 참조(스프라이트 등)."""

        self._scene_bodies.setdefault(scene, []).append(
            f"  m_Sprite: {{fileID: 21300000, guid: {asset_guid}, type: 3}}"
        )

    def write(self) -> Path:
        for name, lines in self._scene_bodies.items():
            path = self.assets / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("%YAML 1.1\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return self.root


def _behaviour(name: str, body: str = "") -> str:
    return (
        "using UnityEngine;\n\n"
        "namespace Game.Gameplay\n{\n"
        f"    public sealed class {name} : MonoBehaviour\n    {{\n{body}    }}\n"
        "}\n"
    )


def _rules(report) -> set[str]:
    return {finding.rule for finding in report.findings}


# ---------------------------------------------------------------------------
# 타입 파싱 — 파일당 첫 하나만 세는 기존 결함을 재현하지 않는지
# ---------------------------------------------------------------------------
def test_parses_every_type_not_just_the_first():
    """``_extract_class_name`` 은 첫 매치만 본다. 검사기는 그러면 안 된다."""

    source = (
        "namespace Game.Gameplay\n{\n"
        "    public sealed class RoomBuilder : MonoBehaviour { }\n"
        "    internal sealed class RoomController : MonoBehaviour { }\n"
        "    internal sealed class RoomData { }\n"
        "    internal static class RoomLog { }\n"
        "    public enum RoomKind { Start, Fight }\n"
        "}\n"
    )
    names = [item.name for item in parse_types(source)]
    assert names == ["RoomBuilder", "RoomController", "RoomData", "RoomLog", "RoomKind"]


def test_ignores_declarations_inside_comments_and_strings():
    """주석·문자열 안의 ``class`` 를 세면 멀쩡한 파일이 거짓 양성을 낸다."""

    source = (
        "namespace Game.Gameplay\n{\n"
        "    // class Spec001 : MonoBehaviour\n"
        "    /* class Spec002 { } */\n"
        '    public sealed class DoorLatch : MonoBehaviour { const string S = "class Spec003"; }\n'
        "}\n"
    )
    assert [item.name for item in parse_types(source)] == ["DoorLatch"]


def test_detects_monobehaviour_by_base_type():
    types = {item.name: item for item in parse_types(_behaviour("DoorLatch") + "\n")}
    assert types["DoorLatch"].is_behaviour

    plain = {item.name: item for item in parse_types("public sealed class Ledger { }")}
    assert not plain["Ledger"].is_behaviour


# ---------------------------------------------------------------------------
# L1 — 문서 번호 이름
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Spec001", "Feature007", "GachaDungeonSpec001"])
def test_document_named_type_fails(tmp_path: Path, name: str):
    builder = ProjectBuilder(tmp_path / "proj")
    guid = builder.script(f"Scripts/World/{name}.cs", _behaviour(name))
    builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    assert RULE_DOCUMENT_NAMED_TYPE in _rules(report)
    assert not report.ok


@pytest.mark.parametrize("name", ["ChunkLoader", "PlayerController", "Inventory2D"])
def test_domain_named_type_passes(tmp_path: Path, name: str):
    """숫자가 들어간 이름이라고 무조건 막으면 ``Inventory2D`` 같은 정상 이름이 걸린다."""

    builder = ProjectBuilder(tmp_path / "proj")
    guid = builder.script(f"Scripts/World/{name}.cs", _behaviour(name))
    builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    assert RULE_DOCUMENT_NAMED_TYPE not in _rules(report)


# ---------------------------------------------------------------------------
# L2 — 어디에도 안 붙은 MonoBehaviour
# ---------------------------------------------------------------------------
def test_unattached_behaviour_fails(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    builder.script("Scripts/World/ChunkLoader.cs", _behaviour("ChunkLoader"))
    attached = builder.script("Scripts/Systems/GameRoot.cs", _behaviour("GameRoot"))
    builder.attach("Scenes/Main.unity", attached)

    report = analyze_project(builder.write())
    unattached = [f for f in report.findings if f.rule == RULE_UNATTACHED_BEHAVIOUR]
    assert [f.target for f in unattached] == ["Scripts/World/ChunkLoader.cs"]
    assert unattached[0].severity is Severity.FAIL


def test_behaviour_attached_to_prefab_passes(tmp_path: Path):
    """씬이 아니라 **프리팹**에 붙은 것도 붙은 것이다 — 오히려 이쪽이 권장이다."""

    builder = ProjectBuilder(tmp_path / "proj")
    guid = builder.script("Scripts/Enemy/Slime.cs", _behaviour("Slime"))
    builder.attach("Prefabs/Slime.prefab", guid)

    report = analyze_project(builder.write())
    assert RULE_UNATTACHED_BEHAVIOUR not in _rules(report)


def test_plain_class_need_not_be_attached(tmp_path: Path):
    """MonoBehaviour 가 아닌 타입까지 부착을 요구하면 데이터 클래스가 전부 걸린다."""

    builder = ProjectBuilder(tmp_path / "proj")
    builder.script("Scripts/Data/LootTable.cs", "public sealed class LootTable { }\n")

    report = analyze_project(builder.write())
    assert RULE_UNATTACHED_BEHAVIOUR not in _rules(report)


# ---------------------------------------------------------------------------
# L3 — 런타임 조립 (조립 도구가 없어 생긴 우회책)
# ---------------------------------------------------------------------------
def test_runtime_bootstrap_warns(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    body = (
        "        private void Awake()\n        {\n"
        "            var go = new GameObject(\"Spawner\");\n"
        "            go.AddComponent<EnemySpawner>();\n"
        "        }\n"
    )
    guid = builder.script("Scripts/Systems/GameRoot.cs", _behaviour("GameRoot", body))
    builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    hits = [f for f in report.findings if f.rule == RULE_RUNTIME_BOOTSTRAP]
    assert len(hits) == 1
    assert hits[0].severity is Severity.WARN
    assert report.ok, "경고는 게이트를 막지 않는다"


def test_addcomponent_alone_does_not_warn(tmp_path: Path):
    """이미 있는 오브젝트에 컴포넌트를 붙이는 것은 정상적인 쓰임이다."""

    builder = ProjectBuilder(tmp_path / "proj")
    body = "        private void Awake() { gameObject.AddComponent<Rigidbody2D>(); }\n"
    guid = builder.script("Scripts/Player/PlayerBody.cs", _behaviour("PlayerBody", body))
    builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    assert RULE_RUNTIME_BOOTSTRAP not in _rules(report)


# ---------------------------------------------------------------------------
# L4 / L5 / L6
# ---------------------------------------------------------------------------
def test_flat_script_root_warns(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    for name in ("ChunkLoader", "PlayerController"):
        guid = builder.script(f"Scripts/{name}.cs", _behaviour(name))
        builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    assert RULE_FLAT_SCRIPT_ROOT in _rules(report)


def test_foldered_scripts_do_not_warn(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    for folder, name in (("World", "ChunkLoader"), ("Player", "PlayerController")):
        guid = builder.script(f"Scripts/{folder}/{name}.cs", _behaviour(name))
        builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    assert RULE_FLAT_SCRIPT_ROOT not in _rules(report)


def test_unused_sprite_warns_and_used_one_does_not(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    used = builder.sprite("Generated/player_idle.png")
    builder.sprite("Generated/unused_prop.png")
    builder.reference("Scenes/Main.unity", used)

    report = analyze_project(builder.write())
    unused = [f for f in report.findings if f.rule == RULE_UNUSED_SPRITE]
    assert [f.target for f in unused] == ["Generated/unused_prop.png"]


def test_filename_must_match_a_public_type(tmp_path: Path):
    """Unity 는 MonoBehaviour 를 파일명으로 찾는다 — 어긋나면 씬에 붙지 않는다."""

    builder = ProjectBuilder(tmp_path / "proj")
    guid = builder.script("Scripts/World/Loader.cs", _behaviour("ChunkLoader"))
    builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    assert RULE_FILENAME_MISMATCH in _rules(report)


def test_internal_helper_beside_matching_public_type_is_fine(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    source = _behaviour("ChunkLoader") + "\ninternal sealed class ChunkKey { }\n"
    guid = builder.script("Scripts/World/ChunkLoader.cs", source)
    builder.attach("Scenes/Main.unity", guid)

    report = analyze_project(builder.write())
    assert RULE_FILENAME_MISMATCH not in _rules(report)


# ---------------------------------------------------------------------------
# 통과해야 하는 프로젝트 — 검사기가 "뭐든 다 잡는" 상태가 아닌지
# ---------------------------------------------------------------------------
def test_well_formed_project_passes_cleanly(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    loader = builder.script("Scripts/World/ChunkLoader.cs", _behaviour("ChunkLoader"))
    player = builder.script("Scripts/Player/PlayerController.cs", _behaviour("PlayerController"))
    builder.script("Scripts/Data/LootTable.cs", "public sealed class LootTable { }\n")
    sprite = builder.sprite("Generated/player_idle.png")

    builder.attach("Scenes/Main.unity", loader)
    builder.attach("Prefabs/Player.prefab", player)
    builder.reference("Prefabs/Player.prefab", sprite)

    report = analyze_project(builder.write())
    assert report.findings == [], f"정상 프로젝트에서 위반이 나왔다: {report.findings}"
    assert report.ok


def test_missing_assets_folder_is_an_input_error(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ProjectLayoutError):
        analyze_project(tmp_path / "empty")


# ---------------------------------------------------------------------------
# 실물 대조 — 이 검사기가 존재하는 이유
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not SHIPPED_SAMPLE.is_dir(), reason="표본 산출물이 없는 체크아웃")
def test_catches_the_shipped_sample():
    """QA PASS 로 배포됐던 산출물이 이 검사기에서는 FAIL 이어야 한다.

    이게 통과(=OK)로 나오면 검사기가 잘못된 것이다. 그 산출물은 스크립트 6개가
    전부 문서 번호 이름이고 대부분 어디에도 붙어 있지 않다.
    """

    report = analyze_project(SHIPPED_SAMPLE)
    assert not report.ok

    rules = _rules(report)
    assert RULE_DOCUMENT_NAMED_TYPE in rules
    assert RULE_UNATTACHED_BEHAVIOUR in rules
    assert RULE_RUNTIME_BOOTSTRAP in rules, "Spec006 의 런타임 부트스트랩을 놓쳤다"

    unattached = {f.target for f in report.findings if f.rule == RULE_UNATTACHED_BEHAVIOUR}
    assert len(unattached) == 5, f"붙지 않은 스크립트가 5개여야 한다: {sorted(unattached)}"
    assert "Scripts/Spec006.cs" not in unattached, "Spec006 은 씬에 붙어 있다"


# ---------------------------------------------------------------------------
# L8 — 컨트롤러 없는 Animator (Issue #28)
# ---------------------------------------------------------------------------
_EMPTY_ANIMATOR = """%YAML 1.1
--- !u!95 &4242
Animator:
  m_ObjectHideFlags: 0
  m_Enabled: 1
  m_Avatar: {fileID: 0}
  m_Controller: {fileID: 0}
  m_CullingMode: 0
"""

_BOUND_ANIMATOR = _EMPTY_ANIMATOR.replace(
    "m_Controller: {fileID: 0}",
    "m_Controller: {fileID: 9100000, guid: 1234567890abcdef1234567890abcdef, type: 2}",
)


def _write_prefab(root: Path, relative: str, body: str) -> None:
    path = root / "Assets" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_animator_without_controller_warns(tmp_path: Path):
    """컨트롤러 없는 Animator 는 오류 없이 조용히 아무 일도 하지 않는다."""

    builder = ProjectBuilder(tmp_path / "proj")
    root = builder.write()
    _write_prefab(root, "Prefabs/Player.prefab", _EMPTY_ANIMATOR)

    report = analyze_project(root)
    findings = [f for f in report.findings if f.rule == RULE_ANIMATOR_WITHOUT_CONTROLLER]

    assert [f.target for f in findings] == ["Prefabs/Player.prefab"]
    assert findings[0].severity is Severity.WARN
    assert report.ok is True, "경고이지 실패는 아니다"


def test_animator_with_controller_does_not_warn(tmp_path: Path):
    builder = ProjectBuilder(tmp_path / "proj")
    root = builder.write()
    _write_prefab(root, "Prefabs/Player.prefab", _BOUND_ANIMATOR)

    report = analyze_project(root)

    assert RULE_ANIMATOR_WITHOUT_CONTROLLER not in _rules(report)
