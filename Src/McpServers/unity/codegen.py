"""완성된 C# 소스를 Unity 저장 경로로 정규화한다.

코드 작성은 호스트 에이전트의 책임이다. 이 모듈은 타입과 경로를 추출할 뿐 모델을
호출하거나 소스를 수정하지 않는다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedScript:
    class_name: str
    path: str
    contents: str
    namespace: str
    types: tuple[str, ...] = ()


class CodeGenerationError(RuntimeError):
    """호스트가 전달한 C#을 정규화할 수 없음."""


def _sanitize_class_name(feature_id: str) -> str:
    parts = re.split(r"[^0-9A-Za-z]+", feature_id)
    name = "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not name or name[0].isdigit():
        name = f"Feature{name}"
    return name


def looks_like_csharp(text: str) -> bool:
    lowered = text.lower()
    return ("class " in lowered or "struct " in lowered) and "{" in text and "}" in text


def _extract_class_name(source: str, fallback: str) -> str:
    match = re.search(r"\b(?:class|struct)\s+([A-Za-z_]\w*)", source)
    return match.group(1) if match else fallback


def extract_declared_types(source: str) -> tuple[str, ...]:
    from project_layout import parse_types

    return tuple(item.name for item in parse_types(source))


class ScriptGenerator:
    """완성된 소스의 타입·네임스페이스·저장 경로를 확정한다."""

    def __init__(self, namespace: str | None = None, script_root: str | None = None) -> None:
        self._namespace = namespace or os.getenv("UNITY_SCRIPT_NAMESPACE", "Game.Gameplay")
        self._script_root = (script_root or os.getenv("UNITY_SCRIPT_ROOT", "Assets/Scripts")).rstrip(
            "/"
        )

    def plan(
        self,
        feature_id: str,
        source: str,
        *,
        planned_path: str = "",
        planned_class: str = "",
    ) -> GeneratedScript:
        if not source.strip() or not looks_like_csharp(source):
            raise CodeGenerationError("contents가 C# 소스로 보이지 않습니다")

        fallback = planned_class or _sanitize_class_name(feature_id)
        class_name = _extract_class_name(source, fallback)
        if planned_class and class_name != planned_class:
            raise CodeGenerationError(
                f"설계 타입과 소스 타입이 다릅니다: {planned_class} != {class_name}"
            )

        path = planned_path or f"{self._script_root}/{class_name}.cs"
        if not path.endswith(".cs") or ".." in path.replace("\\", "/").split("/"):
            raise CodeGenerationError(f"허용되지 않는 C# 경로입니다: {path}")

        return GeneratedScript(
            class_name=class_name,
            path=path,
            contents=source.strip(),
            namespace=self._namespace,
            types=extract_declared_types(source),
        )
