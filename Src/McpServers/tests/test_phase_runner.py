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
from phase_runner.schemas import phase_result_schema


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


def test_codex_exec_adapter_applies_profile_and_captures_fresh_thread(tmp_path, monkeypatch):
    applied = []
    result = _result("planning", assets_required=False)

    def fake_run(command, **kwargs):
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"thread-new"}\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = CodexExecExecutor(
        tmp_path, profile_applier=lambda profile: applied.append(profile) or 0
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
