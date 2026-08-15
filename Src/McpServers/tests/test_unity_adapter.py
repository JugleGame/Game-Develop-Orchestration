"""UnityMcpServer 어댑터 검증 — 실행 중인 Unity Editor 없이 도는 부분만.

Unity 를 실제로 태우는 확인은 운영 문서의 수동 절차를 따른다. 여기서는 MCP
계약과 순수 변환 로직을 고정한다.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("UNITY_PROJECT_PATH", "C:/nonexistent-unity-project")

from unity import server as unity_server  # noqa: E402
from unity.bridge import UnityBridgeError, discover_editor_instance_id  # noqa: E402
from unity.codegen import (  # noqa: E402
    ScriptGenerator,
    _sanitize_class_name,
    looks_like_csharp,
)


def test_discovers_current_unity_editor_pid_from_matching_registry_record(tmp_path, monkeypatch):
    project = tmp_path / "Game"
    project.mkdir()
    record = tmp_path / "connections" / "bridge-game-101.json"
    record.parent.mkdir()
    record.write_text(
        json.dumps(
            {
                "project_path": str(project),
                "editor_pid": 101,
                "connection_path": r"\\.\pipe\unity-mcp-game-101",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("unity.bridge.os.kill", lambda pid, signal: None)

    assert discover_editor_instance_id(str(project), record.parent) == "101"


def test_discovery_ignores_other_projects_and_invalid_pipe_records(tmp_path, monkeypatch):
    project = tmp_path / "Game"
    project.mkdir()
    registry = tmp_path / "connections"
    registry.mkdir()
    (registry / "bridge-other-101.json").write_text(
        json.dumps(
            {
                "project_path": "C:/other",
                "editor_pid": 101,
                "connection_path": r"\\.\pipe\unity-mcp-other-101",
            }
        ),
        encoding="utf-8",
    )
    (registry / "bridge-invalid-102.json").write_text(
        json.dumps(
            {"project_path": str(project), "editor_pid": 102, "connection_path": "not-a-pipe"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("unity.bridge.os.kill", lambda pid, signal: None)

    assert discover_editor_instance_id(str(project), registry) is None


def test_explicit_editor_pid_requires_positive_integer(monkeypatch):
    monkeypatch.setenv("UNITY_EDITOR_PID", "123")
    assert discover_editor_instance_id("C:/game") == "123"

    monkeypatch.setenv("UNITY_EDITOR_PID", "not-a-pid")
    with pytest.raises(UnityBridgeError, match="positive integer"):
        discover_editor_instance_id("C:/game")


# ---------------------------------------------------------------------------
# Agent-first 계약
# ---------------------------------------------------------------------------
def test_exposes_every_contract_tool():
    names = {tool.name for tool in unity_server.mcp._tool_manager.list_tools()}

    assert {
        "create_script",
        "create_scene",
        "import_asset",
        "build_project",
        "run_playmode_test",
        "get_compile_errors",
        "design_architecture",
        "inspect_project_layout",
    } <= names


@pytest.mark.parametrize(
    ("tool_name", "required"),
    [
        ("create_script", {"featureId", "contents"}),
        ("create_scene", {"featureId", "sceneName"}),
        ("import_asset", {"featureId", "assetPath"}),
        ("build_project", {"gameId"}),
        ("run_playmode_test", {"gameId"}),
        ("get_compile_errors", {"gameId"}),
    ],
)
def test_tools_use_camel_case_argument_names(tool_name: str, required: set[str]):
    """오케스트레이터는 camelCase 로 보낸다. snake_case 면 -32602 로 튕긴다."""

    tool = next(t for t in unity_server.mcp._tool_manager.list_tools() if t.name == tool_name)

    assert required <= set(tool.parameters["properties"])


# ---------------------------------------------------------------------------
# 콘솔 항목 → CompileError 변환
# ---------------------------------------------------------------------------
def test_compile_error_parses_unity_message_format():
    """Unity 는 파일/라인을 메시지 문자열 안에 넣는다."""

    entry = {"message": "Assets/Scripts/Player.cs(12,5): error CS0103: name not found"}

    parsed = unity_server._to_compile_error(entry)

    assert parsed["file"] == "Assets/Scripts/Player.cs"
    assert parsed["line"] == 12
    assert "CS0103" in parsed["message"]


def test_compile_error_prefers_explicit_fields():
    entry = {"file": "Assets/A.cs", "line": 7, "message": "boom"}

    assert unity_server._to_compile_error(entry) == {
        "file": "Assets/A.cs",
        "line": 7,
        "message": "boom",
    }


def test_compile_error_never_returns_empty_file():
    """CompileError.file 은 필수(str). 빈 값이면 어떤 파일을 고칠지 알 수 없다."""

    parsed = unity_server._to_compile_error({"message": "generic failure"})

    assert parsed["file"] == "unknown"
    assert parsed["line"] is None


def test_compile_error_survives_non_dict_entries():
    assert unity_server._to_compile_error("raw string")["message"] == "raw string"


def test_build_defaults_to_webgl_with_a_project_relative_directory(monkeypatch):
    monkeypatch.delenv("UNITY_BUILD_TARGET", raising=False)
    monkeypatch.delenv("UNITY_BUILD_OUTPUT", raising=False)

    assert unity_server._build_configuration("slice-001") == (
        "WebGL",
        "Builds/slice-001",
    )


@pytest.mark.parametrize(
    ("target", "output"),
    [
        ("Injected; result.Log(\"unexpected\")", "Builds/game"),
        ("WebGL", "../outside"),
        ("WebGL", "C:/outside"),
        ("WebGL", 'Builds/game"; result.Log("unexpected")'),
    ],
)
def test_build_configuration_rejects_values_that_could_escape_generated_csharp(
    monkeypatch, target, output
):
    monkeypatch.setenv("UNITY_BUILD_TARGET", target)
    monkeypatch.setenv("UNITY_BUILD_OUTPUT", output)

    with pytest.raises(Exception) as exc_info:
        unity_server._build_configuration("slice-001")

    assert "errorCode" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 빌드 결과 추출
# ---------------------------------------------------------------------------
def test_build_result_extracted_from_nested_text():
    """Unity_RunCommand 는 우리 JSON 을 로그 텍스트 안에 섞어 돌려준다."""

    payload = {
        "success": True,
        "logs": 'Compiling...\n{"success":true,"result":"Succeeded","outputPath":"Builds/g1/game.exe","totalErrors":0,"durationSeconds":42}\nDone',
    }

    extracted = unity_server._extract_build_result(payload)

    assert extracted["result"] == "Succeeded"
    assert extracted["outputPath"] == "Builds/g1/game.exe"
    assert extracted["durationSeconds"] == 42


def test_build_result_falls_back_to_payload():
    payload = {"nothing": "useful"}

    assert unity_server._extract_build_result(payload) == payload


# ---------------------------------------------------------------------------
# C# 이름/소스 처리
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("feature_id", "expected"),
    [
        ("f-1", "F1"),
        ("FEAT-PLAYER-001", "FEATPLAYER001"),
        ("double_jump", "DoubleJump"),
        ("123", "Feature123"),
    ],
)
def test_feature_id_becomes_valid_csharp_identifier(feature_id: str, expected: str):
    assert _sanitize_class_name(feature_id) == expected


def test_class_name_is_taken_from_the_source_not_the_feature_id():
    """Unity 는 파일명과 클래스명이 반드시 같아야 한다."""

    generator = ScriptGenerator(namespace="Game.Gameplay", script_root="Assets/Scripts")
    source = "namespace Game.Gameplay { public class PlayerController2D : MonoBehaviour {} }"

    planned = generator.plan("f-1", source)

    assert planned.class_name == "PlayerController2D"
    assert planned.path == "Assets/Scripts/PlayerController2D.cs"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("public class A { }", True),
        ("public struct B { }", True),
        ("Add a double jump to the player", False),
        ("", False),
    ],
)
def test_csharp_detection(text: str, expected: bool):
    """호스트 입력이 C# 소스 형태인지 빠르게 판별한다."""

    assert looks_like_csharp(text) is expected


def test_non_csharp_contents_fail_clearly():
    from unity.codegen import CodeGenerationError

    generator = ScriptGenerator()

    with pytest.raises(CodeGenerationError) as exc_info:
        generator.plan("f-1", "Add a double jump")

    assert "contents" in str(exc_info.value)


# ---------------------------------------------------------------------------
# create_script 갱신 경로. update=True이면 Unity_ManageScript를 사용한다.
# ---------------------------------------------------------------------------
def _record_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def fake_call_unity(tool, arguments, timeout=120.0, build_error=False):
        calls.append((tool, arguments))
        if tool == "Unity_ValidateScript":
            return {"validated": True}
        return {}

    monkeypatch.setattr(unity_server, "_call_unity", fake_call_unity)
    return calls


@pytest.mark.asyncio
async def test_create_script_retry_calls_manage_script_update(monkeypatch):
    calls = _record_calls(monkeypatch)

    await unity_server.create_script(
        featureId="f-1",
        contents="public class F1 : MonoBehaviour { }",
        update=True,
    )

    assert not any(c[0] == "Unity_CreateScript" for c in calls)
    _, args = next(c for c in calls if c[0] == "Unity_ManageScript")
    assert args["action"] == "update"
    assert args["name"] == "F1"
    assert args["path"] == "Assets/Scripts"
    assert args["contents"] == "public class F1 : MonoBehaviour { }"


@pytest.mark.asyncio
async def test_create_script_first_pass_omits_action(monkeypatch):
    calls = _record_calls(monkeypatch)

    await unity_server.create_script(
        featureId="f-1",
        contents="public class F1 : MonoBehaviour { }",
    )

    _, args = next(c for c in calls if c[0] == "Unity_CreateScript")
    assert "Action" not in args


# ---------------------------------------------------------------------------
# import_asset — 서로 다른 featureId 가 같은 파일명(예: wang_0.png)을 만들면
# Assets/Generated 에서 서로 덮어쓰던 결함(2026-08-04 실측)을 고정한다.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_import_asset_dedupes_same_basename_by_feature_id(tmp_path, monkeypatch):
    calls = _record_calls(monkeypatch)
    monkeypatch.setattr(unity_server, "PROJECT_PATH", str(tmp_path))

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for name in ("theme-a", "theme-b"):
        (source_dir / "wang_0.png").write_bytes(b"fake-png")
        await unity_server.import_asset(
            featureId=name, assetPath=str(source_dir / "wang_0.png")
        )

    imported_paths = {
        args["Path"] for tool, args in calls if tool == "Unity_ManageAsset"
    }
    assert imported_paths == {
        "Assets/Generated/theme-a_wang_0.png",
        "Assets/Generated/theme-b_wang_0.png",
    }
    generated_dir = tmp_path / "Assets" / "Generated"
    assert (generated_dir / "theme-a_wang_0.png").exists()
    assert (generated_dir / "theme-b_wang_0.png").exists()


@pytest.mark.asyncio
async def test_import_asset_repairs_model_texture_types(tmp_path, monkeypatch):
    """생성 FBX 의 노멀맵이 ``Default`` 로 들어오면 셰이딩이 깨진다."""

    _record_calls(monkeypatch)
    monkeypatch.setattr(unity_server, "PROJECT_PATH", str(tmp_path))
    commands: list[str] = []

    async def fake_run_command(code, title, timeout):
        commands.append(code)
        return {"success": True, "repaired": ["Assets/Generated/f-1_chest.fbm/albedo_normal.png"]}

    monkeypatch.setattr(unity_server, "_run_command", fake_run_command)
    source = tmp_path / "chest.fbx"
    source.write_bytes(b"Kaydara FBX Binary  ")

    result = await unity_server.import_asset(featureId="f-1", assetPath=str(source))

    assert result["texturesRepaired"] == [
        "Assets/Generated/f-1_chest.fbm/albedo_normal.png"
    ]
    assert 'string folder = "Assets/Generated";' in commands[0]
    assert "TextureImporterType.NormalMap" in commands[0]
