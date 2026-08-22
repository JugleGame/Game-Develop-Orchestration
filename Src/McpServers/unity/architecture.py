"""호스트가 작성한 Unity 파일·프리팹·씬 설계안을 결정론적으로 검증한다."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

# "문서 번호에서 온 이름" 판정은 레이아웃 검사기와 **같은 것을 써야** 한다.
# 두 벌을 두면 설계 단계에서 통과한 이름이 검사 단계에서 막히는(또는 그 반대의)
# 상태가 생기고, 그 어긋남은 실행해 보기 전까지 드러나지 않는다.
from project_layout import DOCUMENT_NAME_PATTERN as _DOCUMENT_NAME_PATTERN

# 파일 경로는 04 명세의 Folder Rule 을 코드로 옮긴 것이다.
# ``Assets/Scripts/<Category>/<ClassName>.cs``
_PATH_PATTERN = re.compile(r"^Assets/Scripts/[A-Z][A-Za-z0-9]*(?:/[A-Z][A-Za-z0-9]*)*\.cs$")
_CLASS_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9]*$")

KINDS = ("MonoBehaviour", "ScriptableObject", "plain", "static")

# 씬이나 프리팹에 붙어야만 실행되는 종류.
_ATTACHABLE = "MonoBehaviour"

class ArchitectureError(RuntimeError):
    """설계안이 규칙을 어겼거나 생성에 실패했다."""


@dataclass(frozen=True)
class PlannedFile:
    """설계안의 파일 한 장. CodeGen 이 이 단위로 순회한다."""

    path: str
    class_name: str
    kind: str
    feature_ids: tuple[str, ...]
    responsibility: str
    depends_on: tuple[str, ...]

    @property
    def is_attachable(self) -> bool:
        return self.kind == _ATTACHABLE


@dataclass
class Design:
    """검증을 통과한 설계안."""

    files: list[PlannedFile] = field(default_factory=list)
    prefabs: list[dict[str, Any]] = field(default_factory=list)
    scene: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [
                {
                    "path": item.path,
                    "className": item.class_name,
                    "kind": item.kind,
                    "featureIds": list(item.feature_ids),
                    "responsibility": item.responsibility,
                    "dependsOn": list(item.depends_on),
                }
                for item in self.files
            ],
            "prefabs": self.prefabs,
            "scene": self.scene,
            "notes": self.notes,
        }

    def type_map(self) -> str:
        """Return the complete type map for cross-file dependency review."""

        lines = ["Types in this game (all of them, decided up front):"]
        for item in self.files:
            lines.append(f"- {item.class_name} ({item.kind}) — {item.responsibility}")
        return "\n".join(lines)


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ArchitectureError(f"{label} 은 배열이어야 합니다")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchitectureError(f"{label} 은 비어 있지 않은 문자열이어야 합니다")
    return value.strip()


def _validate_transform(raw: Any, label: str) -> dict[str, list[float]]:
    if not isinstance(raw, dict):
        raise ArchitectureError(f"{label} 은 객체여야 합니다")
    unknown = sorted(set(raw) - {"position", "rotation", "scale"})
    if unknown:
        raise ArchitectureError(f"{label} 에 알 수 없는 필드가 있습니다: {unknown}")

    transform: dict[str, list[float]] = {}
    for field in ("position", "rotation", "scale"):
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or len(value) != 3 or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(item)
            for item in value
        ):
            raise ArchitectureError(f"{label}.{field} 은 유한한 숫자 3개짜리 배열이어야 합니다")
        transform[field] = [float(item) for item in value]
    return transform


def _order_files(files: list[PlannedFile]) -> list[PlannedFile]:
    """``dependsOn`` 위상 정렬. 순환이면 실패한다.

    의존 대상이 먼저 만들어져야 생성 시점에 그 타입을 참조할 수 있다. 순환이면
    "먼저"가 정의되지 않으므로 설계안 자체를 거부한다 — 순서를 임의로 정해
    넘기면 그 자리에서가 아니라 컴파일 단계에서 터진다.
    """

    by_path = {item.path: item for item in files}
    ordered: list[PlannedFile] = []
    state: dict[str, int] = {}  # 0 방문중, 1 완료

    def visit(path: str, trail: tuple[str, ...]) -> None:
        if state.get(path) == 1:
            return
        if state.get(path) == 0:
            cycle = " → ".join((*trail, path))
            raise ArchitectureError(f"dependsOn 에 순환이 있습니다: {cycle}")
        state[path] = 0
        for dependency in by_path[path].depends_on:
            # 없는 경로를 가리키는 것은 앞선 검사에서 이미 걸렀다.
            visit(dependency, (*trail, path))
        state[path] = 1
        ordered.append(by_path[path])

    for item in files:
        visit(item.path, ())
    return ordered


def validate_design(raw: Any, feature_ids: list[str]) -> Design:
    """설계안을 검증해 :class:`Design` 으로 만든다. 생성이든 전달이든 같은 규칙.

    거부 조건은 ``docs/contracts.md``의 Unity 설계 계약과 대응한다.
    """

    if not isinstance(raw, dict):
        raise ArchitectureError("설계안은 JSON 객체여야 합니다")

    files: list[PlannedFile] = []
    seen_paths: set[str] = set()
    seen_classes: set[str] = set()

    for index, entry in enumerate(_require_list(raw.get("files"), "files")):
        if not isinstance(entry, dict):
            raise ArchitectureError(f"files[{index}] 는 객체여야 합니다")

        path = _require_str(entry.get("path"), f"files[{index}].path")
        class_name = _require_str(entry.get("className"), f"files[{index}].className")
        kind = _require_str(entry.get("kind"), f"files[{index}].kind")

        if kind not in KINDS:
            raise ArchitectureError(f"files[{index}].kind 가 {KINDS} 중 하나가 아닙니다: {kind}")
        if not _CLASS_PATTERN.match(class_name):
            raise ArchitectureError(
                f"className 은 PascalCase 여야 합니다: {class_name!r} (files[{index}])"
            )
        # 문서 번호 이름 금지 — 이 파이프라인이 실제로 만들던 것이 이것이다.
        if _DOCUMENT_NAME_PATTERN.search(class_name):
            raise ArchitectureError(
                f"className '{class_name}' 이 spec 문서 번호에서 왔습니다. "
                "클래스 이름은 게임 개념이어야 합니다 (ChunkLoader, PlayerController)."
            )
        if not _PATH_PATTERN.match(path):
            raise ArchitectureError(
                f"path 는 Assets/Scripts/<Category>/<ClassName>.cs 형식이어야 합니다: {path}"
            )
        if not path.endswith(f"/{class_name}.cs"):
            raise ArchitectureError(
                f"파일명이 className 과 다릅니다: {path} vs {class_name}. "
                "Unity 는 MonoBehaviour 를 파일명으로 찾습니다."
            )
        if path in seen_paths:
            raise ArchitectureError(f"같은 path 가 두 번 나왔습니다: {path}")
        if class_name in seen_classes:
            raise ArchitectureError(f"같은 className 이 두 번 나왔습니다: {class_name}")
        seen_paths.add(path)
        seen_classes.add(class_name)

        feature_list = _require_list(entry.get("featureIds"), f"files[{index}].featureIds")
        if not feature_list:
            raise ArchitectureError(
                f"files[{index}] ({class_name}) 이 어떤 feature 도 구현하지 않습니다"
            )
        unknown = [item for item in feature_list if item not in feature_ids]
        if unknown:
            raise ArchitectureError(f"{class_name} 이 존재하지 않는 feature 를 가리킵니다: {unknown}")

        files.append(
            PlannedFile(
                path=path,
                class_name=class_name,
                kind=kind,
                feature_ids=tuple(str(item) for item in feature_list),
                responsibility=_require_str(
                    entry.get("responsibility"), f"files[{index}].responsibility"
                ),
                depends_on=tuple(
                    str(item)
                    for item in _require_list(entry.get("dependsOn"), f"files[{index}].dependsOn")
                ),
            )
        )

    if not files:
        raise ArchitectureError("설계안에 파일이 하나도 없습니다")

    for item in files:
        missing = [path for path in item.depends_on if path not in seen_paths]
        if missing:
            raise ArchitectureError(f"{item.class_name}.dependsOn 이 없는 파일을 가리킵니다: {missing}")

    # 기획이 요구한 기능을 빠뜨리지 않았는가.
    covered = {feature for item in files for feature in item.feature_ids}
    uncovered = [item for item in feature_ids if item not in covered]
    if uncovered:
        raise ArchitectureError(f"어느 파일도 구현하지 않는 feature 가 있습니다: {uncovered}")

    prefabs = _validate_prefabs(raw.get("prefabs"), seen_classes)
    scene = _validate_scene(raw.get("scene"), seen_classes, {item["name"] for item in prefabs})

    _require_every_behaviour_is_attached(files, prefabs, scene)

    notes = [
        _require_str(item, f"notes[{i}]")
        for i, item in enumerate(_require_list(raw.get("notes", []), "notes"))
    ]

    return Design(files=_order_files(files), prefabs=prefabs, scene=scene, notes=notes)


def _validate_prefabs(raw: Any, known_types: set[str]) -> list[dict[str, Any]]:
    prefabs: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, entry in enumerate(_require_list(raw if raw is not None else [], "prefabs")):
        if not isinstance(entry, dict):
            raise ArchitectureError(f"prefabs[{index}] 는 객체여야 합니다")
        name = _require_str(entry.get("name"), f"prefabs[{index}].name")
        if name in names:
            raise ArchitectureError(f"같은 프리팹 이름이 두 번 나왔습니다: {name}")
        names.add(name)
        path = _require_str(entry.get("path"), f"prefabs[{index}].path")
        if not path.startswith("Assets/Prefabs/") or not path.endswith(".prefab"):
            raise ArchitectureError(
                f"프리팹 경로는 Assets/Prefabs/<Name>.prefab 형식이어야 합니다: {path}"
            )
        components = [
            _require_str(item, f"prefabs[{index}].components[{i}]")
            for i, item in enumerate(
                _require_list(entry.get("components"), f"prefabs[{index}].components")
            )
        ]
        prefabs.append(
            {
                "name": name,
                "path": path,
                "components": components,
                "sprite": str(entry.get("sprite") or ""),
                # 우리가 만든 타입과 Unity 내장 컴포넌트를 구분해 둔다. 조립
                # 도구가 전자는 스크립트로, 후자는 내장 타입으로 붙여야 한다.
                "generatedComponents": [item for item in components if item in known_types],
            }
        )
    return prefabs


def _validate_scene(raw: Any, known_types: set[str], prefab_names: set[str]) -> dict[str, Any]:
    if raw is None:
        raise ArchitectureError("scene 이 없습니다")
    if not isinstance(raw, dict):
        raise ArchitectureError("scene 은 객체여야 합니다")

    name = _require_str(raw.get("name"), "scene.name")
    objects: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, entry in enumerate(_require_list(raw.get("objects"), "scene.objects")):
        if not isinstance(entry, dict):
            raise ArchitectureError(f"scene.objects[{index}] 는 객체여야 합니다")
        object_name = _require_str(entry.get("name"), f"scene.objects[{index}].name")
        if object_name in seen:
            raise ArchitectureError(f"같은 씬 오브젝트 이름이 두 번 나왔습니다: {object_name}")
        seen.add(object_name)

        prefab = str(entry.get("prefab") or "")
        if prefab and prefab not in prefab_names:
            raise ArchitectureError(
                f"씬 오브젝트 '{object_name}' 이 없는 프리팹을 가리킵니다: {prefab}"
            )

        components = [
            _require_str(item, f"scene.objects[{index}].components[{i}]")
            for i, item in enumerate(
                _require_list(entry.get("components"), f"scene.objects[{index}].components")
            )
        ]
        scene_object = {
            "name": object_name,
            "parent": str(entry.get("parent") or ""),
            "components": components,
            "prefab": prefab,
            "generatedComponents": [item for item in components if item in known_types],
        }
        if "transform" in entry:
            scene_object["transform"] = _validate_transform(
                entry["transform"], f"scene.objects[{index}].transform"
            )
        objects.append(scene_object)

    for entry in objects:
        parent = entry["parent"]
        if parent and parent not in seen:
            raise ArchitectureError(
                f"씬 오브젝트 '{entry['name']}' 의 부모 '{parent}' 가 씬에 없습니다"
            )

    return {"name": name, "objects": objects}


def _require_every_behaviour_is_attached(
    files: list[PlannedFile], prefabs: list[dict[str, Any]], scene: dict[str, Any]
) -> None:
    """모든 MonoBehaviour 가 프리팹이나 씬 중 한 곳에는 등장해야 한다.

    이 규칙 하나가 이 저장소의 실제 사고를 막는다 — 스크립트 6개 중 5개가 어떤
    GameObject 에도 붙지 않은 채 빌드되는 일을 막기 위한 규칙이다.
    붙일 데가 없는 타입이라면 애초에 MonoBehaviour 가 아니어야 한다.
    """

    attached: set[str] = set()
    for prefab in prefabs:
        attached.update(prefab["components"])
    for entry in scene["objects"]:
        attached.update(entry["components"])

    orphans = [item.class_name for item in files if item.is_attachable and item.class_name not in attached]
    if orphans:
        raise ArchitectureError(
            f"어떤 프리팹·씬 오브젝트에도 붙지 않는 MonoBehaviour 가 있습니다: {orphans}. "
            "붙을 자리를 주거나, GameObject 가 필요 없다면 kind 를 plain/ScriptableObject 로 바꾸세요."
        )
