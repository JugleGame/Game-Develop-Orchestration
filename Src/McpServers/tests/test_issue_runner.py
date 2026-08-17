"""Issue Work Runner tests use fake Codex execution and temporary Git repositories."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
from issue_runner.executor import CodexExecError
from issue_runner.schemas import MAX_CONTEXT_BYTES, MAX_RESULT_BYTES, phase_result_schema


CRITERIA = ["runner completes", "tests pass"]
TESTS = ["pytest issue runner", "contract check"]


def _issue(*, number: int = 54, state: str = "open") -> dict[str, Any]:
    return {
        "number": number,
        "title": "[pipeline] Issue Work Runner 추가",
        "body": (
            "## 목표\nRunner 추가\n"
            "## 범위\n- 코드\n"
            "## 범위 제외\n- MCP 변경\n"
            "## 완료 조건\n- runner completes\n- tests pass\n"
            "## 검증\n- pytest issue runner\n- contract check"
        ),
        "state": state,
        "url": f"https://github.com/owner/repo/issues/{number}",
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
                    {
                        "id": item,
                        "passed": True,
                        "evidence": "ok",
                        "artifactPaths": [f"var/evidence/verification-test-{index}.txt"],
                    }
                    for index, item in enumerate(TESTS)
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
                    {
                        "id": item,
                        "passed": True,
                        "evidence": "ok",
                        "artifactPaths": [f"var/evidence/review-criterion-{index}.txt"],
                    }
                    for index, item in enumerate(CRITERIA)
                ],
                "testEvidence": [
                    {
                        "id": item,
                        "passed": True,
                        "evidence": "ok",
                        "artifactPaths": [f"var/evidence/review-test-{index}.txt"],
                    }
                    for index, item in enumerate(TESTS)
                ],
                "findings": [],
            }
        )
    return result


@dataclass
class FakeExecutor:
    calls: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    sandboxes: list[str] = field(default_factory=list)
    overrides: dict[str, list[dict[str, Any] | Exception]] = field(default_factory=dict)
    mutate_phases: set[str] = field(default_factory=set)
    commit_phases: set[str] = field(default_factory=set)
    create_artifacts: bool = True

    def execute(self, request: PhaseRequest) -> ExecutionResult:
        self.calls.append(request.phase)
        self.prompts.append(request.prompt)
        self.sandboxes.append(request.sandbox)
        root = request.phase_dir.parents[4]
        if request.phase == "implementation":
            changed = root / "issue_runner" / "controller.py"
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.write_text("implemented\n", encoding="utf-8")
        if request.phase in self.mutate_phases:
            changed = root / "issue_runner" / "controller.py"
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.write_text(f"mutated by {request.phase}\n", encoding="utf-8")
        if request.phase in self.commit_phases:
            _git(root, "add", "--all")
            _git(root, "commit", "-m", f"forbidden {request.phase} commit")
        queued = self.overrides.get(request.phase, [])
        value = queued.pop(0) if queued else _result(request.phase)
        if isinstance(value, Exception):
            raise value
        if self.create_artifacts:
            paths = list(value["artifactPaths"])
            for field in ("testEvidence", "acceptanceCriteriaEvidence"):
                for evidence in value.get(field, []):
                    paths.extend(evidence["artifactPaths"])
            for raw_path in paths:
                artifact = root / raw_path
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("evidence\n", encoding="utf-8")
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
    assert executor.sandboxes == [
        "read-only",
        "workspace-write",
        "workspace-write",
        "read-only",
    ]
    assert len({item["threadId"] for item in state["phases"].values()}) == 4
    assert all(
        not Path(record["resultPath"]).is_absolute()
        for record in state["phases"].values()
    )
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
    with pytest.raises(GitError, match="must match"):
        repository.prepare_branch(54, "dev", "54-test-runner-2")

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


@pytest.mark.parametrize("phase", ["analysis", "verification", "review"])
def test_non_implementation_phases_cannot_modify_source(tmp_path, phase):
    root = _repo(tmp_path)
    executor = FakeExecutor(mutate_phases={phase})
    runner = _runner(root, executor)
    run_id = f"immutable-{phase}"

    if phase == "analysis":
        with pytest.raises(IssueError, match="modified repository source"):
            runner.start(_issue(), work_branch="54-test-immutable", run_id=run_id)
    else:
        runner.start(_issue(), work_branch="54-test-immutable", run_id=run_id)
        runner.approve(run_id)
        with pytest.raises(IssueError, match="modified repository source"):
            runner.resume(run_id)
    assert runner.status(run_id)["status"] == "failed"


def test_retry_rejects_source_left_by_failed_read_only_phase(tmp_path):
    root = _repo(tmp_path)
    executor = FakeExecutor(mutate_phases={"analysis"})
    runner = _runner(root, executor)
    with pytest.raises(IssueError, match="modified repository source"):
        runner.start(_issue(), work_branch="54-test-retry-integrity", run_id="integrity")
    executor.mutate_phases.clear()

    with pytest.raises(IssueError, match="restore it before retry"):
        runner.retry("integrity")


def test_phase_created_commit_is_rejected(tmp_path):
    root = _repo(tmp_path)
    executor = FakeExecutor(commit_phases={"implementation"})
    runner = _runner(root, executor)
    runner.start(_issue(), work_branch="54-test-no-commit", run_id="no-commit")
    runner.approve("no-commit")

    with pytest.raises(IssueError, match="commits are not allowed"):
        runner.resume("no-commit")
    assert runner.status("no-commit")["status"] == "failed"


def test_resume_rejects_another_branch_at_the_same_commit(tmp_path):
    root = _repo(tmp_path)
    runner = _runner(root, FakeExecutor())
    runner.start(_issue(), work_branch="54-test-resume-branch", run_id="branch")
    runner.approve("branch")
    _git(root, "switch", "dev")

    with pytest.raises(IssueError, match="current branch must be"):
        runner.resume("branch")
    assert runner.status("branch")["status"] == "failed"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("var/evidence/missing.json", "does not exist"),
        ("../outside.json", "repository-relative"),
        ("var\\evidence\\analysis.json", "POSIX separators"),
    ],
)
def test_artifact_paths_must_be_existing_portable_repo_paths(tmp_path, path, message):
    root = _repo(tmp_path)
    result = _result("analysis")
    result["artifactPaths"] = [path]
    executor = FakeExecutor(
        overrides={"analysis": [result]},
        create_artifacts=False,
    )

    with pytest.raises(IssueError, match=message):
        _runner(root, executor).start(
            _issue(), work_branch="54-test-artifact-path", run_id="artifact"
        )


def test_result_budget_guarantees_room_for_three_phase_handoffs():
    assert MAX_CONTEXT_BYTES - 3 * MAX_RESULT_BYTES >= 16 * 1024


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda issue: issue.update(
                body="## 목표\n목표\n## 범위\n범위\n## 범위 제외\n제외"
            ),
            "Acceptance Criteria",
        ),
        (
            lambda issue: issue.update(url="https://github.com/owner/repo/issues/999"),
            "matching github.com",
        ),
    ],
)
def test_issue_contract_sections_and_url_are_validated(tmp_path, mutation, message):
    root = _repo(tmp_path)
    issue = _issue()
    mutation(issue)

    with pytest.raises(IssueError, match=message):
        _runner(root, FakeExecutor()).start(
            issue, work_branch="54-test-issue-contract", run_id="contract"
        )


def test_runner_works_from_a_different_unicode_checkout_path(tmp_path):
    checkout_parent = tmp_path / "다른 사용자 작업 공간"
    checkout_parent.mkdir()
    root = _repo(checkout_parent)
    runner = _runner(root, FakeExecutor())
    runner.start(_issue(), work_branch="54-test-portable-checkout", run_id="portable")
    runner.approve("portable")

    assert runner.resume("portable")["status"] == "completed"


def test_resume_uses_repository_relative_state_paths_from_another_cwd(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    runner = _runner(root, FakeExecutor())
    runner.start(_issue(), work_branch="54-test-relative-state", run_id="relative")
    runner.approve("relative")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert runner.resume("relative")["status"] == "completed"


def test_module_cli_runs_from_a_copied_checkout(tmp_path):
    source_root = Path(__file__).resolve().parents[3]
    checkout = tmp_path / "복사된 저장소"
    checkout.mkdir()
    shutil.copytree(source_root / "issue_runner", checkout / "issue_runner")

    completed = subprocess.run(
        [sys.executable, "-m", "issue_runner", "--help"],
        cwd=checkout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    assert "start" in completed.stdout
    assert str(source_root) not in completed.stdout


def test_repository_local_skill_metadata_and_triggers():
    root = Path(__file__).resolve().parents[3]
    skill_root = root / ".agents" / "skills" / "issue-work-runner"
    skill = skill_root / "SKILL.md"
    content = skill.read_text(encoding="utf-8")
    ui_metadata = (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert content.startswith("---\nname: issue-work-runner\n")
    frontmatter = content.split("---", 2)[1]
    description_line = next(
        line for line in frontmatter.splitlines() if line.startswith("description: ")
    )
    description = json.loads(description_line.removeprefix("description: "))
    assert "Issue #N 작업 시작" in description
    assert "이 Issue 구현 시작" in description
    assert "Issue 작업 자동화" in description
    assert "omit an Issue number" in description
    assert "GitHub connector" in content
    assert "--snapshot-file" in content
    assert "이 작업으로 Issue 생성해줘" in content
    assert "Issue #<created-number> 작업 시작" in content
    assert "infer it from the current branch" in content
    assert "$issue-work-runner" in ui_metadata


def test_issue_entry_guard_provides_copyable_corrections():
    root = Path(__file__).resolve().parents[3]
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    operations = (root / "docs" / "operations.md").read_text(encoding="utf-8")
    workflow = (root / "docs" / "github-issue-workflow.md").read_text(
        encoding="utf-8"
    )

    for content in (agents, operations, workflow):
        assert "이 작업으로 Issue 생성해줘" in content
        assert "Issue #<number> 작업 시작" in content
    assert "do not edit files, create a branch, or start a runner" in agents
    assert "current conversation has already established the open Issue" in agents
    assert "Issue #<created-number> 작업 시작" in operations
    assert "Issue creation alone must not start implementation" in workflow


def test_codex_exec_adapter_captures_a_fresh_thread(tmp_path, monkeypatch):
    result = _result("analysis")

    def fake_run(command, **kwargs):
        fake_run.command = command
        fake_run.kwargs = kwargs
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
        sandbox="read-only",
    )
    execution = CodexExecExecutor(tmp_path).execute(request)

    assert execution.thread_id == "thread-new"
    assert execution.result == result
    assert fake_run.command[fake_run.command.index("--sandbox") + 1] == "read-only"
    assert fake_run.kwargs["timeout"] == 3600
    assert (tmp_path / "phase" / "events.jsonl").is_file()


def test_codex_exec_timeout_removes_stale_result_and_saves_partial_logs(
    tmp_path, monkeypatch
):
    phase_dir = tmp_path / "phase"
    phase_dir.mkdir()
    stale = phase_dir / "result.json"
    stale.write_text('{"stale":true}', encoding="utf-8")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd="codex exec", timeout=1, output="partial events", stderr="timed out"
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    request = PhaseRequest(
        run_id="timeout",
        phase="analysis",
        prompt="analyze",
        schema=phase_result_schema("analysis"),
        phase_dir=phase_dir,
        sandbox="read-only",
    )

    with pytest.raises(CodexExecError, match="timed out"):
        CodexExecExecutor(tmp_path, timeout_seconds=1).execute(request)
    assert not stale.exists()
    assert (phase_dir / "events.jsonl").read_text(encoding="utf-8") == "partial events"
    assert (phase_dir / "stderr.log").read_text(encoding="utf-8") == "timed out"


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
