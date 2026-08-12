"""설계 패스 검증 — 거부해야 할 것을 실제로 거부하는지.

이 모듈의 규칙들이 이 저장소가 실제로 낸 결함을 **설계 시점에** 막는 장치다
(``docs/contracts.md``의 Unity 설계 계약). 그래서
:func:`test_rejects_the_structure_that_actually_shipped` 가 이 파일의 중심이다 —
그 구조가 여기서 통과하면 1단계가 아무것도 한 게 없는 것이다.

모델이 만든 설계안이든 사람이 ``design`` 인자로 넘긴 것이든 같은 검증을 거친다
(CLAUDE.md 의 두-경로 규칙). 그래서 이 테스트는 LLM 없이 돈다.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from unity.architecture import ArchitectureError, Design, validate_design

FEATURE_IDS = ["spec-001", "spec-002"]


def _design(**overrides: Any) -> dict[str, Any]:
    """규칙을 모두 지키는 최소 설계안. 테스트가 한 곳씩 망가뜨린다."""

    base: dict[str, Any] = {
        "files": [
            {
                "path": "Assets/Scripts/World/ChunkLoader.cs",
                "className": "ChunkLoader",
                "kind": "MonoBehaviour",
                "featureIds": ["spec-001"],
                "responsibility": "플레이어 주변 청크를 로드하고 벗어난 것을 해제한다.",
                "dependsOn": [],
            },
            {
                "path": "Assets/Scripts/World/ChunkKey.cs",
                "className": "ChunkKey",
                "kind": "plain",
                "featureIds": ["spec-001"],
                "responsibility": "청크 좌표를 값으로 표현한다.",
                "dependsOn": [],
            },
            {
                "path": "Assets/Scripts/Player/PlayerController.cs",
                "className": "PlayerController",
                "kind": "MonoBehaviour",
                "featureIds": ["spec-002"],
                "responsibility": "8방향 입력을 Rigidbody2D 속도로 옮긴다.",
                "dependsOn": ["Assets/Scripts/World/ChunkLoader.cs"],
            },
        ],
        "prefabs": [
            {
                "name": "Player",
                "path": "Assets/Prefabs/Player.prefab",
                "components": ["PlayerController", "Rigidbody2D", "SpriteRenderer"],
                "sprite": "Assets/Generated/player_idle.png",
            }
        ],
        "scene": {
            "name": "Main",
            "objects": [
                {"name": "WorldRoot", "parent": "", "components": ["ChunkLoader"], "prefab": ""},
                {"name": "Player", "parent": "", "components": [], "prefab": "Player"},
            ],
        },
        "notes": [],
    }
    base.update(overrides)
    return base


def _validate(raw: dict[str, Any]) -> Design:
    return validate_design(raw, FEATURE_IDS)


# ---------------------------------------------------------------------------
# 통과해야 하는 설계안
# ---------------------------------------------------------------------------
def test_accepts_a_well_formed_design():
    design = _validate(_design())

    assert [item.class_name for item in design.files] == [
        "ChunkLoader",
        "ChunkKey",
        "PlayerController",
    ]
    assert design.prefabs[0]["generatedComponents"] == ["PlayerController"]


def test_orders_files_so_dependencies_come_first():
    raw = _design()
    # 의존 대상을 뒤에 두어도 정렬이 앞으로 끌어와야 한다.
    raw["files"] = [raw["files"][2], raw["files"][0], raw["files"][1]]

    ordered = [item.class_name for item in _validate(raw).files]

    assert ordered.index("ChunkLoader") < ordered.index("PlayerController")


def test_type_map_lists_every_type_for_the_cache_prefix():
    """첫 파일부터 전체 타입 지도를 보게 하는 것이 설계 패스의 목적 중 하나다."""

    type_map = _validate(_design()).type_map()

    for name in ("ChunkLoader", "ChunkKey", "PlayerController"):
        assert name in type_map


# ---------------------------------------------------------------------------
# 이 저장소가 실제로 낸 구조 — 반드시 거부돼야 한다
# ---------------------------------------------------------------------------
def test_rejects_the_structure_that_actually_shipped():
    """spec 한 장 = 파일 한 개, 문서 번호 이름, 아무 데도 안 붙음.

    ``gacha-pet-dungeon-roguelike-001`` 이 정확히 이 모양이었고 QA PASS 가 났다.
    설계 단계에서 거부되면 그 산출물은 애초에 만들어지지 않는다.
    """

    shipped = {
        "files": [
            {
                "path": "Assets/Scripts/Spec001.cs",
                "className": "Spec001",
                "kind": "MonoBehaviour",
                "featureIds": ["spec-001"],
                "responsibility": "가챠 소환.",
                "dependsOn": [],
            },
            {
                "path": "Assets/Scripts/Spec002.cs",
                "className": "Spec002",
                "kind": "MonoBehaviour",
                "featureIds": ["spec-002"],
                "responsibility": "펫 스킬.",
                "dependsOn": [],
            },
        ],
        "prefabs": [],
        "scene": {"name": "Main", "objects": []},
        "notes": [],
    }

    with pytest.raises(ArchitectureError) as exc:
        _validate(shipped)

    # 첫 번째로 걸리는 것은 이름이다 — 어느 규칙이든 걸리기만 하면 되지만,
    # 이름 규칙이 가장 먼저 보이는 증상이라 그 순서를 고정해 둔다.
    assert "Spec001" in str(exc.value)


def test_rejects_a_monobehaviour_attached_to_nothing():
    """이 규칙 하나가 "파일은 있는데 실행되지 않는 코드"를 원천 차단한다."""

    raw = _design()
    raw["files"].append(
        {
            "path": "Assets/Scripts/Systems/DayNightCycle.cs",
            "className": "DayNightCycle",
            "kind": "MonoBehaviour",
            "featureIds": ["spec-002"],
            "responsibility": "낮과 밤을 교대시킨다.",
            "dependsOn": [],
        }
    )

    with pytest.raises(ArchitectureError, match="DayNightCycle"):
        _validate(raw)


def test_a_plain_type_need_not_be_attached():
    """GameObject 가 필요 없는 타입까지 부착을 요구하면 데이터 클래스를 못 만든다."""

    raw = _design()
    raw["files"].append(
        {
            "path": "Assets/Scripts/Data/LootTable.cs",
            "className": "LootTable",
            "kind": "plain",
            "featureIds": ["spec-002"],
            "responsibility": "드롭 확률표를 담는다.",
            "dependsOn": [],
        }
    )

    assert any(item.class_name == "LootTable" for item in _validate(raw).files)


# ---------------------------------------------------------------------------
# 나머지 거부 규칙
# ---------------------------------------------------------------------------
def test_rejects_an_uncovered_feature():
    raw = _design()
    raw["files"] = [item for item in raw["files"] if item["featureIds"] != ["spec-002"]]
    raw["scene"]["objects"] = [raw["scene"]["objects"][0]]
    raw["prefabs"] = []

    with pytest.raises(ArchitectureError, match="spec-002"):
        _validate(raw)


def test_rejects_a_feature_id_that_does_not_exist():
    raw = _design()
    raw["files"][0]["featureIds"] = ["spec-009"]

    with pytest.raises(ArchitectureError, match="spec-009"):
        _validate(raw)


@pytest.mark.parametrize(
    "path",
    [
        "Assets/Scripts/ChunkLoader.cs",  # 폴더 없음 — Folder Rule 위반
        "Scripts/World/ChunkLoader.cs",  # Assets 밖
        "Assets/Scripts/World/ChunkLoader.txt",  # .cs 아님
    ],
)
def test_rejects_a_path_outside_the_folder_rule(path: str):
    raw = _design()
    raw["files"][0]["path"] = path

    with pytest.raises(ArchitectureError):
        _validate(raw)


def test_rejects_a_filename_that_does_not_match_the_class():
    raw = _design()
    raw["files"][0]["path"] = "Assets/Scripts/World/Loader.cs"

    with pytest.raises(ArchitectureError, match="Loader"):
        _validate(raw)


def test_rejects_duplicate_paths():
    raw = _design()
    raw["files"][1]["path"] = raw["files"][0]["path"]
    raw["files"][1]["className"] = "ChunkLoader"

    with pytest.raises(ArchitectureError):
        _validate(raw)


def test_rejects_a_dependency_cycle():
    raw = _design()
    raw["files"][0]["dependsOn"] = ["Assets/Scripts/Player/PlayerController.cs"]

    with pytest.raises(ArchitectureError, match="순환"):
        _validate(raw)


def test_rejects_a_dependency_on_a_file_that_is_not_planned():
    raw = _design()
    raw["files"][0]["dependsOn"] = ["Assets/Scripts/Ghost/Missing.cs"]

    with pytest.raises(ArchitectureError, match="Missing"):
        _validate(raw)


def test_rejects_a_scene_object_pointing_at_an_unknown_prefab():
    raw = _design()
    raw["scene"]["objects"][1]["prefab"] = "Ghost"

    with pytest.raises(ArchitectureError, match="Ghost"):
        _validate(raw)


def test_rejects_a_scene_object_whose_parent_is_absent():
    raw = _design()
    raw["scene"]["objects"][0]["parent"] = "NoSuchRoot"

    with pytest.raises(ArchitectureError, match="NoSuchRoot"):
        _validate(raw)


def test_rejects_an_unknown_kind():
    raw = _design()
    raw["files"][0]["kind"] = "Component"

    with pytest.raises(ArchitectureError, match="kind"):
        _validate(raw)


def test_rejects_an_empty_design():
    with pytest.raises(ArchitectureError):
        _validate({"files": [], "prefabs": [], "scene": {"name": "M", "objects": []}, "notes": []})


def test_rejects_a_prefab_outside_the_prefab_folder():
    raw = _design()
    raw["prefabs"][0]["path"] = "Assets/Player.prefab"

    with pytest.raises(ArchitectureError):
        _validate(raw)


def test_validation_does_not_mutate_the_input():
    """호출자가 넘긴 dict 를 조용히 고치면 경로 B 가 무엇을 보냈는지 알 수 없다."""

    raw = _design()
    before = copy.deepcopy(raw)

    _validate(raw)

    assert raw == before
