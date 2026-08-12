"""Validate host-authored architecture and C#, then apply it through Unity Editor."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent

from common.errors import MCP_ERROR, UNITY_BUILD_ERROR, VALIDATION_ERROR, tool_error
from common.server import build, expects_dict_return, serve
from project_layout import ProjectLayoutError, analyze_project

from . import assemblies, assembly, csharp_check
from .architecture import ArchitectureError, validate_design
from .assembly import AssemblyError
from .bridge import UnityBridge, UnityBridgeError
from .codegen import CodeGenerationError, ScriptGenerator

mcp = build("UnityMcpServer")

logger = logging.getLogger("UnityMcpServer")

PROJECT_PATH = os.getenv("UNITY_PROJECT_PATH", "")
_bridge = UnityBridge(project_path=PROJECT_PATH)
_generator = ScriptGenerator()


# ---------------------------------------------------------------------------
# 브리지 헬퍼
# ---------------------------------------------------------------------------
async def _ensure_bridge() -> UnityBridge:
    """첫 사용 시 릴레이를 띄운다.

    기동 시점에 붙지 않고 미루는 이유: Unity Editor 가 아직 안 떠 있어도
    이 서버는 뜨고 ``tools/list`` 는 응답해야 하기 때문이다.
    """

    if _bridge._task is None or _bridge._task.done():
        try:
            await _bridge.start()
        except UnityBridgeError as exc:
            raise tool_error(MCP_ERROR, str(exc)) from exc
    return _bridge


def _text_of(result: CallToolResult) -> str:
    return "".join(b.text for b in result.content if isinstance(b, TextContent))


def _payload_of(result: CallToolResult) -> dict[str, Any]:
    """Unity 도구 결과를 dict 로 정규화한다."""

    if result.structured_content is not None:
        return result.structured_content
    text = _text_of(result)
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except ValueError:
        return {"text": text}
    return decoded if isinstance(decoded, dict) else {"result": decoded}


async def _call_unity(
    tool: str, arguments: dict[str, Any], timeout: float = 120.0, build_error: bool = False
) -> dict[str, Any]:
    """Unity 도구를 부르고 실패를 MCP 계약 오류로 정규화한다."""

    bridge = await _ensure_bridge()
    try:
        result = await bridge.call(tool, arguments, timeout=timeout)
    except UnityBridgeError as exc:
        raise tool_error(MCP_ERROR, str(exc), unityTool=tool) from exc
    except TimeoutError as exc:
        raise tool_error(
            MCP_ERROR,
            f"Unity 도구 '{tool}' 가 {timeout}초 안에 응답하지 않았습니다.",
            unityTool=tool,
        ) from exc

    payload = _payload_of(result)
    if result.is_error or payload.get("success") is False:
        message = payload.get("message") or _text_of(result) or f"{tool} failed"
        raise tool_error(
            UNITY_BUILD_ERROR if build_error else MCP_ERROR,
            f"Unity '{tool}' 실패: {message}",
            unityTool=tool,
        )
    return payload


def _require(value: str, field: str) -> str:
    if not value or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value.strip()


async def _run_command(code: str, title: str, timeout: float) -> dict[str, Any]:
    """``Unity_RunCommand`` 로 C# 을 실행하고 **우리가 심은 결과 JSON** 을 읽는다.

    ``_call_unity`` 를 거치지 않는 이유가 하나 있다. ``Unity_RunCommand`` 는 경고만
    있어도 ``errorCode 4000 "Command was executed partially, but reported warnings
    or errors"`` 를 돌려주는데, 그 응답 **안에** 우리 결과 JSON 이 성공으로 들어
    있는 경우가 실제로 있었다 (백로그 「build_project 가 경고를 실패로 오인한다」).
    ``_call_unity`` 는 그 결과를 읽기 전에 예외를 던지므로, 조립 도구는 여기서
    **우리 표식을 먼저 찾고**, 없을 때만 Unity 의 실패 신호를 따른다.

    Unity 패키지가 뱉는 셰이더 경고 때문에 프리팹 저장이 실패로 보고되는 일을
    이 순서가 막는다.
    """

    bridge = await _ensure_bridge()
    try:
        raw = await bridge.call("Unity_RunCommand", {"Code": code, "Title": title}, timeout=timeout)
    except UnityBridgeError as exc:
        raise tool_error(MCP_ERROR, str(exc), unityTool="Unity_RunCommand") from exc
    except TimeoutError as exc:
        raise tool_error(
            MCP_ERROR,
            f"Unity 조립 명령이 {timeout}초 안에 끝나지 않았습니다: {title}",
            unityTool="Unity_RunCommand",
        ) from exc

    payload = _payload_of(raw)
    inner = _extract_command_result(payload)

    if inner.get("success") is True:
        return inner
    if "success" in inner:
        raise tool_error(
            MCP_ERROR,
            f"Unity 조립 실패: {inner.get('message') or title}",
            unityTool="Unity_RunCommand",
            **{key: value for key, value in inner.items() if key not in ("success", "message")},
        )

    # 우리 표식이 아예 없다 — 명령이 컴파일조차 되지 않았을 때의 모습이다.
    message = payload.get("message") or _text_of(raw) or "no result payload"
    raise tool_error(
        MCP_ERROR,
        f"Unity 조립 명령이 결과를 남기지 않았습니다 ({title}): {message}",
        unityTool="Unity_RunCommand",
    )


def _assembly_timeout() -> float:
    return float(os.getenv("UNITY_ASSEMBLY_TIMEOUT", "300"))


#: 컴파일러가 낸 오류. ``error CS0234`` 처럼 코드가 붙는다.
_COMPILER_ERROR = re.compile(r"\berror\s+CS\d+\b")

#: 런타임 예외. 콘솔 첫 줄이 ``NullReferenceException: ...`` 형태다.
#: 콜론을 요구하는 이유 — 그냥 ``Exception`` 이라는 낱말이 들어간 평범한 로그
#: ("No Exception occurred")까지 오류로 세면 멀쩡한 빌드가 FAIL 이 된다.
_RUNTIME_ERROR = re.compile(r"\b\w*Exception\s*:")

#: 콘솔 항목의 ``Type`` 이 제대로 왔을 때 오류로 치는 값들.
_ERROR_TYPES = frozenset({"error", "exception", "assert"})


async def _read_error_console(count: int = 200) -> list[dict[str, Any]]:
    """콘솔에서 **오류만** 걷어온다.

    ``Unity_ReadConsole`` 에 ``Types: ["Error"]`` 를 주면 **컴파일 오류가 0건으로
    온다.** 이 브리지가 컴파일 오류를 ``Type: "Log"`` 로 실어 보내기 때문이다 —
    실측으로 확인했다 (2026-07-31). 같은 순간 ``Types: ["All"]`` 로 읽으면 그
    오류가 나온다::

        Types ["Error"] → 0건
        Types ["All"]   → 1건, {"Type": "Log", "Message": "... error CS0234 ..."}

    그래서 **전부 읽고 우리가 분류한다.** ``Type`` 이 맞게 왔으면 그것을 믿고,
    아니면 메시지 모양으로 판정한다. 이 필터가 조용히 비면 재시도 루프는 고칠
    것이 없다고 판단할 수 있다. ``docs/contracts.md``가 요구하는
    "잘못된 결과가 합격으로 기록되는" 경로가 정확히 이것이다.
    """

    console = await _call_unity(
        "Unity_ReadConsole",
        {
            "Action": "Get",
            "Types": ["All"],
            "Count": count,
            "Format": "Json",
            "IncludeStacktrace": True,
        },
        timeout=60,
    )

    entries = console.get("data") or []
    if not isinstance(entries, list):
        return []

    found: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("Type") or entry.get("type") or "").strip().lower()
        message = str(entry.get("Message") or entry.get("message") or "")
        if (
            kind in _ERROR_TYPES
            or _COMPILER_ERROR.search(message)
            or _RUNTIME_ERROR.search(message)
        ):
            found.append(entry)
    return found


# ---------------------------------------------------------------------------
# Agent-first MCP 도구
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "Validate the host-authored file, type, prefab, and scene plan for one game. "
        "Call once per game."
    )
)
@expects_dict_return
async def design_architecture(
    gameId: str,
    featurePrompts: list[dict[str, Any]],
    design: dict[str, Any],
) -> dict[str, Any]:
    """기획의 기능 경계를 호스트가 정한 코드 경계로 검증한다."""

    gameId = _require(gameId, "gameId")
    prompts = featurePrompts or []
    if not prompts:
        raise tool_error(VALIDATION_ERROR, "featurePrompts 가 비어 있습니다", gameId=gameId)

    missing = [index for index, item in enumerate(prompts) if not item.get("feature_id")]
    if missing:
        raise tool_error(
            VALIDATION_ERROR,
            f"featurePrompts[{missing}] 에 feature_id 가 없습니다",
            gameId=gameId,
        )
    feature_ids = [str(item["feature_id"]) for item in prompts]

    try:
        planned = validate_design(design, feature_ids)
    except ArchitectureError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=gameId) from exc

    logger.info(
        "Architecture designed",
        extra={
            "game_id": gameId,
            "files": len(planned.files),
            "prefabs": len(planned.prefabs),
            "scene_objects": len(planned.scene.get("objects", [])),
            "supplied": True,
        },
    )

    body = planned.to_dict()
    body["gameId"] = gameId
    body["typeMap"] = planned.type_map()
    return body


@mcp.tool(description="Validate host-authored C#, then create or update its Unity script.")
@expects_dict_return
async def create_script(
    featureId: str,
    contents: str,
    plannedPath: str = "",
    plannedClass: str = "",
    update: bool = False,
) -> dict[str, Any]:
    """완성된 C#을 정적 검사한 뒤 Unity에 반영한다."""

    featureId = _require(featureId, "featureId")

    try:
        script = _generator.plan(
            featureId,
            _require(contents, "contents"),
            planned_path=plannedPath,
            planned_class=plannedClass,
        )
    except CodeGenerationError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), featureId=featureId) from exc

    gate = csharp_check.check(script.contents)

    if not gate.ok:
        # Unity 를 건드리기 전에 멈춘다. 여기서 통과시키면 이 오류를 확인하는
        # 데 빌드 한 바퀴(기본 예산 1800초)를 쓰게 된다.
        raise tool_error(
            VALIDATION_ERROR,
            f"C# 문법 검사 실패 ({gate.checked_by}): {gate.summary()}",
            featureId=featureId,
            syntaxErrors=list(gate.errors),
        )

    if update:
        # A retry repairing an existing file. Unity_CreateScript has no update
        # capability at all — its schema (Path/Contents/ScriptType/Namespace)
        # carries no Action field, so an "Action": "Update" key here is simply
        # ignored and Unity rejects the call with "Script already exists ...
        # Use 'update' action to modify" (백로그 「unity 서버 create_script 에
        # 갱신 경로가 없다」). Unity_ManageScript is the router that actually
        # supports action="update" for an existing file, but its argument
        # shape is unrelated (lowercase keys, name/path split instead of a
        # single Path).
        await _call_unity(
            "Unity_ManageScript",
            {
                "action": "update",
                "name": Path(script.path).stem,
                "path": str(Path(script.path).parent.as_posix()),
                "contents": script.contents,
                "script_type": "MonoBehaviour",
                "namespace": script.namespace,
            },
            timeout=120,
        )
    else:
        create_args: dict[str, Any] = {
            "Path": script.path,
            "Contents": script.contents,
            "ScriptType": "MonoBehaviour",
            "Namespace": script.namespace,
        }
        await _call_unity("Unity_CreateScript", create_args, timeout=120)

    diagnostics: dict[str, Any] = {}
    try:
        diagnostics = await _call_unity(
            "Unity_ValidateScript",
            {"Uri": script.path, "Level": "basic", "IncludeDiagnostics": True},
            timeout=60,
        )
    except Exception:  # noqa: BLE001 — 검증 실패가 생성 성공을 뒤집지는 않는다
        diagnostics = {"validated": False}

    # 호스트가 안정적으로 파일을 찾도록 계약의 ``file``을 항상 채운다.
    body: dict[str, Any] = {
        "file": script.path,
        "className": script.class_name,
        "types": list(script.types),
        "namespace": script.namespace,
        "featureId": featureId,
        "validation": diagnostics,
        "syntaxGate": {
            "ok": gate.ok,
            "checkedBy": gate.checked_by,
            "repaired": False,
        },
        "contents": script.contents,
    }
    return body


@mcp.tool(description="Create an empty Unity scene.")
@expects_dict_return
async def create_scene(featureId: str, sceneName: str) -> dict[str, Any]:
    featureId = _require(featureId, "featureId")
    sceneName = _require(sceneName, "sceneName")

    payload = await _call_unity(
        "Unity_ManageScene",
        {
            "Action": "Create",
            "Name": sceneName,
            "Path": os.getenv("UNITY_SCENE_ROOT", "Assets/Scenes"),
        },
        timeout=120,
    )
    return {
        "scene": f"{os.getenv('UNITY_SCENE_ROOT', 'Assets/Scenes')}/{sceneName}.unity",
        "featureId": featureId,
        "unity": payload,
    }


@mcp.tool(description="Import a generated asset into the Unity project.")
@expects_dict_return
async def import_asset(featureId: str, assetPath: str) -> dict[str, Any]:
    """AssetGenMcpServer 가 만든 파일을 Unity 가 인식하게 만든다.

    Unity 는 ``Assets/`` 아래만 임포트할 수 있으므로, 프로젝트 밖 경로는
    먼저 복사한 뒤 임포트한다.
    """

    featureId = _require(featureId, "featureId")
    assetPath = _require(assetPath, "assetPath")

    project_assets = os.path.join(PROJECT_PATH, "Assets")
    unity_path = assetPath.replace("\\", "/")

    if not unity_path.startswith("Assets/"):
        import shutil
        from pathlib import Path

        source = Path(assetPath)
        if not source.exists():
            raise tool_error(
                VALIDATION_ERROR, f"에셋 파일이 없습니다: {assetPath}", featureId=featureId
            )
        target_dir = Path(project_assets) / os.getenv("UNITY_IMPORT_SUBDIR", "Generated")
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_feature_id = re.sub(r"[^A-Za-z0-9_-]", "_", featureId)
        target_name = f"{safe_feature_id}_{source.name}"
        target = target_dir / target_name
        shutil.copy2(source, target)
        unity_path = f"Assets/{os.getenv('UNITY_IMPORT_SUBDIR', 'Generated')}/{target_name}"

    payload = await _call_unity(
        "Unity_ManageAsset",
        {"Action": "Import", "Path": unity_path, "GeneratePreview": False},
        timeout=120,
    )
    return {"imported": unity_path, "featureId": featureId, "unity": payload}


# ---------------------------------------------------------------------------
# 조립 3종 — ``docs/contracts.md``의 설계안을 Unity에서 실행한다.
#
# 이 셋은 새로 판단하지 않는다. ``design_architecture`` 의 ``prefabs[]`` 가
# ``create_prefab`` 의 인자이고 ``scene`` 이 ``compose_scene`` 의 인자다. 그래서
# 같은 설계안이면 조립 결과가 실행마다 같다.
#
# 이 셋이 생기기 전에는 모델이 런타임 ``new GameObject`` + ``AddComponent`` 로
# 조립을 흉내 내는 런타임 부트스트랩이 생길 수 있었다. 이 도구로 그 우회를 막는다.
# 않는 것이 3단계 성공의 신호다.
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "Create one planned prefab, attach its components, and save it under Assets/Prefabs. "
        "Requires a running Unity Editor."
    )
)
@expects_dict_return
async def create_prefab(
    gameId: str,
    prefabName: str,
    components: list[str] | None = None,
    sprite: str = "",
    prefabPath: str = "",
) -> dict[str, Any]:
    """``design_architecture`` 의 ``prefabs[]`` 한 항목을 그대로 받는다.

    반복 등장하는 것(적·아이템·발사체)은 프리팹이어야 한다 — 04 명세의
    **Prefab Rule** 이다. 지금까지는 이 규칙을 이행할 도구 자체가 없어서
    준수 여부를 판정할 수 없었다.

    ``components`` 에는 우리가 만든 타입과 Unity 내장 타입을 섞어 넣어도 된다.
    이름으로 못 찾은 것은 실패가 아니라 ``missing`` 으로 보고된다 — 아직
    컴파일되지 않은 스크립트를 붙이려 한 경우가 대부분이라, 그 사실을 알려주는
    쪽이 프리팹 생성을 통째로 되돌리는 것보다 낫다.
    """

    gameId = _require(gameId, "gameId")
    root = os.getenv("UNITY_PREFAB_ROOT", "Assets/Prefabs").rstrip("/")

    try:
        name = assembly.require_name(prefabName, "prefabName")
        path = assembly.require_asset_path(
            prefabPath or f"{root}/{name}.prefab", "prefabPath", suffix=".prefab"
        )
        types = [
            assembly.require_type_name(item, f"components[{index}]")
            for index, item in enumerate(components or [])
        ]
        image = assembly.require_asset_path(sprite, "sprite") if str(sprite).strip() else ""
    except AssemblyError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=gameId) from exc

    inner = await _run_command(
        assembly.prefab_command(name, path, types, image),
        f"AutoGen prefab {name}",
        _assembly_timeout(),
    )

    logger.info(
        "Prefab created",
        extra={"game_id": gameId, "prefab": path, "components": len(types)},
    )
    return {
        "prefab": inner.get("prefab", path),
        "gameId": gameId,
        "attached": inner.get("attached", []),
        "missing": inner.get("missing", []),
    }


@mcp.tool(
    description=(
        "Create the planned scene hierarchy, attach components, save it, and register it in "
        "build settings. Requires a running Unity Editor."
    )
)
@expects_dict_return
async def compose_scene(
    gameId: str, sceneName: str, objects: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """``design_architecture`` 의 ``scene`` 을 그대로 받는다.

    ``create_scene`` 이 만드는 것은 빈 씬이다. 지금까지 파이프라인 7단계
    「씬 구성」의 실제 도구가 그것뿐이라, 스크립트가 어디에도 붙지 않은 채
    빌드까지 갈 수 있었다. 이 도구가 그 자리를 채운다.

    부모가 자식보다 먼저 만들어지도록 순서를 여기서 확정한다 — 설계 검증은
    "부모가 씬에 있는가"만 보고 순서는 보지 않기 때문이다.
    """

    gameId = _require(gameId, "gameId")
    scene_root = os.getenv("UNITY_SCENE_ROOT", "Assets/Scenes").rstrip("/")

    try:
        name = assembly.require_name(sceneName, "sceneName")
        scene_path = assembly.require_asset_path(
            f"{scene_root}/{name}.unity", "sceneName", suffix=".unity"
        )
        entries = assembly.order_objects(list(objects or []))
        for index, entry in enumerate(entries):
            assembly.require_name(str(entry.get("name") or ""), f"objects[{index}].name")
            if entry.get("parent"):
                assembly.require_name(str(entry["parent"]), f"objects[{index}].parent")
            if entry.get("prefab"):
                assembly.require_asset_path(
                    str(entry["prefab"]), f"objects[{index}].prefab", suffix=".prefab"
                )
            for position, item in enumerate(entry.get("components") or []):
                assembly.require_type_name(str(item), f"objects[{index}].components[{position}]")
    except AssemblyError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=gameId) from exc

    if not entries:
        raise tool_error(VALIDATION_ERROR, "objects 가 비어 있습니다", gameId=gameId)

    inner = await _run_command(
        assembly.scene_command(scene_path, entries),
        f"AutoGen scene {name}",
        _assembly_timeout(),
    )

    logger.info(
        "Scene composed",
        extra={"game_id": gameId, "scene": scene_path, "objects": len(entries)},
    )
    return {
        "scene": inner.get("scene", scene_path),
        "gameId": gameId,
        "objects": inner.get("objects", len(entries)),
        "attached": inner.get("attached", []),
        "missing": inner.get("missing", []),
    }


@mcp.tool(
    description=(
        "Bind a sprite or prefab to a serialized Inspector field. Target is a prefab path or "
        "a scene hierarchy path."
    )
)
@expects_dict_return
async def bind_reference(
    gameId: str, target: str, field: str, value: str, scene: str = ""
) -> dict[str, Any]:
    """생성된 그림을 실제로 **화면에 나오게** 만드는 단계다.

    ``import_asset`` 은 임포트만 하고 참조를 꽂지 않는다. 그래서 그림 아홉 장을
    만들어 놓고 그것을 참조하는 코드가 0줄인 상태를 막는다.

    ``field`` 는 ``"enemyPrefab"`` 처럼 필드 이름만 주거나
    ``"EnemySpawner.enemyPrefab"`` 처럼 컴포넌트까지 못박을 수 있다. 못박는 쪽이
    안전하다 — 이름만 주면 같은 이름의 직렬화 필드를 가진 첫 컴포넌트에 꽂힌다.
    """

    gameId = _require(gameId, "gameId")
    scene_root = os.getenv("UNITY_SCENE_ROOT", "Assets/Scenes").rstrip("/")

    try:
        if target.strip().endswith(".prefab"):
            resolved = assembly.require_asset_path(target, "target", suffix=".prefab")
            scene_path = ""
        else:
            resolved = assembly.require_object_path(target, "target")
            scene_path = (
                assembly.require_asset_path(
                    f"{scene_root}/{assembly.require_name(scene, 'scene')}.unity",
                    "scene",
                    suffix=".unity",
                )
                if str(scene).strip()
                else ""
            )
        type_name, field_name = assembly.split_field(field)
        value_path = assembly.require_asset_path(value, "value")
    except AssemblyError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=gameId) from exc

    inner = await _run_command(
        assembly.bind_command(resolved, type_name, field_name, value_path, scene_path),
        f"AutoGen bind {resolved}.{field_name}",
        _assembly_timeout(),
    )

    logger.info(
        "Reference bound",
        extra={"game_id": gameId, "target": resolved, "field": field_name},
    )
    return {
        "bound": True,
        "gameId": gameId,
        "target": resolved,
        "field": field_name,
        "component": inner.get("component", type_name),
        "value": value_path,
    }


# ---------------------------------------------------------------------------
# 마지막 단계 — 어셈블리 분리 (``docs/architecture.md``의 종료 조건)
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "Plan or create per-category .asmdef files to reduce recompilation. Skip any split that "
        "could introduce a dependency cycle."
    )
)
@expects_dict_return
async def define_assemblies(
    gameId: str, projectPath: str = "", apply: bool = True
) -> dict[str, Any]:
    """04 명세의 **Assembly Rule** 을 이행한다.

    지금은 생성된 C# 이 전부 ``Assembly-CSharp`` 한 덩어리라, 파일 하나를 고쳐도
    프로젝트 전체가 다시 컴파일된다. 재시도 루프가 그만큼 느리다.

    **이 도구는 나눌 수 없는 것을 나누지 않는다.** 어셈블리를 잘못 나누면 순환
    참조로 컴파일이 통째로 막히는데, 그건 "느려진다"가 아니라 "게임이 안 만들어
    진다"라서 되돌리기가 비싸다. 그래서 ``unity/assemblies.py`` 는 순환이 생길
    수 있는 카테고리를 **후보에서 빼고**, 남은 것은 ``Assembly-CSharp`` 에 그대로
    둔다. 덜 나뉜 상태는 느릴 뿐 깨지지 않는다.

    빠진 것은 ``skipped`` 로 **이유와 함께** 돌려준다. 조용히 빠지면 왜 안 빨라
    졌는지 알 수 없기 때문이다.

    ``apply=False`` 면 계획만 돌려주고 파일을 쓰지 않는다. ``.asmdef`` 를 놓는
    것은 프로젝트 전체의 컴파일 단위를 바꾸는 일이라, 먼저 보고 나서 쓸 수 있어야
    한다.
    """

    gameId = _require(gameId, "gameId")
    root = projectPath.strip() or PROJECT_PATH
    if not root:
        raise tool_error(
            VALIDATION_ERROR,
            "Unity 프로젝트 경로를 알 수 없습니다. UNITY_PROJECT_PATH 를 설정하거나 "
            "projectPath 인자를 넘기세요.",
            gameId=gameId,
        )

    namespace = os.getenv("UNITY_SCRIPT_NAMESPACE", "Game.Gameplay")
    try:
        layout = assemblies.plan_assemblies(Path(root), root_namespace=namespace)
    except assemblies.AssemblyPlanError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=gameId, projectPath=root) from exc

    written: list[str] = []
    if apply:
        for plan in layout.assemblies:
            target = Path(root) / plan.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plan.to_json(layout.root_namespace), encoding="utf-8")
            written.append(plan.path)

        # 파일만 놓으면 Unity 는 모른다. 임포트해야 컴파일 단위가 실제로 갈린다.
        for path in written:
            await _call_unity(
                "Unity_ManageAsset",
                {"Action": "Import", "Path": path, "GeneratePreview": False},
                timeout=120,
            )

    logger.info(
        "Assemblies defined",
        extra={
            "game_id": gameId,
            "assemblies": len(layout.assemblies),
            "skipped": len(layout.skipped),
            "applied": apply,
        },
    )
    return {
        "gameId": gameId,
        "projectPath": root,
        "applied": apply,
        "assemblies": [
            {
                "name": plan.name,
                "folder": plan.folder,
                "path": plan.path,
                "references": list(plan.references),
                "packages": list(plan.packages),
                "scripts": list(plan.scripts),
            }
            for plan in layout.assemblies
        ],
        "skipped": [{"category": item.category, "reason": item.reason} for item in layout.skipped],
        "written": written,
    }


# ``Unity_RunCommand`` 로 실행할 빌드 스크립트. Unity 에 전용 빌드 도구가
# 없으므로 BuildPipeline 을 직접 호출한다.
#
# 이 코드는 ``Unity_RunCommand`` 의 계약을 정확히 지켜야 한다 — 클래스명은
# ``CommandScript``, 접근성은 ``internal``, 진입점은 ``Execute(ExecutionResult)``.
# 다른 이름·접근성으로 보내면 Unity 가 로그도 남기지 않고 실행에 실패한다
# ("No logs available"). 또한 Unity 는 이 코드를 ``Unity.AI...Editor``
# 네임스페이스로 감싸므로, ``Unity.*`` 와 이름이 겹칠 수 있는 타입은 모두
# ``global::`` 로 못박는다.
#
# 결과 JSON 은 ``result.Log`` 로 흘려보낸다. ``_extract_build_result`` 가 응답의
# ``data.executionLogs`` 에서 그 JSON 을 찾아낸다.
_BUILD_CSHARP = """
using UnityEngine;

internal class CommandScript : IRunCommand
{
    public void Execute(ExecutionResult result)
    {
        var scenes = new System.Collections.Generic.List<string>();
        foreach (var entry in global::UnityEditor.EditorBuildSettings.scenes)
        {
            if (entry.enabled)
            {
                scenes.Add(entry.path);
            }
        }

        if (scenes.Count == 0)
        {
            result.Log("BUILD_RESULT {0}",
                "{\\"success\\":false,\\"message\\":\\"No enabled scenes in Build Settings.\\"}");
            return;
        }

        var options = new global::UnityEditor.BuildPlayerOptions
        {
            scenes = scenes.ToArray(),
            locationPathName = @"__OUTPUT__",
            target = global::UnityEditor.BuildTarget.__TARGET__,
            options = global::UnityEditor.BuildOptions.Development
        };

        var report = global::UnityEditor.BuildPipeline.BuildPlayer(options);
        var summary = report.summary;
        bool ok = summary.result == global::UnityEditor.Build.Reporting.BuildResult.Succeeded;

        string payload = "{\\"success\\":" + (ok ? "true" : "false")
            + ",\\"result\\":\\"" + summary.result + "\\""
            + ",\\"totalErrors\\":" + summary.totalErrors
            + ",\\"totalWarnings\\":" + summary.totalWarnings
            + ",\\"outputPath\\":\\"" + summary.outputPath.Replace("\\\\", "/") + "\\""
            + ",\\"durationSeconds\\":" + (int)summary.totalTime.TotalSeconds
            + "}";

        result.Log("BUILD_RESULT {0}", payload);
    }
}
"""

_BUILD_TARGET_DEFAULT_OUTPUTS = {
    "WebGL": "Builds/{gameId}",
    "StandaloneWindows64": "Builds/{gameId}/game.exe",
}
_BUILD_OUTPUT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")


def _build_configuration(game_id: str) -> tuple[str, str]:
    """Return a C#-safe Unity build target and project-relative output path."""

    target = os.getenv("UNITY_BUILD_TARGET", "WebGL").strip() or "WebGL"
    if target not in _BUILD_TARGET_DEFAULT_OUTPUTS:
        allowed = ", ".join(_BUILD_TARGET_DEFAULT_OUTPUTS)
        raise tool_error(
            VALIDATION_ERROR,
            f"UNITY_BUILD_TARGET must be one of: {allowed}.",
        )

    configured = os.getenv("UNITY_BUILD_OUTPUT", "").strip()
    output = configured or _BUILD_TARGET_DEFAULT_OUTPUTS[target].format(gameId=game_id)
    normalized = output.replace("\\", "/")
    parts = Path(normalized).parts
    if (
        not _BUILD_OUTPUT_PATTERN.fullmatch(normalized)
        or Path(normalized).is_absolute()
        or ".." in parts
        or not normalized.startswith("Builds/")
    ):
        raise tool_error(
            VALIDATION_ERROR,
            "UNITY_BUILD_OUTPUT must be a safe project-relative path under Builds/.",
        )
    return target, normalized


@mcp.tool(description="Build the Unity project through BuildPipeline.")
@expects_dict_return
async def build_project(gameId: str) -> dict[str, Any]:
    """Unity 에는 빌드 도구가 없어 ``Unity_RunCommand`` 로 C# 을 실행한다.

    빌드 타깃과 출력 경로는 환경변수로 조정한다
    (``UNITY_BUILD_TARGET``, ``UNITY_BUILD_OUTPUT``).
    """

    gameId = _require(gameId, "gameId")

    target, output = _build_configuration(gameId)
    code = _BUILD_CSHARP.replace("__TARGET__", target).replace("__OUTPUT__", output)

    payload = await _call_unity(
        "Unity_RunCommand",
        {"Code": code, "Title": f"AutoGen build {gameId}"},
        timeout=float(os.getenv("UNITY_BUILD_TIMEOUT", "1800")),
        build_error=True,
    )

    # RunCommand 는 실행 결과를 텍스트로 돌려준다. 우리 JSON 을 찾아 꺼낸다.
    inner = _extract_build_result(payload)
    if inner.get("success") is False:
        raise tool_error(
            UNITY_BUILD_ERROR,
            f"Unity 빌드 실패: {inner.get('message') or inner.get('result')}",
            gameId=gameId,
            **{k: v for k, v in inner.items() if k != "success"},
        )

    return {
        "buildId": f"{gameId}-{inner.get('durationSeconds', 0)}",
        "gameId": gameId,
        "artifactPath": inner.get("outputPath", output),
        # 배포 대상은 빌드 바이너리가 아니라 소스다 (검토서 §3.2 "배포용 코드").
        # GitMcpServer 의 git_commit(sourcePath=...) 가 이 값을 받아 프로젝트를
        # 저장소에 동기화하므로, 여기서 노출하지 않으면 오케스트레이터는
        # 환경변수 GIT_SOURCE_PATH 에 의존할 수밖에 없다 (§05 §1.6 요청 사항).
        "projectPath": PROJECT_PATH,
        "target": target,
        "totalErrors": inner.get("totalErrors", 0),
        "totalWarnings": inner.get("totalWarnings", 0),
        "unity": payload,
    }


#: 빌드 JSON 이 실려 올 수 있는 키. ``executionLogs``/``data`` 는 ``Unity_RunCommand``
#: 가 실행 로그를 한 단계 안쪽에 담아 돌려주기 때문에 필요하다 — 최상위만 훑으면
#: 빌드가 성공해도 결과를 못 읽고 기본값을 보고하게 된다.
_BUILD_RESULT_KEYS = (
    "result",
    "returnValue",
    "output",
    "logs",
    "text",
    "message",
    "executionLogs",
    "data",
)


def _extract_command_result(payload: dict[str, Any]) -> dict[str, Any]:
    """RunCommand 응답 어딘가에 박혀 있는 ``{"success": ...}`` JSON 을 찾아낸다.

    빌드와 조립이 같은 방식으로 결과를 실어 보내므로(``result.Log`` 에 JSON 한 줄)
    찾는 코드도 하나다. 두 벌을 두면 한쪽만 고쳐진 채 남는다.
    """

    found = _search_build_result(payload, depth=0)
    return found if found is not None else payload


#: 빌드 경로에서 쓰던 이름. 부르는 곳과 테스트가 이 이름을 알고 있어 남겨 둔다.
_extract_build_result = _extract_command_result


def _search_build_result(node: Any, depth: int) -> dict[str, Any] | None:
    """빌드 JSON 을 담을 수 있는 값들을 깊이 제한을 두고 훑는다."""

    if depth > 3 or not isinstance(node, dict):
        return None

    for key in _BUILD_RESULT_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            if "success" in value:
                return value
            nested = _search_build_result(value, depth + 1)
            if nested is not None:
                return nested
        elif isinstance(value, str) and '"success"' in value:
            decoded = _decode_embedded_json(value)
            if decoded is not None:
                return decoded
    return None


def _decode_embedded_json(value: str) -> dict[str, Any] | None:
    """문자열 안에 박힌 첫 번째 빌드 JSON 객체를 꺼낸다."""

    start = value.find("{")
    while start != -1:
        try:
            decoded, _ = json.JSONDecoder().raw_decode(value, start)
        except ValueError:
            start = value.find("{", start + 1)
            continue
        if isinstance(decoded, dict) and "success" in decoded:
            return decoded
        start = value.find("{", start + 1)
    return None


@mcp.tool(description="Run PlayMode and collect runtime errors.")
@expects_dict_return
async def run_playmode_test(gameId: str) -> dict[str, Any]:
    """Editor 를 Play 로 전환했다가 멈추고 그 사이의 콘솔을 걷어온다."""

    gameId = _require(gameId, "gameId")
    duration = float(os.getenv("UNITY_PLAYMODE_SECONDS", "10"))

    await _call_unity("Unity_ReadConsole", {"Action": "Clear"}, timeout=30)
    await _call_unity(
        "Unity_ManageEditor", {"Action": "Play", "WaitForCompletion": True}, timeout=120
    )

    import asyncio

    await asyncio.sleep(duration)

    await _call_unity(
        "Unity_ManageEditor", {"Action": "Stop", "WaitForCompletion": True}, timeout=120
    )
    errors = await _read_error_console()
    return {
        "gameId": gameId,
        "passed": len(errors) == 0,
        "durationSeconds": duration,
        "errorCount": len(errors),
        "errors": errors,
    }


@mcp.tool(description="Return errors from the latest compile or run.")
@expects_dict_return
async def get_compile_errors(gameId: str) -> dict[str, Any]:
    """호스트는 ``body["errors"]``를 ``CompileError`` 증거로 읽는다.

    ``CompileError`` 는 ``{file, line, message}`` 이므로 Unity 콘솔 항목을
    그 모양으로 변환해서 돌려준다.
    """

    gameId = _require(gameId, "gameId")

    errors = [_to_compile_error(entry) for entry in await _read_error_console()]
    return {"gameId": gameId, "errors": errors}


def _to_compile_error(entry: Any) -> dict[str, Any]:
    """Unity 콘솔 항목 → §5 CompileError ``{file, line, message}``.

    ``file`` 은 필수(str)라 알 수 없을 때도 빈 문자열이 아닌 표식을 넣는다 —
    빈 값이면 오케스트레이터가 어떤 파일을 다시 만들지 판단할 수 없다.
    """

    if not isinstance(entry, dict):
        return {"file": "unknown", "line": None, "message": str(entry)}

    message = entry.get("message") or entry.get("Message") or ""
    file = entry.get("file") or entry.get("File") or ""
    line = entry.get("line") or entry.get("Line")

    if not file:
        # "Assets/Scripts/Player.cs(12,5): error CS0103: ..." 형태를 파싱한다.
        match = re.search(r"([\w/\\.\-]+\.cs)\((\d+),\d+\)", message)
        if match:
            file, line = match.group(1), int(match.group(2))

    return {
        "file": file or "unknown",
        "line": int(line) if isinstance(line, (int, str)) and str(line).isdigit() else None,
        "message": message.strip() or "(no message)",
    }


@mcp.tool(
    description=(
        "Inspect script attachment, naming, and folder structure from project files. "
        "Does not require Unity Editor."
    )
)
@expects_dict_return
async def inspect_project_layout(gameId: str = "", projectPath: str = "") -> dict[str, Any]:
    """QA 가 **구조**를 판정할 근거를 만든다.

    왜 필요한가. QA 에 넘어가는 ``build`` 문자열은 빌드 메타데이터와 플레이모드
    결과뿐이라, 판정자가 "이 스크립트가 어디에 붙어 있는가" 를 알 방법이 없었다.
    그래서 ``verify_prototype_structure`` 는 이름과 달리 ``core_mechanics``
    커버리지만 봤고, 여섯 스크립트 중 다섯이 어디에도 안 붙은 프로토타입이
    통과하는 일을 막기 위한 구조 검사다.

    Unity Editor 가 필요 없다 — ``.cs.meta`` 의 guid 와 ``.unity``/``.prefab`` 의
    ``m_Script`` 참조를 대조하는 텍스트 판정이라, 에디터가 꺼져 있어도, CI 에서도
    같은 답을 낸다. 그래서 이 도구만은 브리지를 거치지 않는다.
    """

    root = projectPath.strip() or PROJECT_PATH
    if not root:
        raise tool_error(
            VALIDATION_ERROR,
            "Unity 프로젝트 경로를 알 수 없습니다. UNITY_PROJECT_PATH 를 설정하거나 "
            "projectPath 인자를 넘기세요.",
            gameId=gameId,
        )

    try:
        report = analyze_project(Path(root))
    except ProjectLayoutError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=gameId, projectPath=root) from exc

    return {
        "gameId": gameId,
        "projectPath": root,
        "counts": {
            "scripts": report.scripts,
            "monoBehaviours": report.behaviours,
            "prefabs": report.prefabs,
            "scenes": report.scenes,
        },
        "ok": report.ok,
        "findings": [
            {
                "rule": item.rule,
                "severity": str(item.severity),
                "target": item.target,
                "message": item.message,
            }
            for item in report.findings
        ],
    }


@mcp.tool(description="Report Unity bridge status and available Unity tools.")
@expects_dict_return
async def unity_bridge_status() -> dict[str, Any]:
    """진단용 — 오케스트레이터는 부르지 않는다."""

    try:
        bridge = await _ensure_bridge()
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "projectPath": PROJECT_PATH, "error": str(exc)}
    return {
        "connected": True,
        "projectPath": PROJECT_PATH,
        "relayPath": str(bridge.relay_path),
        "unityToolCount": len(bridge.tool_names),
        "unityTools": bridge.tool_names,
    }


if __name__ == "__main__":
    serve(mcp)
