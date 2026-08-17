"""Issue Work Runner tests use fake Codex execution and temporary Git repositories."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from issue_runner import (
    CodexExecExecutor,
    ExecutionResult,
    GitError,
    GitRepository,
    IssueBlocked,
    IssueError,
    IssueRunner,
    PhaseRequest,
)
from issue_runner.schemas import phase_result_schema


CRITERIA = ["runner completes", "tests pass"]
TESTS = ["pytest issue runner", "contract check"]


def _issue(*, number: int = 54, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "title": "[pipeline] Issue Work Runner 추가",
        "body": "## 목표\nRunner 추가\n## 범위\n- 코드\n## 범위 제외\n- MCP 변경",
        "state": state,
        "url": f"https://github.test/owner/repo/issues/{number}",
    }


def _result(phase: str, *, verdict: str = "PASS") -> dict[str, Any]:
    result: dict[str, Any] = {
        "phase": phase,
        "status": "completed",
        "summary": f"{phase} complete",
        "artifactPaths": [f"var/evidence/{phase}.json"],
    }
    if phase == "analysis":
        result.update(
            {
                "objective": "Add the runner",
                "scope": ["runner code"],
                "outOfScope": ["MCP changes"],
                "acceptanceCriteria": CRITERIA,
                "tests": TESTS,
                "implementationPlan": ["implement", "verify"],
            }
        )
    elif phase == "implementation":
        result.update(
            {"changedFiles": ["issue_runner/controller.py"], "implementationNotes": ["done"]}
        )
    elif phase == "verification":
        result.update(
            {
                "testEvidence": [
                    {"id": item, "passed": True, "evidence": "ok", "artifactPaths": []}
                    for item in TESTS
                ],
                "allTestsPassed": True,
            }
        )
    else:
        result.update(
            {
                "verdict": verdict,
                "changedFiles": ["issue_runner/controller.py"],
                "scopeViolations": [],
                "acceptanceCriteriaEvidence": [
                    {"id": item, "passed": True, "evidence": "ok", "artifactPaths": []}
                    for item in CRITERIA
                ],
                "testEvidence": [
                    {"id": item, "passed": True, "evidence": "ok", "artifactPaths": []}
                    for item in TESTS
                ],
                "findings": [],
            }
        )
    return result


@dataclass
class FakeExecutor:
    calls: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    overrides: dict[str, list[dict[str, Any] | Exception]] = field(default_factory=dict)

    def execute(self, request: PhaseRequest) -> ExecutionResult:
        self.calls.append(request.phase)
        self.prompts.append(request.prompt)
        if request.phase == "implementation":
            root = request.phase_dir.parents[4]
            changed = root / "issue_runner" / "controller.py"
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.write_text("implemented\n", encoding="utf-8")
        queued = self.overrides.get(request.phase, [])
        value = queued.pop(0) if queued else _result(request.phase)
        if isinstance(value, Exception):
            raise value
        return ExecutionResult(f"thread-{request.phase}-{len(self.calls)}", value)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "dev")
    _git(root, "config", "user.name", "Runner Test")
    _git(root, "config", "user.email", "runner@example.test")
    (root / "README.md").write_text("test\n", encoding="utf-8")
    (root / ".gitignore").write_text("/var/\n", encoding="utf-8")
    _git(root, "add", "README.md", ".gitignore")
    _git(root, "commit", "-m", "initial")
    return root


def _runner(root: Path, executor: FakeExecutor) -> IssueRunner:
    return IssueRunner(root, executor, runs_root=root / "var" / "issue-runs")


def test_full_transition_uses_fresh_threads_and_bounded_handoffs(tmp_path):
    root = _repo(tmp_path)
    executor = FakeExecutor()
    runner = _runner(root, executor)

    state = runner.start(_issue(), work_branch="54-feat-issue-work-runner", run_id="full")
    assert state["status"] == "awaiting-analysis-approval"
    assert _git(root, "branch", "--show-current") == "54-feat-issue-work-runner"
    with pytest.raises(IssueBlocked, match="approval"):
        runner.resume("full")

    runner.approve("full")
    state = runner.resume("full")

    assert state["status"] == "completed"
    assert executor.calls == list(("analysis", "implementation", "verification", "review"))
    assert len({item["threadId"] for item in state["phases"].values()}) == 4
    assert json.dumps(_issue()["body"], ensure_ascii=False) in executor.prompts[0]
    assert _issue()["body"] not in executor.prompts[1]
    assert "repository.diff" in executor.prompts[-1]


def test_implementation_is_blocked_until_analysis_approval(tmp_path):
    runner = _runner(_repo(tmp_path), FakeExecutor())
    runner.start(_issue(), work_branch="54-feat-runner", run_id="gate")

    with pytest.raises(IssueBlocked, match="approval"):
        runner.resume("gate")
    assert runner.reject("gate", "plan needs revision")["status"] == "rejected"
    with pytest.raises(IssueBlocked, match="rejected"):
        runner.resume("gate")


def test_failure_is_saved_and_retry_is_explicit(tmp_path):
    root = _repo(tmp_path)
    executor = FakeExecutor(
        overrides={"implementation": [RuntimeError("worker failed"), _result("implementation")]}
    )
    runner = _runner(root, executor)
    runner.start(_issue(), work_branch="54-fix-retry", run_id="retry")
    runner.approve("retry")

    with pytest.raises(IssueError, match="worker failed"):
        runner.resume("retry")
    assert runner.status("retry")["status"] == "failed"
    with pytest.raises(IssueBlocked, match="failed"):
        runner.resume("retry")

    state = runner.retry("retry")
    assert state["phases"]["implementation"]["attempts"] == 2
    assert runner.resume("retry")["status"] == "completed"


def test_completed_run_is_idempotent(tmp_path):
    root = _repo(tmp_path)
    executor = FakeExecutor()
    runner = _runner(root, executor)
    runner.start(_issue(), work_branch="54-chore-idempotent", run_id="done")
    runner.approve("done")
    runner.resume("done")
    calls = list(executor.calls)

    with pytest.raises(IssueBlocked, match="completed"):
        _runner(root, executor).resume("done")
    assert executor.calls == calls


def test_same_run_concurrent_execution_is_blocked(tmp_path):
    root = _repo(tmp_path)
    runner = _runner(root, FakeExecutor())
    runner.start(_issue(), work_branch="54-test-run-lock", run_id="locked")

    with runner._locked("locked"):
        with pytest.raises(IssueBlocked, match="already active"):
            _runner(root, FakeExecutor()).resume("locked")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda issue: issue.update(state="closed"), "must be open"),
        (lambda issue: issue.pop("body"), "exactly"),
    ],
)
def test_invalid_issue_snapshot_is_rejected_before_git_change(tmp_path, mutation, message):
    root = _repo(tmp_path)
    snapshot = _issue()
    mutation(snapshot)

    with pytest.raises(IssueError, match=message):
        _runner(root, FakeExecutor()).start(
            snapshot, work_branch="54-feat-invalid", run_id="invalid"
        )
    assert _git(root, "branch", "--show-current") == "dev"


def test_git_preconditions_cover_dirty_base_collision_and_branch_contract(tmp_path):
    root = _repo(tmp_path)
    repository = GitRepository(root)
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(GitError, match="clean"):
        repository.prepare_branch(54, "dev", "54-feat-runner")
    (root / "dirty.txt").unlink()

    with pytest.raises(GitError, match="base branch dev"):
        repository.prepare_branch(54, "main", "54-feat-runner")
    with pytest.raises(GitError, match="must match"):
        repository.prepare_branch(54, "dev", "55-feat-runner")

    _git(root, "branch", "54-feat-runner")
    with pytest.raises(GitError, match="already exists"):
        repository.prepare_branch(54, "dev", "54-feat-runner")


def test_changed_files_preserve_non_ascii_paths(tmp_path):
    root = _repo(tmp_path)
    repository = GitRepository(root)
    repository.prepare_branch(54, "dev", "54-test-path-reporting")
    (root / "한글 파일.txt").write_text("evidence\n", encoding="utf-8")

    assert repository.changed_files("dev") == ["한글 파일.txt"]


@pytest.mark.parametrize("failure", ["scope", "criterion", "test", "verdict"])
def test_review_cannot_pass_without_complete_scope_and_evidence(tmp_path, failure):
    root = _repo(tmp_path)
    review = _result("review")
    if failure == "scope":
        review["scopeViolations"] = ["changed excluded MCP server"]
    elif failure == "criterion":
        review["acceptanceCriteriaEvidence"].pop()
    elif failure == "test":
        review["testEvidence"][0]["passed"] = False
    else:
        review["verdict"] = "FAIL"
    executor = FakeExecutor(overrides={"review": [review]})
    runner = _runner(root, executor)
    runner.start(_issue(), work_branch="54-test-review-gate", run_id=f"review-{failure}")
    runner.approve(f"review-{failure}")

    with pytest.raises(IssueError, match="phase failed: review"):
        runner.resume(f"review-{failure}")
    assert runner.status(f"review-{failure}")["status"] == "failed"


def test_verification_failure_blocks_review(tmp_path):
    root = _repo(tmp_path)
    verification = _result("verification")
    verification["allTestsPassed"] = False
    executor = FakeExecutor(overrides={"verification": [verification]})
    runner = _runner(root, executor)
    runner.start(_issue(), work_branch="54-test-verification", run_id="verify-fail")
    runner.approve("verify-fail")

    with pytest.raises(IssueError, match="failing tests"):
        runner.resume("verify-fail")
    assert "review" not in executor.calls


def test_verification_requires_every_planned_test(tmp_path):
    root = _repo(tmp_path)
    verification = _result("verification")
    verification["testEvidence"].pop()
    executor = FakeExecutor(overrides={"verification": [verification]})
    runner = _runner(root, executor)
    runner.start(_issue(), work_branch="54-test-test-coverage", run_id="verify-coverage")
    runner.approve("verify-coverage")

    with pytest.raises(IssueError, match="every Test exactly"):
        runner.resume("verify-coverage")
    assert "review" not in executor.calls


def test_repository_local_skill_metadata_and_triggers():
    root = Path(__file__).resolve().parents[3]
    skill_root = root / ".agents" / "skills" / "issue-work-runner"
    skill = skill_root / "SKILL.md"
    content = skill.read_text(encoding="utf-8")
    ui_metadata = (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert content.startswith("---\nname: issue-work-runner\n")
    frontmatter = content.split("---", 2)[1]
    assert "description:" in frontmatter
    assert "Issue #N 작업 시작" in frontmatter
    assert "이 Issue 구현 시작" in frontmatter
    assert "Issue 작업 자동화" in frontmatter
    assert "GitHub connector" in content
    assert "--snapshot-file" in content
    assert "$issue-work-runner" in ui_metadata


def test_codex_exec_adapter_captures_a_fresh_thread(tmp_path, monkeypatch):
    result = _result("analysis")

    def fake_run(command, **kwargs):
        path = Path(command[command.index("--output-last-message") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"thread-new"}\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    request = PhaseRequest(
        run_id="adapter",
        phase="analysis",
        prompt="analyze",
        schema=phase_result_schema("analysis"),
        phase_dir=tmp_path / "phase",
    )
    execution = CodexExecExecutor(tmp_path).execute(request)

    assert execution.thread_id == "thread-new"
    assert execution.result == result
    assert (tmp_path / "phase" / "events.jsonl").is_file()


@pytest.mark.skipif(
    os.environ.get("GDAI_RUN_CODEX_SMOKE") != "1",
    reason="real Codex execution is explicit opt-in",
)
def test_codex_exec_smoke_opt_in(tmp_path):
    root = Path(__file__).resolve().parents[3]
    request = PhaseRequest(
        run_id="smoke",
        phase="analysis",
        prompt="Do not call tools or edit files. Return a minimal valid analysis result.",
        schema=phase_result_schema("analysis"),
        phase_dir=tmp_path / "codex-smoke",
    )
    execution = CodexExecExecutor(root).execute(request)
    assert execution.thread_id
    assert execution.result["phase"] == "analysis"
