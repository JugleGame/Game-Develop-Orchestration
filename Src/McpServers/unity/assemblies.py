"""4단계 — 카테고리 폴더마다 ``.asmdef`` 를 놓아 빌드를 나눈다.

**왜.** 지금은 생성된 C# 이 전부 ``Assembly-CSharp`` 한 덩어리에 들어간다. 그래서
파일 하나를 고쳐도 프로젝트 전체가 다시 컴파일되고, 재시도 루프가 그만큼 느려진다.
``docs/contracts.md``의 Unity 구조 계약이 요구하는 분리다.

**왜 마지막인가.** 어셈블리를 잘못 나누면 순환 참조로 **컴파일이 아예 안 된다** —
증상이 "느려진다"가 아니라 "게임이 안 만들어진다"라서 1~3단계가 실물로 안정된
뒤에 손대도록 순서를 둔다 (``docs/architecture.md``의 종료 조건).

그래서 이 모듈은 **순환을 검출해서 거부하지 않고, 애초에 만들지 않는다.** 그 차이가
이 모듈 설계의 전부다.

``.asmdef`` 의 제약 두 가지가 그 방법을 정한다.

1. **하나의 ``.asmdef`` 는 자기 폴더와 그 하위 전체를 덮는다.** 그러니 서로를
   참조하는 두 형제 폴더(``World`` ↔ ``Player``)를 한 어셈블리로 합칠 수가 없다 —
   파일을 옮기지 않는 한. 합칠 수 없으면 나눌 수도 없다(나누면 순환이다).
2. **``.asmdef`` 어셈블리는 ``Assembly-CSharp`` 를 참조할 수 없다.** 방향이 한쪽
   뿐이다. 그러니 어떤 카테고리를 떼어내려면 **그것이 의존하는 것이 전부 함께**
   떨어져 나와야 한다.

두 제약을 그대로 규칙으로 옮기면 이렇게 된다.

* 서로 물린 카테고리 뭉치(SCC)는 **통째로 건드리지 않는다.**
* 떼어낼 수 있는 것은 **자기가 도달하는 모든 카테고리도 떼어낼 수 있는** 카테고리뿐이다.
* 남은 것은 ``Assembly-CSharp`` 에 그대로 둔다. 나누다 만 상태는 **느릴 뿐 깨지지
  않는다** — 이 모듈이 최악의 경우에도 보장하는 것이 그것이다.

그래서 이 모듈이 내놓는 참조 그래프에는 순환이 **생길 수 없다.** 검사기가 잡아주기
때문이 아니라, 순환을 만들 수 있는 조합이 애초에 후보에서 빠지기 때문이다.

**두 번째 함정 — 패키지 참조.** ``Assembly-CSharp`` 는 프로젝트의 모든 패키지
어셈블리를 자동으로 참조한다. ``.asmdef`` 를 놓는 순간 그 자동 참조가 사라지므로,
``using UnityEngine.InputSystem;`` 한 줄이 들어 있던 파일은 **참조를 명시하지
않으면 컴파일되지 않는다.** 이건 조용한 실패가 아니라 즉시 빌드 실패라서, 소스의
``using`` 을 읽어 :data:`PACKAGE_ASSEMBLIES` 로 되짚는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from project_layout import ScriptFile, collect_scripts
from unity.csharp_check import strip_code

#: ``Assets/Scripts`` 바로 아래에 놓인 파일들. 카테고리가 없으므로 어셈블리로
#: 떼어낼 수 없고, 이것을 참조하는 카테고리도 함께 떼어낼 수 없다.
ROOT_CATEGORY = ""

_USING = re.compile(r"^[ \t]*using[ \t]+(?:static[ \t]+)?([A-Za-z_][\w.]*)[ \t]*;", re.MULTILINE)
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")

#: 네임스페이스 → 그것을 담고 있는 Unity 어셈블리 이름.
#:
#: ``Assembly-CSharp`` 가 공짜로 주던 참조라 ``.asmdef`` 를 놓기 전에는 아무도
#: 신경 쓸 필요가 없었다. 놓는 순간 필수가 된다. 여기 없는 패키지를 쓰는 코드가
#: 나오면 컴파일 오류로 즉시 드러나므로, 그때 한 줄 더하면 된다.
PACKAGE_ASSEMBLIES: dict[str, str] = {
    "UnityEngine.InputSystem": "Unity.InputSystem",
    "UnityEngine.UI": "UnityEngine.UI",
    "UnityEngine.EventSystems": "UnityEngine.UI",
    "UnityEngine.Rendering.Universal": "Unity.RenderPipelines.Universal.Runtime",
    "UnityEngine.U2D.Animation": "Unity.2D.Animation.Runtime",
    "UnityEngine.Timeline": "Unity.Timeline",
    "TMPro": "Unity.TextMeshPro",
    "Cinemachine": "Unity.Cinemachine",
    "Unity.Cinemachine": "Unity.Cinemachine",
    "Unity.Mathematics": "Unity.Mathematics",
    "Unity.Collections": "Unity.Collections",
    "Unity.Burst": "Unity.Burst",
    "Unity.VisualScripting": "Unity.VisualScripting.Core",
    "Newtonsoft.Json": "Unity.Nuget.Newtonsoft-Json",
}


class AssemblyPlanError(RuntimeError):
    """어셈블리 계획을 세울 수 없다 (프로젝트 경로가 틀렸다 등)."""


@dataclass(frozen=True)
class AssemblyPlan:
    """카테고리 하나에 놓을 ``.asmdef`` 한 장."""

    name: str
    category: str
    folder: str
    path: str
    references: tuple[str, ...]
    packages: tuple[str, ...]
    scripts: tuple[str, ...]

    def to_json(self, root_namespace: str) -> str:
        """Unity 가 읽는 ``.asmdef`` 본문.

        키 순서를 고정한다 — 같은 프로젝트를 두 번 돌렸을 때 파일이 바이트까지
        같아야 git 이 의미 없는 변경을 잡지 않는다.
        """

        body = {
            "name": self.name,
            "rootNamespace": root_namespace,
            "references": [*self.references, *self.packages],
            "includePlatforms": [],
            "excludePlatforms": [],
            "allowUnsafeCode": False,
            "overrideReferences": False,
            "precompiledReferences": [],
            "autoReferenced": True,
            "defineConstraints": [],
            "versionDefines": [],
            "noEngineReferences": False,
        }
        return json.dumps(body, indent=4, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class SkippedCategory:
    """떼어내지 않기로 한 카테고리와 그 이유. 조용히 빠지면 안 된다."""

    category: str
    reason: str


@dataclass(frozen=True)
class AssemblyLayout:
    """계획 전체. 쓰기 전에 이 값만 보고도 판단할 수 있어야 한다."""

    assemblies: tuple[AssemblyPlan, ...]
    skipped: tuple[SkippedCategory, ...]
    root_namespace: str


def _category_of(relative: str, script_root: str) -> str:
    """``Scripts/World/Chunks/ChunkLoader.cs`` → ``World``.

    최상위 한 칸만 본다. ``.asmdef`` 가 하위 폴더까지 덮으므로 중첩된 폴더를
    따로 세면 덮이는 범위와 어긋난다.
    """

    parts = Path(relative).parts
    root_parts = Path(script_root).parts
    if parts[: len(root_parts)] != root_parts:
        return ROOT_CATEGORY
    rest = parts[len(root_parts) :]
    return rest[0] if len(rest) > 1 else ROOT_CATEGORY


def _packages_used(source: str) -> set[str]:
    """소스의 ``using`` 에서 필요한 패키지 어셈블리를 되짚는다.

    가장 긴 접두사가 이긴다. ``UnityEngine.UIElements`` 가 ``UnityEngine.UI`` 로
    잘못 잡히지 않도록 경계(``.``)를 요구한다.
    """

    found: set[str] = set()
    for namespace in _USING.findall(source):
        best = ""
        for key in PACKAGE_ASSEMBLIES:
            if namespace == key or namespace.startswith(f"{key}."):
                if len(key) > len(best):
                    best = key
        if best:
            found.add(PACKAGE_ASSEMBLIES[best])
    return found


def _reachable(start: str, edges: dict[str, set[str]]) -> set[str]:
    """``start`` 에서 도달하는 카테고리 전부 (자기 자신은 뺀다)."""

    seen: set[str] = set()
    stack = list(edges.get(start, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    seen.discard(start)
    return seen


def plan_assemblies(
    project_path: Path,
    root_namespace: str = "Game.Gameplay",
    script_root: str = "Scripts",
) -> AssemblyLayout:
    """프로젝트를 읽어 어셈블리 분할을 계획한다. Unity Editor 가 필요 없다.

    **설계안이 아니라 실제 소스를 읽는다.** 설계안의 ``dependsOn`` 은 의도이고,
    컴파일되는 것은 실제로 쓰인 타입이다. 참조를 하나라도 빠뜨리면 컴파일이
    깨지므로, 여기서는 의도가 아니라 결과를 봐야 한다.

    참조 판정은 **넉넉한 쪽으로 틀린다** — 다른 카테고리의 타입 이름이 소스에
    낱말로 등장하면 참조로 친다. 주석이나 문자열 안의 우연한 일치는
    :func:`strip_code` 가 먼저 지운다. 과하게 잡히면 어셈블리가 덜 쪼개질 뿐이고,
    모자라게 잡히면 컴파일이 깨진다.
    """

    assets = project_path / "Assets"
    if not assets.is_dir():
        raise AssemblyPlanError(f"Assets 폴더가 없습니다: {assets}")

    scripts = collect_scripts(assets)
    if not scripts:
        return AssemblyLayout(assemblies=(), skipped=(), root_namespace=root_namespace)

    by_category: dict[str, list[ScriptFile]] = {}
    owner_of_type: dict[str, str] = {}
    for script in scripts:
        category = _category_of(script.relative, script_root)
        by_category.setdefault(category, []).append(script)
        for declaration in script.types:
            owner_of_type[declaration.name] = category

    # 카테고리 사이의 참조 — 실제 소스에 등장한 타입 이름으로 판정한다.
    edges: dict[str, set[str]] = {category: set() for category in by_category}
    packages: dict[str, set[str]] = {category: set() for category in by_category}
    for category, files in by_category.items():
        for script in files:
            packages[category] |= _packages_used(script.source)
            for token in set(_IDENTIFIER.findall(strip_code(script.source))):
                owner = owner_of_type.get(token)
                if owner is not None and owner != category:
                    edges[category].add(owner)

    closure = {category: _reachable(category, edges) for category in by_category}

    # 떼어낼 수 있는 후보 — 카테고리가 있고, 자기와 물린 상대가 없는 것.
    # 서로 도달하면 한 어셈블리여야 하는데 형제 폴더는 한 .asmdef 에 담기지
    # 않으므로, 그 뭉치는 통째로 Assembly-CSharp 에 남긴다.
    skipped: list[SkippedCategory] = []
    candidates: set[str] = set()
    for category in by_category:
        if category == ROOT_CATEGORY:
            continue
        tangled = sorted(
            other for other in closure[category] if category in closure.get(other, set())
        )
        if tangled:
            skipped.append(
                SkippedCategory(
                    category=category,
                    reason=(
                        f"{', '.join(tangled)} 와 서로 참조한다. 한 .asmdef 는 폴더 하나만 "
                        "덮으므로 이 뭉치는 나눌 수 없다 — Assembly-CSharp 에 남긴다."
                    ),
                )
            )
            continue
        candidates.add(category)

    # .asmdef 어셈블리는 Assembly-CSharp 를 참조할 수 없다. 그러니 자기가 도달하는
    # 카테고리가 **전부** 함께 떨어져 나올 때만 떼어낼 수 있다.
    splittable = {category for category in candidates if closure[category] <= candidates}
    for category in sorted(candidates - splittable):
        blockers = sorted(closure[category] - candidates)
        skipped.append(
            SkippedCategory(
                category=category,
                reason=(
                    f"{', '.join(blockers) or 'Assets/Scripts 최상위 파일'} 에 의존하는데 "
                    "그쪽이 Assembly-CSharp 에 남는다. .asmdef 는 Assembly-CSharp 를 "
                    "참조할 수 없어 함께 남긴다."
                ),
            )
        )

    if ROOT_CATEGORY in by_category:
        skipped.append(
            SkippedCategory(
                category="(Assets/Scripts 최상위)",
                reason=(
                    f"{len(by_category[ROOT_CATEGORY])}개 파일이 카테고리 폴더 없이 놓여 있다. "
                    "덮을 폴더가 없어 .asmdef 를 둘 수 없다 — Folder Rule 을 먼저 지켜야 한다."
                ),
            )
        )

    prefix = root_namespace.split(".")[0] or "Game"
    assemblies = tuple(
        AssemblyPlan(
            name=f"{prefix}.{category}",
            category=category,
            folder=f"Assets/{script_root}/{category}",
            path=f"Assets/{script_root}/{category}/{prefix}.{category}.asmdef",
            references=tuple(f"{prefix}.{item}" for item in sorted(edges[category])),
            packages=tuple(sorted(packages[category])),
            scripts=tuple(sorted(item.relative for item in by_category[category])),
        )
        for category in sorted(splittable)
    )

    return AssemblyLayout(
        assemblies=assemblies,
        skipped=tuple(sorted(skipped, key=lambda item: item.category)),
        root_namespace=root_namespace,
    )


# 놓인 ``.asmdef`` 의 참조 순환을 잡는 검사(L7)는 :mod:`project_layout` 에 있다.
# 이 모듈이 그쪽을 import 하므로 반대 방향으로는 둘 수 없고, 애초에 그건 결과물을
# 판정하는 일이라 다른 규칙들과 한자리에 있는 편이 맞다.
