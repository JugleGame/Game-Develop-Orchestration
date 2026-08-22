"""The local Phase Runner is deterministic and never needs a model in tests."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from phase_runner import (
    CodexExecExecutor,
    ExecutionResult,
    PhaseBlocked,
    PhaseError,
    PhaseRequest,
    PhaseRunner,
)
from phase_runner.schemas import codex_phase_result_schema, phase_result_schema
from phase_runner.executor import CodexExecError


def _result(
    phase: str,
    *,
    dimension: str = "2D",
    assets_required: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "phase": phase,
        "status": "completed",
        "summary": f"{phase} complete",
        "handoff": f"bounded handoff for {phase}",
        "artifactPaths": [f"var/evidence/{phase}.json"],
    }
    if phase == "planning":
        result.update(
            {"visualDimension": dimension, "assetsRequired": assets_required}
        )
    if phase in {"unity_implementation", "unity_integration"}:
        result["qaStatus"] = "PASS"
    return result


@dataclass
class FakeExecutor:
    dimension: str = "2D"
    assets_required: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    overrides: dict[str, list[dict[str, Any] | Exception]] = field(default_factory=dict)

    def execute(self, request):
        self.calls.append((request.phase, request.profile))
        self.prompts.append(request.prompt)
        queued = self.overrides.get(request.phase, [])
        if queued:
            value = queued.pop(0)
            if isinstance(value, Exception):
                raise value
            result = value
        else:
            result = _result(
                request.phase,
                dimension=self.dimension,
                assets_required=self.assets_required,
            )
        return ExecutionResult(
            thread_id=f"thread-{request.phase}-{len(self.calls)}", result=result
        )


def _runner(tmp_path: Path, executor: FakeExecutor) -> PhaseRunner:
    return PhaseRunner(tmp_path, executor, runs_root=tmp_path / "runs")


@pytest.mark.parametrize(
    ("dimension", "assets_required", "expected"),
    [
        ("2D", True, [("asset2d_generation", "asset2d")]),
        ("3D", True, [("asset3d_generation", "asset3d")]),
        (
            "hybrid",
            True,
            [("asset2d_generation", "asset2d"), ("asset3d_generation", "asset3d")],
        ),
        ("2D", False, []),
    ],
)
def test_all_routes_use_fresh_role_scoped_phases(
    tmp_path, dimension, assets_required, expected
):
    executor = FakeExecutor(dimension=dimension, assets_required=assets_required)
    runner = _runner(tmp_path, executor)

    state = runner.start("make a game", run_id="route")
    assert state["status"] == "awaiting-planning-approval"
    assert executor.calls == [("planning", "research")]
    with pytest.raises(PhaseBlocked, match="planning approval"):
        runner.resume("route")

    runner.approve("route", "planning")
    state = runner.resume("route")
    if assets_required:
        assert state["status"] == "awaiting-asset-generation-approval"
        runner.approve("route", "asset-generation")
        state = runner.resume("route")
        assert state["status"] == "awaiting-asset-review"
        runner.approve("route", "asset-review")
        state = runner.resume("route")

    assert state["status"] == "completed"
    assert executor.calls == [
        ("planning", "research"),
        ("unity_implementation", "unity"),
        *expected,
        ("unity_integration", "unity"),
    ]
    assert len({record["threadId"] for record in state["phases"].values()}) == len(
        state["phases"]
    )
    assert "make a game" in executor.prompts[0]
    assert "make a game" not in executor.prompts[1]


def test_approval_gates_cannot_be_skipped_or_approved_early(tmp_path):
    runner = _runner(tmp_path, FakeExecutor())
    runner.start("make a game", run_id="gates")

    with pytest.raises(PhaseBlocked):
        runner.approve("gates", "asset-generation")
    with pytest.raises(PhaseBlocked):
        runner.resume("gates")
    state = runner.reject("gates", "planning", "needs revision")
    assert state["status"] == "rejected"
    with pytest.raises(PhaseBlocked, match="rejected"):
        runner.resume("gates")


def test_completed_phases_are_idempotent_across_reloads(tmp_path):
    executor = FakeExecutor(assets_required=False)
    runner = _runner(tmp_path, executor)
    runner.start("make a game", run_id="idempotent")
    runner.approve("idempotent", "planning")
    assert runner.resume("idempotent")["status"] == "completed"
    calls = list(executor.calls)

    reloaded = _runner(tmp_path, executor)
    with pytest.raises(PhaseBlocked, match="completed"):
        reloaded.resume("idempotent")
    assert executor.calls == calls


def test_failure_is_saved_and_requires_explicit_retry(tmp_path):
    executor = FakeExecutor(
        assets_required=False,
        overrides={
            "unity_implementation": [
                RuntimeError("relay unavailable"),
                _result("unity_implementation"),
            ]
        },
    )
    runner = _runner(tmp_path, executor)
    runner.start("make a game", run_id="retry")
    runner.approve("retry", "planning")

    with pytest.raises(PhaseError, match="relay unavailable"):
        runner.resume("retry")
    state = runner.status("retry")
    assert state["status"] == "failed"
    assert state["phases"]["unity_implementation"]["attempts"] == 1
    with pytest.raises(PhaseBlocked, match="failed"):
        runner.resume("retry")

    state = _runner(tmp_path, executor).retry("retry")
    assert state["phases"]["unity_implementation"]["attempts"] == 2
    assert _runner(tmp_path, executor).resume("retry")["status"] == "completed"


def test_invalid_phase_result_fails_closed(tmp_path):
    invalid = _result("planning")
    invalid["visualDimension"] = "VR"
    executor = FakeExecutor(overrides={"planning": [invalid]})
    runner = _runner(tmp_path, executor)

    with pytest.raises(PhaseError, match="invalid phase result"):
        runner.start("make a game", run_id="invalid")
    assert runner.status("invalid")["status"] == "failed"


def test_non_pass_unity_qa_stops_workflow_before_integration(tmp_path):
    incomplete = _result("unity_implementation")
    incomplete["qaStatus"] = "INCOMPLETE"
    incomplete["summary"] = "Unity 연결이 취소되어 증거가 없습니다."
    executor = FakeExecutor(
        assets_required=False,
        overrides={"unity_implementation": [incomplete]},
    )
    runner = _runner(tmp_path, executor)
    runner.start("build it", run_id="qa-incomplete")
    runner.approve("qa-incomplete", "planning")

    with pytest.raises(PhaseError, match="functional QA did not pass: INCOMPLETE"):
        runner.resume("qa-incomplete")
    state = runner.status("qa-incomplete")
    assert state["status"] == "failed"
    assert state["phases"]["unity_implementation"]["status"] == "failed"
    assert "unity_integration" not in state["phases"]


def test_unknown_persisted_state_is_rejected_without_executor_call(tmp_path):
    executor = FakeExecutor()
    runner = _runner(tmp_path, executor)
    runner.start("make a game", run_id="unknown")
    path = tmp_path / "runs" / "unknown" / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["status"] = "teleporting"
    path.write_text(json.dumps(state), encoding="utf-8")
    calls = list(executor.calls)

    with pytest.raises(PhaseError, match="unknown run status"):
        runner.resume("unknown")
    assert executor.calls == calls


def test_state_writes_are_atomic_and_prompt_is_not_in_status(tmp_path):
    runner = _runner(tmp_path, FakeExecutor())
    public = runner.start("private initial prompt", run_id="atomic")

    assert "initialPrompt" not in public
    run_dir = tmp_path / "runs" / "atomic"
    assert not list(run_dir.glob("state.json.*"))
    assert json.loads((run_dir / "state.json").read_text(encoding="utf-8"))[
        "initialPrompt"
    ] == "private initial prompt"


def test_codex_exec_adapter_applies_profile_and_captures_fresh_thread(tmp_path):
    applied = []
    result = _result("planning", assets_required=False)

    def fake_run(command, **kwargs):
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"thread-new"}\n',
            stderr="",
        )

    executor = CodexExecExecutor(
        tmp_path,
        codex_command="codex-test",
        profile_applier=lambda profile: applied.append(profile) or 0,
        process_runner=fake_run,
    )
    request = PhaseRequest(
        run_id="adapter",
        phase="planning",
        profile="research",
        prompt="return a planning result",
        schema=phase_result_schema("planning"),
        phase_dir=tmp_path / "phase",
    )

    execution = executor.execute(request)

    assert applied == ["research"]
    assert execution.thread_id == "thread-new"
    assert execution.result == result
    assert (tmp_path / "phase" / "output-schema.json").is_file()
    assert (tmp_path / "phase" / "events.jsonl").is_file()


def test_phase_schemas_declare_types_for_const_and_enum_properties():
    planning = phase_result_schema("planning")

    assert planning["properties"]["phase"] == {
        "type": "string",
        "const": "planning",
    }
    assert planning["properties"]["status"] == {
        "type": "string",
        "const": "completed",
    }
    assert planning["properties"]["visualDimension"]["type"] == "string"

    transport = codex_phase_result_schema("planning")
    packed = json.dumps(transport)
    assert "minLength" not in packed
    assert "maxLength" not in packed
    assert "maxItems" not in packed
    assert phase_result_schema("planning")["properties"]["summary"]["maxLength"] == 2000


def test_codex_exec_failure_surfaces_jsonl_cause_and_removes_stale_result(tmp_path):
    phase_dir = tmp_path / "phase"
    phase_dir.mkdir()
    stale_result = phase_dir / "result.json"
    stale_result.write_text('{"stale": true}', encoding="utf-8")
    api_error = {
        "type": "error",
        "error": {
            "code": "invalid_json_schema",
            "message": "schema must have a type key",
        },
    }
    turn_failure = {
        "type": "turn.failed",
        "error": {"message": json.dumps({"error": api_error["error"]})},
    }

    def failed_run(command, **kwargs):
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(api_error) + "\n" + json.dumps(turn_failure) + "\n",
            stderr="unhelpful warning\n",
        )

    executor = CodexExecExecutor(
        tmp_path,
        codex_command="codex-test",
        profile_applier=lambda _profile: 0,
        process_runner=failed_run,
    )
    request = PhaseRequest(
        run_id="failure",
        phase="planning",
        profile="research",
        prompt="plan",
        schema=phase_result_schema("planning"),
        phase_dir=phase_dir,
    )

    with pytest.raises(CodexExecError, match="invalid_json_schema.*type key"):
        executor.execute(request)
    assert not stale_result.exists()
    assert (phase_dir / "events.jsonl").is_file()
    assert (phase_dir / "stderr.log").is_file()


def test_codex_exec_failure_detail_is_bounded_and_redacts_credentials(tmp_path):
    secret = "sk-" + ("a" * 32)

    def failed_run(command, **kwargs):
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"failure {secret} " + ("x" * 3000),
        )

    executor = CodexExecExecutor(
        tmp_path,
        codex_command="codex-test",
        profile_applier=lambda _profile: 0,
        process_runner=failed_run,
    )
    request = PhaseRequest(
        run_id="bounded",
        phase="planning",
        profile="research",
        prompt="plan",
        schema=phase_result_schema("planning"),
        phase_dir=tmp_path / "bounded-phase",
    )

    with pytest.raises(CodexExecError) as raised:
        executor.execute(request)
    message = str(raised.value)
    assert secret not in message
    assert "[REDACTED]" in message
    assert len(message) < 1600


def test_windows_command_discovery_skips_denied_desktop_binary(tmp_path):
    windows_apps = tmp_path / "WindowsApps"
    app_data = tmp_path / "AppData" / "Roaming"
    npm_bin = app_data / "npm"
    windows_apps.mkdir()
    npm_bin.mkdir(parents=True)
    protected = windows_apps / "codex.exe"
    standalone = npm_bin / "codex.cmd"
    protected.touch()
    standalone.touch()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == str(protected.resolve()):
            raise PermissionError(5, "access denied")
        return SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")

    executor = CodexExecExecutor(
        tmp_path,
        process_runner=fake_run,
        environment={"PATH": str(windows_apps), "APPDATA": str(app_data)},
        platform_name="nt",
    )

    assert executor.validate_command() == (str(standalone.resolve()),)
    assert [call[0] for call in calls] == [str(protected.resolve()), str(standalone.resolve())]


def test_windows_codex_processes_are_started_without_console_window(tmp_path):
    calls = []
    result = _result("planning", assets_required=False)

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"thread-hidden"}\n',
            stderr="",
        )

    executor = CodexExecExecutor(
        tmp_path,
        codex_command="codex.cmd",
        profile_applier=lambda _profile: 0,
        process_runner=fake_run,
        platform_name="nt",
    )
    executor.execute(
        PhaseRequest(
            run_id="hidden",
            phase="planning",
            profile="research",
            prompt="plan",
            schema=phase_result_schema("planning"),
            phase_dir=tmp_path / "hidden-phase",
        )
    )

    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert len(calls) == 2
    assert all(kwargs["creationflags"] == expected for _command, kwargs in calls)


def test_explicit_codex_override_fails_closed_without_path_fallback(tmp_path):
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    (fallback_dir / "codex.exe").touch()
    calls = []

    def denied(command, **kwargs):
        calls.append(command)
        raise PermissionError(5, "access denied")

    executor = CodexExecExecutor(
        tmp_path,
        process_runner=denied,
        environment={"PATH": str(fallback_dir), "GDAI_CODEX_COMMAND": "chosen-codex"},
        platform_name="nt",
    )

    with pytest.raises(CodexExecError, match="chosen-codex"):
        executor.validate_command()
    assert calls == [["chosen-codex", "--version"]]


def test_forced_codex_recheck_detects_removed_or_blocked_command(tmp_path):
    available = True
    calls = []

    def changing_runner(command, **kwargs):
        calls.append(command)
        if not available:
            raise PermissionError(5, "access denied after startup")
        return SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")

    executor = CodexExecExecutor(
        tmp_path, codex_command="codex-test", process_runner=changing_runner
    )
    assert executor.validate_command() == ("codex-test",)
    assert executor.validate_command() == ("codex-test",)
    assert len(calls) == 1

    available = False
    with pytest.raises(CodexExecError, match="after startup"):
        executor.validate_command(force=True)
    assert len(calls) == 2


def test_codex_preflight_failure_does_not_create_run_state(tmp_path):
    def denied(command, **kwargs):
        raise PermissionError(5, "access denied")

    executor = CodexExecExecutor(
        tmp_path,
        codex_command="protected-codex",
        profile_applier=lambda _profile: 0,
        process_runner=denied,
    )
    runner = PhaseRunner(tmp_path, executor, runs_root=tmp_path / "runs")

    with pytest.raises(PhaseError, match="Codex CLI is not executable"):
        runner.start("must fail before state creation", run_id="no-state")
    assert not (tmp_path / "runs" / "no-state").exists()


def test_unavailable_cli_does_not_mask_gate_or_mutate_retry_state(tmp_path):
    planning_executor = FakeExecutor()
    runner = _runner(tmp_path, planning_executor)
    runner.start("wait at planning", run_id="gate-first")

    class UnavailableExecutor:
        def validate_command(self, *, force=False):
            raise OSError("CLI unavailable")

        def execute(self, request):
            raise AssertionError("execute must not be reached")

    unavailable = PhaseRunner(
        tmp_path, UnavailableExecutor(), runs_root=tmp_path / "runs"
    )
    with pytest.raises(PhaseBlocked, match="planning approval"):
        unavailable.resume("gate-first")

    failing_executor = FakeExecutor(
        assets_required=False,
        overrides={"unity_implementation": [RuntimeError("first failure")]},
    )
    failing = _runner(tmp_path, failing_executor)
    failing.start("create retry state", run_id="retry-preflight")
    failing.approve("retry-preflight", "planning")
    with pytest.raises(PhaseError, match="first failure"):
        failing.resume("retry-preflight")

    before = unavailable.status("retry-preflight")
    with pytest.raises(PhaseError, match="CLI unavailable"):
        unavailable.retry("retry-preflight")
    after = unavailable.status("retry-preflight")
    assert after == before


@pytest.mark.skipif(
    os.environ.get("GDAI_RUN_CODEX_SMOKE") != "1",
    reason="real Codex execution is explicit opt-in",
)
def test_codex_exec_smoke_opt_in(tmp_path):
    root = Path(__file__).resolve().parents[3]
    executor = CodexExecExecutor(root, profile_applier=lambda _profile: 0)
    request = PhaseRequest(
        run_id="smoke",
        phase="planning",
        profile="research",
        prompt=(
            "Do not call tools or change files. Return a minimal planning JSON result for a 2D "
            "game with assetsRequired false."
        ),
        schema=phase_result_schema("planning"),
        phase_dir=tmp_path / "codex-smoke",
    )

    execution = executor.execute(request)

    assert execution.thread_id
    assert execution.result["phase"] == "planning"
