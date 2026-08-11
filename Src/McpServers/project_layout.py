"""생성된 Unity 프로젝트의 **구조**를 판정한다 — Unity Editor 없이, 텍스트만 읽어서.

왜 필요한가. 기존 검사 넷 중 어느 것도 산출물의 구조를 보지 않는다.
``verify_contract.py`` 는 MCP 도구 목록만, ``qa.verify_prototype_structure`` 는
청사진의 ``core_mechanics`` 커버리지만, ``evals/codegen.jsonl`` 은 파일 하나 안의
단어만, ``spec_rules.json`` 은 spec 문서 형식만 본다. 그래서
``Assets/Scripts/Spec001.cs`` ~ ``Spec006.cs`` 가 나오고 그중 다섯 개가 **어떤
GameObject 에도 붙어 있지 않은** 상태로 QA PASS 가 나 태그까지 붙었다
(`Doc/설계/06_코드생성_아키텍처_진단_260730.md` §2.1).

**이 모듈은 판정만 한다.** 출력·종료 코드는 ``verify_project_layout.py`` 가 맡는다.
그래야 같은 판정을 테스트에서도 부를 수 있다.

판정 근거는 전부 텍스트 대조라 Unity 가 필요 없다:

* ``.cs.meta`` 의 ``guid`` ↔ ``.unity``/``.prefab`` 의 ``m_Script: {... guid: ...}``
  → 이 스크립트가 어딘가에 실제로 붙어 있는가
* ``.png.meta`` 의 ``guid`` ↔ 씬·프리팹·에셋 안의 모든 ``guid``
  → 이 그림이 실제로 쓰이는가

Unity 가 meta 파일에 guid 를 적는 형식은 버전과 무관하게 안정적이라, 이 대조는
에디터를 띄우는 것보다 싸고 CI 에서 돈다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from unity.csharp_check import strip_code

# ---------------------------------------------------------------------------
# 규칙 식별자 — 리포트와 테스트가 문자열이 아니라 이 값으로 규칙을 가리킨다.
# ---------------------------------------------------------------------------
RULE_DOCUMENT_NAMED_TYPE = "L1"
RULE_UNATTACHED_BEHAVIOUR = "L2"
RULE_RUNTIME_BOOTSTRAP = "L3"
RULE_FLAT_SCRIPT_ROOT = "L4"
RULE_UNUSED_SPRITE = "L5"
RULE_FILENAME_MISMATCH = "L6"
RULE_ASSEMBLY_CYCLE = "L7"


class Severity(StrEnum):
    """FAIL 은 종료 코드를 바꾸고, WARN 은 보고만 한다."""

    FAIL = "FAIL"
    WARN = "WARN"


# spec-001 → Spec001. 문서 번호가 그대로 타입 이름이 된 흔적.
# ``_sanitize_class_name`` 이 만드는 형태(``Spec001``, ``Feature001``)와, 경로 A 가
# 넘기는 ``{game_id}__spec-001`` 이 변환된 형태(``…Spec001``) 를 함께 잡는다.
DOCUMENT_NAME_PATTERN = re.compile(r"(?:^|[a-z0-9])(?:Spec|Feature|Item)\d{2,}$")

# MonoBehaviour 가 씬·프리팹에 붙을 때 Unity 가 남기는 참조.
_SCRIPT_REFERENCE = re.compile(r"m_Script:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-f]{32})")
_ANY_GUID = re.compile(r"guid:\s*([0-9a-f]{32})")
_META_GUID = re.compile(r"^guid:\s*([0-9a-f]{32})\s*$", re.MULTILINE)

# 타입 선언. ``strip_code`` 로 주석·문자열을 지운 뒤에만 신뢰할 수 있다.
_TYPE_DECLARATION = re.compile(
    r"^[ \t]*(?P<modifiers>(?:(?:public|internal|private|protected|abstract|sealed"
    r"|static|partial|unsafe|new|readonly|ref)[ \t]+)*)"
    r"(?P<kind>class|struct|interface|enum|record)[ \t]+"
    r"(?P<name>[A-Za-z_]\w*)"
    r"(?P<generic><[^>\r\n]*>)?"
    r"(?:[ \t]*:[ \t]*(?P<bases>[^{\r\n]+))?",
    re.MULTILINE,
)

# 조립 도구가 없어서 모델이 코드로 조립을 흉내 낸 흔적. 둘이 같은 파일에 함께
# 있을 때만 신호로 친다 — ``AddComponent`` 하나만으로는 정상적인 쓰임이 많다.
_RUNTIME_SPAWN = re.compile(r"new\s+GameObject\s*\(")
_RUNTIME_ATTACH = re.compile(r"\.AddComponent\s*<")

_SCENE_SUFFIXES = (".unity", ".prefab")
_REFERENCE_SUFFIXES = (".unity", ".prefab", ".asset", ".playable", ".controller")
_SPRITE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tga", ".psd")

# Unity 가 프로젝트를 만들 때 딸려오는 것들. 생성물이 아니므로 판정 대상이 아니다.
_IGNORED_DIRECTORIES = ("Settings", "TutorialInfo", "Samples", "Plugins", "TextMesh Pro")


@dataclass(frozen=True)
class TypeDeclaration:
    """C# 파일이 선언한 타입 하나."""

    name: str
    kind: str
    bases: tuple[str, ...]
    is_public: bool

    @property
    def is_behaviour(self) -> bool:
        """씬이나 프리팹에 붙어야만 실행되는 타입인가.

        직계 부모만 본다. 중간 기반 클래스를 거치는 경우는 놓치지만, 놓치는 쪽이
        멀쩡한 코드를 FAIL 로 막는 것보다 낫다 — 이 검사기의 판정은 사람이 아니라
        파이프라인을 세우는 데 쓰이기 때문이다.
        """

        return "MonoBehaviour" in self.bases


@dataclass(frozen=True)
class ScriptFile:
    """``.cs`` 한 장과 그 ``.cs.meta`` 의 guid."""

    path: Path
    relative: str
    guid: str
    types: tuple[TypeDeclaration, ...]
    source: str

    @property
    def behaviours(self) -> tuple[TypeDeclaration, ...]:
        return tuple(item for item in self.types if item.is_behaviour)


@dataclass(frozen=True)
class Finding:
    """규칙 위반 하나. 사람이 읽을 문장과 기계가 볼 규칙 ID 를 함께 갖는다."""

    rule: str
    severity: Severity
    target: str
    message: str


@dataclass
class LayoutReport:
    """한 프로젝트에 대한 판정 전체."""

    project: str
    scripts: int = 0
    behaviours: int = 0
    prefabs: int = 0
    scenes: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def failures(self) -> list[Finding]:
        return [item for item in self.findings if item.severity is Severity.FAIL]

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.severity is Severity.WARN]

    @property
    def ok(self) -> bool:
        return not self.failures


class ProjectLayoutError(RuntimeError):
    """검사 대상 자체가 잘못됐다 (경로 없음 등). 규칙 위반과 구분한다."""


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------
def _read(path: Path) -> str:
    """Unity 산출물은 UTF-8 이지만, 손으로 만진 파일이 섞여도 검사가 죽지 않게 한다."""

    return path.read_text(encoding="utf-8", errors="replace")


def _is_ignored(relative: str) -> bool:
    parts = Path(relative).parts
    return any(part in _IGNORED_DIRECTORIES for part in parts)


def parse_types(source: str) -> tuple[TypeDeclaration, ...]:
    """소스가 선언한 **모든** 타입. 첫 하나가 아니다.

    첫 하나만 보는 것이 지금 파이프라인의 결함이다 — ``existing_types`` 에 파일당
    한 타입만 실려서 같은 파일 안의 형제 타입이 다음 feature 에게 보이지 않는다
    (§2.1). 검사기는 그 결함을 재현하면 안 되므로 전부 센다.
    """

    code = strip_code(source)
    found: list[TypeDeclaration] = []
    for match in _TYPE_DECLARATION.finditer(code):
        bases_raw = match.group("bases") or ""
        bases = tuple(
            part.strip().split("<")[0].split(".")[-1]
            for part in bases_raw.split(",")
            if part.strip()
        )
        found.append(
            TypeDeclaration(
                name=match.group("name"),
                kind=match.group("kind"),
                bases=bases,
                is_public="public" in (match.group("modifiers") or ""),
            )
        )
    return tuple(found)


def looks_like_runtime_bootstrap(source: str) -> bool:
    """에디터가 할 조립을 코드가 대신하고 있는가.

    ``new GameObject(...)`` 와 ``AddComponent<...>`` 가 **한 파일에 함께** 있을
    때만 참이다. ``AddComponent`` 하나만으로는 이미 있는 오브젝트에 컴포넌트를
    더하는 정상적인 쓰임이 많아서, 그것까지 잡으면 검사기가 시끄러워진다.

    레이아웃 검사기(L3)와 codegen eval 이 같은 답을 내야 하므로 여기 한 곳에만 둔다.
    """

    code = strip_code(source)
    return bool(_RUNTIME_SPAWN.search(code) and _RUNTIME_ATTACH.search(code))


def _meta_guid(meta_path: Path) -> str:
    if not meta_path.is_file():
        return ""
    match = _META_GUID.search(_read(meta_path))
    return match.group(1) if match else ""


def collect_scripts(assets: Path) -> list[ScriptFile]:
    """``Assets/`` 아래 생성된 ``.cs`` 를 전부 읽는다 (Unity 기본 제공물 제외)."""

    scripts: list[ScriptFile] = []
    for path in sorted(assets.rglob("*.cs")):
        relative = path.relative_to(assets).as_posix()
        if _is_ignored(relative):
            continue
        source = _read(path)
        scripts.append(
            ScriptFile(
                path=path,
                relative=relative,
                guid=_meta_guid(path.with_suffix(".cs.meta")),
                types=parse_types(source),
                source=source,
            )
        )
    return scripts


def collect_referenced_guids(assets: Path) -> tuple[set[str], set[str]]:
    """(스크립트로 붙은 guid, 어디서든 참조된 guid) 를 돌려준다.

    앞쪽은 ``m_Script`` 참조만이라 "이 MonoBehaviour 가 실제로 붙어 있는가" 를
    답하고, 뒤쪽은 모든 guid 라 "이 그림이 어디서든 쓰이는가" 를 답한다.
    """

    attached: set[str] = set()
    referenced: set[str] = set()
    for suffix in _REFERENCE_SUFFIXES:
        for path in assets.rglob(f"*{suffix}"):
            relative = path.relative_to(assets).as_posix()
            if _is_ignored(relative):
                continue
            text = _read(path)
            referenced.update(_ANY_GUID.findall(text))
            if suffix in _SCENE_SUFFIXES:
                attached.update(_SCRIPT_REFERENCE.findall(text))
    return attached, referenced


# ---------------------------------------------------------------------------
# 규칙
# ---------------------------------------------------------------------------
def _check_naming(script: ScriptFile) -> list[Finding]:
    """L1/L6 — 이름이 게임 개념에서 왔는가, 파일명과 맞는가."""

    findings: list[Finding] = []
    for declaration in script.types:
        if DOCUMENT_NAME_PATTERN.search(declaration.name):
            findings.append(
                Finding(
                    rule=RULE_DOCUMENT_NAMED_TYPE,
                    severity=Severity.FAIL,
                    target=f"{script.relative}::{declaration.name}",
                    message=(
                        f"타입 이름 '{declaration.name}' 이 spec 문서 번호에서 왔다. "
                        "클래스 이름은 게임 개념(ChunkLoader, PlayerController)이어야 한다."
                    ),
                )
            )

    stem = Path(script.relative).stem
    public_types = [item for item in script.types if item.is_public]
    if public_types and all(item.name != stem for item in public_types):
        findings.append(
            Finding(
                rule=RULE_FILENAME_MISMATCH,
                severity=Severity.FAIL,
                target=script.relative,
                message=(
                    f"파일명 '{stem}' 과 일치하는 public 타입이 없다 "
                    f"(선언된 public 타입: {', '.join(item.name for item in public_types)}). "
                    "Unity 는 MonoBehaviour 를 파일명으로 찾으므로 붙지 않는다."
                ),
            )
        )
    return findings


def _check_attachment(script: ScriptFile, attached: set[str]) -> list[Finding]:
    """L2 — 이 MonoBehaviour 가 씬이나 프리팹 중 한 곳에는 붙어 있는가."""

    if not script.behaviours or not script.guid:
        return []
    if script.guid in attached:
        return []
    names = ", ".join(item.name for item in script.behaviours)
    return [
        Finding(
            rule=RULE_UNATTACHED_BEHAVIOUR,
            severity=Severity.FAIL,
            target=script.relative,
            message=(
                f"MonoBehaviour({names}) 가 어떤 씬·프리팹에도 붙어 있지 않다. "
                "파일은 있지만 게임에서 실행되지 않는다."
            ),
        )
    ]


def _check_runtime_bootstrap(script: ScriptFile) -> list[Finding]:
    """L3 — 에디터가 할 조립을 코드가 대신하고 있는가.

    ``new GameObject(...)`` 와 ``AddComponent<...>`` 가 한 파일에 같이 있으면,
    프리팹·씬으로 구성했어야 할 것을 런타임에 짓고 있다는 신호다. 조립 도구가
    없던 시절의 우회책이라, 도구가 생긴 뒤에도 남아 있으면 설계가 안 옮겨진 것이다.
    """

    if not looks_like_runtime_bootstrap(script.source):
        return []
    return [
        Finding(
            rule=RULE_RUNTIME_BOOTSTRAP,
            severity=Severity.WARN,
            target=script.relative,
            message=(
                "런타임에 GameObject 를 만들어 AddComponent 로 붙이고 있다. "
                "프리팹이나 씬 구성으로 옮길 수 있는지 확인한다."
            ),
        )
    ]


def _check_flat_root(scripts: list[ScriptFile], script_root: str) -> list[Finding]:
    """L4 — ``Assets/Scripts/`` 바로 아래 평면 나열인가 (Folder Rule)."""

    flat = [
        item.relative for item in scripts if Path(item.relative).parent.as_posix() == script_root
    ]
    if len(flat) < 2:
        return []
    return [
        Finding(
            rule=RULE_FLAT_SCRIPT_ROOT,
            severity=Severity.WARN,
            target=script_root,
            message=(
                f"{len(flat)}개 스크립트가 '{script_root}' 바로 아래 평면으로 놓여 있다. "
                "04 명세의 Folder Rule 은 역할별 폴더를 요구한다 "
                "(예: Scripts/World/ChunkLoader.cs)."
            ),
        )
    ]


def _asmdef_reachable(start: str, edges: dict[str, set[str]]) -> set[str]:
    """``start`` 에서 참조로 도달하는 어셈블리 전부."""

    seen: set[str] = set()
    stack = list(edges.get(start, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return seen


def _check_assembly_cycles(assets: Path) -> list[Finding]:
    """L7 — 놓인 ``.asmdef`` 들의 참조에 순환이 있는가.

    순환이 있으면 Unity 는 컴파일을 **통째로** 거부한다. 증상이 "느려진다"가 아니라
    "게임이 안 만들어진다"라서, 빌드 한 바퀴를 쓰기 전에 텍스트로 잡는다.

    ``unity.define_assemblies`` 가 만든 것에는 순환이 생길 수 없다 — 순환이 될 수
    있는 조합을 계획 단계에서 후보에서 빼기 때문이다. 이 검사는 사람이 손으로
    고쳤거나 다른 도구가 놓은 ``.asmdef`` 가 섞였을 때를 위한 것이다.
    """

    owned: dict[str, set[str]] = {}
    for path in sorted(assets.rglob("*.asmdef")):
        if _is_ignored(path.relative_to(assets).as_posix()):
            continue
        try:
            body = json.loads(_read(path))
        except json.JSONDecodeError:
            continue
        name = str(body.get("name") or "")
        if not name:
            continue
        owned[name] = {
            str(item) for item in body.get("references") or [] if not str(item).startswith("GUID:")
        }

    # 우리 어셈블리끼리만 본다 — 패키지 어셈블리는 우리 것을 참조하지 않으므로
    # 순환의 한쪽이 될 수 없다.
    edges = {name: {ref for ref in refs if ref in owned} for name, refs in owned.items()}

    findings: list[Finding] = []
    for name in sorted(edges):
        for other in sorted(_asmdef_reachable(name, edges)):
            if name < other and name in _asmdef_reachable(other, edges):
                findings.append(
                    Finding(
                        rule=RULE_ASSEMBLY_CYCLE,
                        severity=Severity.FAIL,
                        target=f"{name} ↔ {other}",
                        message=(
                            f"어셈블리 '{name}' 과 '{other}' 가 서로를 참조한다. "
                            "Unity 는 순환 참조가 있으면 컴파일을 거부한다 — "
                            "두 폴더를 하나로 합치거나 참조 방향을 한쪽으로 정리해야 한다."
                        ),
                    )
                )
    return findings


def _check_unused_sprites(assets: Path, referenced: set[str]) -> list[Finding]:
    """L5 — 임포트됐지만 아무 데서도 참조되지 않는 그림."""

    findings: list[Finding] = []
    for suffix in _SPRITE_SUFFIXES:
        for path in sorted(assets.rglob(f"*{suffix}")):
            relative = path.relative_to(assets).as_posix()
            if _is_ignored(relative):
                continue
            guid = _meta_guid(path.with_suffix(path.suffix + ".meta"))
            if not guid or guid in referenced:
                continue
            findings.append(
                Finding(
                    rule=RULE_UNUSED_SPRITE,
                    severity=Severity.WARN,
                    target=relative,
                    message=(
                        "임포트됐지만 어떤 씬·프리팹도 이 그림을 참조하지 않는다. "
                        "화면에 나오지 않는다."
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def analyze_project(project_path: Path, script_root: str = "Scripts") -> LayoutReport:
    """Unity 프로젝트 하나를 판정한다. ``project_path`` 는 ``Assets/`` 의 부모다."""

    assets = project_path / "Assets"
    if not assets.is_dir():
        raise ProjectLayoutError(f"Assets 폴더가 없습니다: {assets}")

    scripts = collect_scripts(assets)
    attached, referenced = collect_referenced_guids(assets)

    report = LayoutReport(
        project=project_path.name,
        scripts=len(scripts),
        behaviours=sum(len(item.behaviours) for item in scripts),
        prefabs=sum(1 for _ in assets.rglob("*.prefab")),
        scenes=sum(
            1
            for path in assets.rglob("*.unity")
            if not _is_ignored(path.relative_to(assets).as_posix())
        ),
    )

    for script in scripts:
        report.findings.extend(_check_naming(script))
        report.findings.extend(_check_attachment(script, attached))
        report.findings.extend(_check_runtime_bootstrap(script))

    report.findings.extend(_check_flat_root(scripts, script_root))
    report.findings.extend(_check_unused_sprites(assets, referenced))
    report.findings.extend(_check_assembly_cycles(assets))
    return report


def find_projects(root: Path) -> list[Path]:
    """``root`` 아래에서 Unity 프로젝트(=``Assets/`` 를 가진 폴더)를 찾는다.

    ``git_output/work/<repo>/`` 처럼 한 단계 아래 있는 경우가 많아 재귀로 찾되,
    프로젝트를 하나 찾으면 그 아래로는 더 내려가지 않는다 — Unity 프로젝트 안에
    또 프로젝트가 있을 일은 없고, ``Library/`` 를 훑으면 느려진다.
    """

    if (root / "Assets").is_dir():
        return [root]

    found: list[Path] = []
    for path in sorted(root.iterdir()) if root.is_dir() else []:
        if path.name.startswith(".") or not path.is_dir():
            continue
        found.extend(find_projects(path))
    return found
