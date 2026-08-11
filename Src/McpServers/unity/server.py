"""UnityMcpServer — §03 계약을 Unity 공식 MCP 위에 얹는 어댑터.

이 서버는 **MCP 서버이자 MCP 클라이언트**다::

    오케스트레이터 --Streamable HTTP :9102--> [이 어댑터] --stdio--> relay_win.exe --pipe--> Unity Editor
                     §03 도구 이름                                     Unity_* 도구 이름

Unity 공식 MCP(``com.unity.ai.assistant``)가 §03 과 어긋나는 지점 세 가지를
여기서 흡수한다.

1. **전송이 다르다.** Unity 는 stdio 릴레이 + 명명 파이프. 오케스트레이터는
   Streamable HTTP. → ``bridge.UnityBridge``.
2. **코드를 생성하지 않는다.** ``Unity_CreateScript`` 는 완성된 ``Contents``
   를 요구하는데 §03 ``create_script`` 는 자연어 ``prompt`` 를 준다.
   → ``codegen.ScriptGenerator`` 가 C# 을 만들어 넣는다.
3. **빌드 도구가 없다.** Unity 의 54개 도구 어디에도 빌드가 없다.
   → ``Unity_RunCommand`` 로 ``BuildPipeline`` C# 을 실행한다.
"""

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
from common.usage import merge_usage
from project_layout import ProjectLayoutError, analyze_project

from . import assemblies, assembly, csharp_check
from .architecture import ArchitectureDesigner, ArchitectureError, validate_design
from .assembly import AssemblyError
from .bridge import UnityBridge, UnityBridgeError
from .codegen import CodeGenerationError, ScriptGenerator, looks_like_csharp

mcp = build("UnityMcpServer", 9102)

logger = logging.getLogger("UnityMcpServer")

PROJECT_PATH = os.getenv("UNITY_PROJECT_PATH", "")
_bridge = UnityBridge(project_path=PROJECT_PATH)
_generator = ScriptGenerator()
_designer = ArchitectureDesigner()


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

    if result.structuredContent is not None:
        return result.structuredContent
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
    """Unity 도구를 부르고 실패를 §03 오류로 정규화한다."""

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
    if result.isError or payload.get("success") is False:
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
    것이 없다고 판단하고 QA 는 근거 없이 통과시킨다 — 06 문서가 말하는
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
# §03 도구
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "코드를 쓰기 전에 게임 전체의 파일·타입·프리팹·씬 구성을 한 번에 설계한다. "
        "게임당 한 번만 부른다. design 인자로 완성된 설계안을 넘기면 모델을 부르지 않는다."
    )
)
@expects_dict_return
async def design_architecture(
    gameId: str,
    gameDesign: dict[str, Any] | None = None,
    featurePrompts: list[dict[str, Any]] | None = None,
    design: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """기획의 **기능 경계**를 코드 경계로 옮긴다.

    기획(``unityHints.components``)은 "청크 좌표 계산" 같은 우리말 기능 덩어리까지만
    정하고, 그것을 어떤 클래스·파일·프리팹으로 나눌지는 여기서 정해진다 — 사람이
    그렇게 정했다 (06 문서 §3.1).

    **게임당 한 번만 부른다.** 결과가 게임 안에서 불변이라 이후 ``create_script``
    호출들의 캐시 접두사로 쓰이고, 재시도가 설계를 다시 뽑지 않아야 회차마다
    구조가 흔들리지 않는다.

    ``design`` 을 채우면 서버는 모델을 호출하지 않고 **검증만** 한다 (경로 B).
    넘긴 값도 생성한 값과 똑같이 검증되므로 이 우회가 규칙에 구멍을 내지 않는다.
    """

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
        if design is not None:
            planned = validate_design(design, feature_ids)
        else:
            planned = await _designer.design(gameDesign or {}, prompts)
    except ArchitectureError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=gameId) from exc

    logger.info(
        "Architecture designed",
        extra={
            "game_id": gameId,
            "files": len(planned.files),
            "prefabs": len(planned.prefabs),
            "scene_objects": len(planned.scene.get("objects", [])),
            "supplied": design is not None,
        },
    )

    body = planned.to_dict()
    body["gameId"] = gameId
    # 생성 단계가 캐시 접두사로 싣는다 — 첫 파일부터 전체 타입 지도를 보게 된다.
    body["typeMap"] = planned.type_map()
    # 넘겨받은 설계안은 지출이 0 이므로 usage 를 싣지 않는다.
    if planned.usage is not None:
        body["usage"] = planned.usage
    return body


@mcp.tool(description="기능 설명으로 C# 스크립트를 생성해 Unity 프로젝트에 추가한다.")
@expects_dict_return
async def create_script(
    featureId: str,
    prompt: str,
    contents: str = "",
    projectContext: str = "",
    existingTypes: list[str] | None = None,
    previousSource: str = "",
    plannedPath: str = "",
    plannedClass: str = "",
) -> dict[str, Any]:
    """프롬프트를 C# 으로 바꿔 ``Unity_CreateScript`` 에 넣는다.

    §03 필수 인자는 ``featureId``/``prompt`` 뿐이고 나머지는 모두 옵션이라,
    계약대로만 부르는 호출자도 그대로 동작한다.

    * ``contents`` — 완성된 C# 을 직접 준다. 생성 단계를 건너뛴다 (키 불필요).
    * ``projectContext`` — 이 게임의 청사진 요약·코드 규약. 한 게임 안에서
      불변이라 프롬프트 캐시의 접두사로 쓰인다.
    * ``existingTypes`` — 이미 만들어진 타입 목록. 같은 게임의 다른 스크립트를
      알아보게 한다.
    * ``previousSource`` — 재시도일 때 이전 소스. 주어지면 백지 재작성이 아니라
      **수정**으로 동작한다.
    * ``plannedPath`` / ``plannedClass`` — ``design_architecture`` 가 정한 파일
      경로와 타입 이름. 주어지면 이름을 여기서 다시 짓지 않는다. **이 둘이 없을
      때만** ``feature_id`` 에서 이름을 만드는 옛 규칙이 돌고, 그 규칙이
      ``Spec001`` 같은 문서 번호 이름의 출처였다 (06 문서 §1.3).

    한 번에 파일 **하나**를 만든다. spec 하나가 파일 여럿이 되는 것은 설계안이
    파일을 여럿으로 나누고 호출자가 그만큼 부르기 때문이지, 이 도구가 여러 개를
    돌려주기 때문이 아니다 — 그래서 §03 의 ``{file}`` 반환이 그대로 유지된다.

    Unity 에 넣기 **전에** 로컬 문법 게이트를 통과시킨다. 통과하지 못하면
    (그리고 우리가 생성한 소스라면) 그 오류를 근거로 한 번 고쳐 보고, 그래도
    안 되면 Unity 를 건드리지 않고 실패한다 — 명백한 구문 오류를 30분짜리
    빌드로 확인하지 않기 위해서다.
    """

    featureId = _require(featureId, "featureId")
    supplied = bool(contents.strip()) or looks_like_csharp(prompt)

    try:
        if contents.strip():
            script = _generator.plan(
                featureId,
                contents.strip(),
                planned_path=plannedPath,
                planned_class=plannedClass,
            )
        elif looks_like_csharp(prompt):
            # 호출자가 프롬프트 자리에 이미 C# 을 넣은 경우.
            script = _generator.plan(
                featureId,
                prompt.strip(),
                planned_path=plannedPath,
                planned_class=plannedClass,
            )
        else:
            script = await _generator.generate(
                featureId,
                _require(prompt, "prompt"),
                project_context=projectContext,
                existing_types=existingTypes or [],
                previous_source=previousSource,
                planned_path=plannedPath,
                planned_class=plannedClass,
            )
    except CodeGenerationError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), featureId=featureId) from exc

    gate = csharp_check.check(script.contents)
    repair_usage: dict[str, Any] | None = None

    if not gate.ok and not supplied:
        # 우리가 만든 소스이므로 한 번은 스스로 고쳐 본다. 넘겨받은 소스는
        # 호출자의 산출물이라 말없이 바꾸지 않는다.
        logger.warning(
            "Generated C# failed the local syntax gate; attempting one repair",
            extra={"feature_id": featureId, "gate_errors": list(gate.errors)},
        )
        try:
            repaired = await _generator.generate(
                featureId,
                "The file has these syntax errors:\n"
                + "\n".join(f"- {item}" for item in gate.errors),
                project_context=projectContext,
                existing_types=existingTypes or [],
                previous_source=script.contents,
            )
        except CodeGenerationError as exc:
            raise tool_error(VALIDATION_ERROR, str(exc), featureId=featureId) from exc
        repair_usage = script.usage
        script = repaired
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

    if previousSource.strip():
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

    # §03: 오케스트레이터는 body["file"] 만 읽는다. 반드시 채워야 한다.
    body: dict[str, Any] = {
        "file": script.path,
        "className": script.class_name,
        # 이 파일이 선언한 **모든** 타입. 호출자가 다음 스크립트의
        # ``existingTypes`` 로 넘겨, 같은 파일 안의 형제 타입이 뒤에 오는
        # 스크립트에게 보이지 않던 문제를 없앤다 (06 문서 §2.1).
        "types": list(script.types),
        "namespace": script.namespace,
        "featureId": featureId,
        "validation": diagnostics,
        "syntaxGate": {
            "ok": gate.ok,
            "checkedBy": gate.checked_by,
            "repaired": repair_usage is not None,
        },
        # 재시도 루프가 이전 소스를 되돌려 받아 다음 라운드에 넘긴다.
        "contents": script.contents,
    }
    # 생성 경로로 왔을 때만 붙는다. contents 를 직접 받은 호출은 토큰을 쓰지 않는다.
    # 게이트 재시도가 있었다면 두 호출의 지출을 모두 합산한다 — 한쪽만 실으면
    # 그만큼이 통째로 안 보이게 된다.
    if script.usage is not None:
        body["usage"] = merge_usage(repair_usage, script.usage)
    return body


@mcp.tool(description="Unity 씬을 생성한다.")
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


@mcp.tool(description="생성된 에셋을 Unity 프로젝트로 임포트한다.")
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
# 조립 3종 — 설계안을 실행만 한다 (06 문서 §3.2 3단계)
#
# 이 셋은 새로 판단하지 않는다. ``design_architecture`` 의 ``prefabs[]`` 가
# ``create_prefab`` 의 인자이고 ``scene`` 이 ``compose_scene`` 의 인자다. 그래서
# 같은 설계안이면 조립 결과가 실행마다 같다.
#
# 이 셋이 생기기 전에는 모델이 런타임 ``new GameObject`` + ``AddComponent`` 로
# 조립을 흉내 낼 수밖에 없었다 (06 문서 §1.2). 그 부트스트랩이 더 이상 나오지
# 않는 것이 3단계 성공의 신호다.
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "설계안의 프리팹 하나를 실제로 만든다 — 오브젝트에 컴포넌트를 붙여 "
        "Assets/Prefabs/ 에 저장한다. Unity Editor 가 떠 있어야 한다."
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
    준수 여부를 논할 수조차 없었다 (06 문서 §2.2).

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
        "설계안의 씬 계층을 실제로 만든다 — 오브젝트를 배치하고 컴포넌트를 붙여 "
        "저장하고 빌드 설정에 등록한다. Unity Editor 가 떠 있어야 한다."
    )
)
@expects_dict_return
async def compose_scene(
    gameId: str, sceneName: str, objects: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """``design_architecture`` 의 ``scene`` 을 그대로 받는다.

    ``create_scene`` 이 만드는 것은 빈 씬이다. 지금까지 파이프라인 7단계
    「씬 구성」의 실제 도구가 그것뿐이라, 스크립트가 어디에도 붙지 않은 채
    빌드까지 갔다 (06 문서 §1.2). 이 도구가 그 자리를 채운다.

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
        "인스펙터의 [SerializeField] 칸에 스프라이트나 프리팹을 꽂는다. "
        "target 은 .prefab 경로이거나 씬 안의 계층 경로다."
    )
)
@expects_dict_return
async def bind_reference(
    gameId: str, target: str, field: str, value: str, scene: str = ""
) -> dict[str, Any]:
    """생성된 그림을 실제로 **화면에 나오게** 만드는 단계다.

    ``import_asset`` 은 임포트만 하고 참조를 꽂지 않는다. 그래서 그림 아홉 장을
    만들어 놓고 그것을 참조하는 코드가 0줄인 상태가 나왔다 (06 문서 §1.2).

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
# 4단계 — 어셈블리 분리 (06 문서 §3.2 4단계)
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "카테고리 폴더마다 .asmdef 를 놓아 빌드를 나눈다 — 한 파일을 고칠 때 전체 "
        "재컴파일을 피한다. 순환이 생길 조합은 계획 단계에서 빠지므로 나누다 만 "
        "상태가 되어도 컴파일은 깨지지 않는다."
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


@mcp.tool(description="Unity 프로젝트를 빌드한다 (BuildPipeline 실행).")
@expects_dict_return
async def build_project(gameId: str) -> dict[str, Any]:
    """Unity 에는 빌드 도구가 없어 ``Unity_RunCommand`` 로 C# 을 실행한다.

    빌드 타깃과 출력 경로는 환경변수로 조정한다
    (``UNITY_BUILD_TARGET``, ``UNITY_BUILD_OUTPUT``).
    """

    gameId = _require(gameId, "gameId")

    target = os.getenv("UNITY_BUILD_TARGET", "StandaloneWindows64")
    output = os.getenv("UNITY_BUILD_OUTPUT", f"Builds/{gameId}/game.exe").replace("\\", "/")
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


@mcp.tool(description="플레이 모드를 실행해 런타임 오류를 수집한다.")
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


@mcp.tool(description="마지막 컴파일/실행에서 발생한 오류를 반환한다.")
@expects_dict_return
async def get_compile_errors(gameId: str) -> dict[str, Any]:
    """§03: 오케스트레이터는 ``body["errors"]`` 를 ``CompileError`` 로 읽는다.

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
        "Unity 프로젝트의 구조를 보고한다 — 스크립트가 실제로 씬·프리팹에 붙어 있는지, "
        "이름 규칙과 폴더 규칙을 지키는지. Unity Editor 없이 파일만 읽는다."
    )
)
@expects_dict_return
async def inspect_project_layout(gameId: str = "", projectPath: str = "") -> dict[str, Any]:
    """QA 가 **구조**를 판정할 근거를 만든다.

    왜 필요한가. QA 에 넘어가는 ``build`` 문자열은 빌드 메타데이터와 플레이모드
    결과뿐이라, 판정자가 "이 스크립트가 어디에 붙어 있는가" 를 알 방법이 없었다.
    그래서 ``verify_prototype_structure`` 는 이름과 달리 ``core_mechanics``
    커버리지만 봤고, 여섯 스크립트 중 다섯이 어디에도 안 붙은 프로토타입이
    PASS 로 통과했다 (06 문서 §2.1).

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


@mcp.tool(description="Unity 브리지 상태와 사용 가능한 Unity 도구를 보고한다.")
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
