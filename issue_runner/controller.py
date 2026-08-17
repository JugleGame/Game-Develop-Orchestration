"""Deterministic state machine for repository Issue analysis, work, and review."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from jsonschema import ValidationError, validate

from .executor import ExecutionResult, PhaseExecutor, PhaseRequest
from .git import GitRepository
from .schemas import (
    MAX_CONTEXT_BYTES,
    MAX_ISSUE_BODY_BYTES,
    MAX_RESULT_BYTES,
    phase_result_schema,
)

FORMAT_VERSION = 1
PHASES = ("analysis", "implementation", "verification", "review")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class IssueError(RuntimeError):
    pass


class IssueBlocked(IssueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class IssueRunner:
    def __init__(
        self,
        root: Path,
        executor: PhaseExecutor,
        *,
        runs_root: Path | None = None,
        git: GitRepository | None = None,
    ) -> None:
        self.root = root.resolve()
        self.executor = executor
        self.runs_root = (runs_root or self.root / "var" / "issue-runs").resolve()
        self.git = git or GitRepository(self.root)

    def start(
        self,
        snapshot: dict[str, Any],
        *,
        work_branch: str,
        base_branch: str = "dev",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        issue = self._validate_snapshot(snapshot)
        run_id = run_id or f"issue-{issue['number']}-" + uuid.uuid4().hex[:8]
        self._validate_run_id(run_id)
        run_dir = self._run_dir(run_id)
        if run_dir.exists():
            raise IssueError(f"run already exists: {run_id}")

        self.git.prepare_branch(issue["number"], base_branch, work_branch)
        run_dir.mkdir(parents=True)
        state = {
            "formatVersion": FORMAT_VERSION,
            "runId": run_id,
            "createdAt": _now(),
            "updatedAt": _now(),
            "status": "ready",
            "currentPhase": None,
            "issue": issue,
            "baseBranch": base_branch,
            "workBranch": work_branch,
            "analysisApproval": "pending",
            "phases": {},
            "lastError": None,
        }
        self._save(state)
        return self.resume(run_id, max_phases=1)

    def load(self, run_id: str) -> dict[str, Any]:
        self._validate_run_id(run_id)
        path = self._run_dir(run_id) / "state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IssueError(f"cannot read run state: {path}") from exc
        if state.get("formatVersion") != FORMAT_VERSION or state.get("runId") != run_id:
            raise IssueError("unknown or mismatched run state format")
        self._validate_state(state)
        return state

    def status(self, run_id: str) -> dict[str, Any]:
        return self.load(run_id)

    def approve(self, run_id: str) -> dict[str, Any]:
        with self._locked(run_id):
            state = self.load(run_id)
            if not self._completed(state, "analysis") or state["analysisApproval"] != "pending":
                raise IssueBlocked("analysis is not awaiting approval")
            state["analysisApproval"] = "approved"
            state["status"] = "ready"
            self._save(state)
            return state

    def reject(self, run_id: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise IssueError("rejection reason must contain 1-2000 characters")
        with self._locked(run_id):
            state = self.load(run_id)
            if not self._completed(state, "analysis") or state["analysisApproval"] != "pending":
                raise IssueBlocked("analysis is not awaiting approval")
            state["analysisApproval"] = "rejected"
            state["status"] = "rejected"
            state["lastError"] = {"phase": "analysis", "message": reason}
            self._save(state)
            return state

    def resume(self, run_id: str, *, max_phases: int | None = None) -> dict[str, Any]:
        with self._locked(run_id):
            state = self.load(run_id)
            if state["status"] in {"failed", "rejected", "completed"}:
                raise IssueBlocked(f"run is {state['status']}")
            if any(record.get("status") == "running" for record in state["phases"].values()):
                raise IssueBlocked("a phase is still marked running; use retry explicitly")
            executed = 0
            while max_phases is None or executed < max_phases:
                phase = self._next_phase(state)
                if phase is None:
                    state["status"] = "completed"
                    state["currentPhase"] = None
                    self._save(state)
                    break
                self._execute_phase(state, phase)
                executed += 1
            if self._completed(state, "analysis") and state["analysisApproval"] == "pending":
                state["status"] = "awaiting-analysis-approval"
                self._save(state)
            return state

    def retry(self, run_id: str) -> dict[str, Any]:
        with self._locked(run_id):
            state = self.load(run_id)
            retryable = [
                name
                for name, record in state["phases"].items()
                if record.get("status") in {"failed", "running"}
            ]
            if len(retryable) != 1:
                raise IssueBlocked("run must contain exactly one failed or interrupted phase")
            phase = retryable[0]
            state["phases"][phase]["status"] = "pending"
            state["status"] = "ready"
            state["lastError"] = None
            self._save(state)
            self._execute_phase(state, phase)
            if phase == "analysis" and state["analysisApproval"] == "pending":
                state["status"] = "awaiting-analysis-approval"
                self._save(state)
            return state

    def _next_phase(self, state: dict[str, Any]) -> str | None:
        if not self._completed(state, "analysis"):
            return "analysis"
        if state["analysisApproval"] != "approved":
            state["status"] = "awaiting-analysis-approval"
            self._save(state)
            raise IssueBlocked("analysis approval is required before implementation")
        for phase in PHASES[1:]:
            if not self._completed(state, phase):
                return phase
        return None

    def _execute_phase(self, state: dict[str, Any], phase: str) -> None:
        record = state["phases"].setdefault(phase, {"status": "pending", "attempts": 0})
        if record["status"] == "completed":
            return
        record.update(
            {
                "status": "running",
                "attempts": int(record.get("attempts", 0)) + 1,
                "startedAt": _now(),
                "error": None,
            }
        )
        state["status"] = "running"
        state["currentPhase"] = phase
        self._save(state)
        try:
            phase_dir = self._run_dir(state["runId"]) / "phases" / phase
            if phase != "analysis":
                self.git.write_diff(state["baseBranch"], phase_dir / "repository.diff")
            request = PhaseRequest(
                run_id=state["runId"],
                phase=phase,
                prompt=self._prompt(state, phase),
                schema=phase_result_schema(phase),
                phase_dir=phase_dir,
            )
            execution = self.executor.execute(request)
            self._validate_result(state, phase, execution)
            result_path = phase_dir / "result.json"
            self._atomic_json(result_path, execution.result)
            record.update(
                {
                    "status": "completed",
                    "completedAt": _now(),
                    "threadId": execution.thread_id,
                    "resultPath": str(result_path),
                }
            )
            state["status"] = "ready"
            state["lastError"] = None
            self._save(state)
        except Exception as exc:
            record.update({"status": "failed", "failedAt": _now(), "error": str(exc)})
            state["status"] = "failed"
            state["lastError"] = {"phase": phase, "message": str(exc)}
            self._save(state)
            raise IssueError(f"phase failed: {phase}: {exc}") from exc

    def _prompt(self, state: dict[str, Any], phase: str) -> str:
        if phase == "analysis":
            context: dict[str, Any] = {
                "issue": state["issue"],
                "baseBranch": state["baseBranch"],
                "workBranch": state["workBranch"],
            }
        else:
            context = {
                "issue": {key: state["issue"][key] for key in ("number", "title", "url")},
                "baseBranch": state["baseBranch"],
                "workBranch": state["workBranch"],
                "completedResults": {},
                "changedFiles": self.git.changed_files(state["baseBranch"]),
                "diffPath": str(
                    self._run_dir(state["runId"]) / "phases" / phase / "repository.diff"
                ),
            }
            for name in PHASES:
                record = state["phases"].get(name, {})
                if record.get("status") == "completed":
                    path = Path(record["resultPath"]).resolve()
                    if not path.is_relative_to(self._run_dir(state["runId"])):
                        raise IssueError(f"phase result path escapes run directory: {name}")
                    context["completedResults"][name] = json.loads(
                        path.read_text(encoding="utf-8")
                    )
        packed = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(packed.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise IssueError(f"bounded phase context exceeds {MAX_CONTEXT_BYTES} bytes")
        instructions = {
            "analysis": (
                "Read README.md, AGENTS.md, the complete supplied Issue contract, and only the "
                "repository documents required for this Issue. Analyze without changing source "
                "files. Copy every Acceptance Criterion and Test into the structured result as "
                "distinct items."
            ),
            "implementation": (
                "Implement only the approved analysis plan. Preserve user changes. Do not commit, "
                "push, open a PR, merge, or modify the GitHub Issue. Return changed paths and "
                "artifact paths."
            ),
            "verification": (
                "Run every test from the approved analysis plus relevant regression checks. "
                "Record one testEvidence item per planned test, using the exact planned test text "
                "as its id."
            ),
            "review": (
                "Review the repository diff against Scope, Out of Scope, every Acceptance "
                "Criterion, and every Test. Use the exact criterion/test text as evidence ids. "
                "Verdict PASS is allowed only when there are no scope violations and every item "
                "has passing evidence."
            ),
        }
        return (
            f"You are the fresh '{phase}' worker for Issue run '{state['runId']}'.\n"
            f"{instructions[phase]}\n"
            "Return only JSON conforming to the supplied schema. Read full logs from artifact "
            "paths; do not paste them into the result. Bounded context follows:\n"
            + packed
        )

    def _validate_result(
        self, state: dict[str, Any], phase: str, execution: ExecutionResult
    ) -> None:
        encoded = json.dumps(execution.result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise IssueError(f"phase result exceeds {MAX_RESULT_BYTES} bytes")
        try:
            validate(execution.result, phase_result_schema(phase))
        except ValidationError as exc:
            raise IssueError(f"invalid phase result: {exc.message}") from exc
        if not execution.thread_id.strip():
            raise IssueError("executor returned an empty thread id")
        if phase == "analysis":
            for name in ("scope", "outOfScope", "acceptanceCriteria", "tests"):
                values = execution.result[name]
                if len(values) != len(set(values)):
                    raise IssueError(f"analysis contains duplicate {name} items")
        if phase == "implementation":
            self._validate_changed_files(state, execution.result)
        if phase == "verification":
            self._validate_verification(state, execution.result)
        if phase == "review":
            self._validate_changed_files(state, execution.result)
            self._validate_review(state, execution.result)

    def _validate_changed_files(
        self, state: dict[str, Any], result: dict[str, Any]
    ) -> None:
        actual = set(self.git.changed_files(state["baseBranch"]))
        reported = set(result["changedFiles"])
        if len(result["changedFiles"]) != len(reported) or reported != actual:
            raise IssueError("reported changedFiles do not match the repository")

    @staticmethod
    def _validate_verification(state: dict[str, Any], result: dict[str, Any]) -> None:
        analysis_path = Path(state["phases"]["analysis"]["resultPath"])
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        entries = result["testEvidence"]
        ids = [entry["id"] for entry in entries]
        if len(ids) != len(set(ids)) or set(ids) != set(analysis["tests"]):
            raise IssueError("verification evidence does not cover every Test exactly")
        if not result["allTestsPassed"] or not all(entry["passed"] for entry in entries):
            raise IssueError("verification reported failing tests")

    @staticmethod
    def _validate_review(state: dict[str, Any], result: dict[str, Any]) -> None:
        analysis_path = Path(state["phases"]["analysis"]["resultPath"])
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        expected_criteria = set(analysis["acceptanceCriteria"])
        expected_tests = set(analysis["tests"])

        def evidence_map(name: str) -> dict[str, bool]:
            entries = result[name]
            ids = [entry["id"] for entry in entries]
            if len(ids) != len(set(ids)):
                raise IssueError(f"review contains duplicate {name} ids")
            return {entry["id"]: entry["passed"] for entry in entries}

        criteria = evidence_map("acceptanceCriteriaEvidence")
        tests = evidence_map("testEvidence")
        if set(criteria) != expected_criteria:
            raise IssueError("review evidence does not cover every Acceptance Criterion exactly")
        if set(tests) != expected_tests:
            raise IssueError("review evidence does not cover every Test exactly")
        if result["scopeViolations"]:
            raise IssueError("review found out-of-scope changes")
        if not all(criteria.values()) or not all(tests.values()):
            raise IssueError("review evidence contains a failing item")
        if result["verdict"] != "PASS":
            raise IssueError("review verdict is not PASS")

    @staticmethod
    def _validate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise IssueError("Issue snapshot must be a JSON object")
        required = {"number", "title", "body", "state", "url"}
        if set(snapshot) != required:
            raise IssueError("Issue snapshot must contain exactly number, title, body, state, url")
        number = snapshot["number"]
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise IssueError("Issue number must be a positive integer")
        if snapshot["state"] != "open":
            raise IssueError("Issue must be open")
        for key in ("title", "body", "url"):
            if not isinstance(snapshot[key], str) or not snapshot[key].strip():
                raise IssueError(f"Issue {key} must be a non-empty string")
        if len(snapshot["body"].encode("utf-8")) > MAX_ISSUE_BODY_BYTES:
            raise IssueError(f"Issue body exceeds {MAX_ISSUE_BODY_BYTES} bytes")
        return {
            "number": number,
            "title": snapshot["title"].strip(),
            "body": snapshot["body"],
            "state": "open",
            "url": snapshot["url"].strip(),
        }

    @staticmethod
    def _completed(state: dict[str, Any], phase: str) -> bool:
        return state["phases"].get(phase, {}).get("status") == "completed"

    @staticmethod
    def _validate_state(state: dict[str, Any]) -> None:
        if state.get("status") not in {
            "ready",
            "running",
            "awaiting-analysis-approval",
            "failed",
            "rejected",
            "completed",
        }:
            raise IssueError(f"unknown run status: {state.get('status')}")
        if state.get("analysisApproval") not in {"pending", "approved", "rejected"}:
            raise IssueError("unknown analysis approval status")
        phases = state.get("phases")
        if not isinstance(phases, dict) or any(name not in PHASES for name in phases):
            raise IssueError("unknown phase in run state")
        if any(
            not isinstance(record, dict)
            or record.get("status") not in {"pending", "running", "completed", "failed"}
            for record in phases.values()
        ):
            raise IssueError("unknown phase status")

    def _save(self, state: dict[str, Any]) -> None:
        state["updatedAt"] = _now()
        self._atomic_json(self._run_dir(state["runId"]) / "state.json", state)

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _run_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise IssueError("run id must be 1-64 safe filename characters")

    @contextmanager
    def _locked(self, run_id: str) -> Iterator[None]:
        self._validate_run_id(run_id)
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise IssueError(f"run does not exist: {run_id}")
        lock_path = run_dir / ".lock"
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise IssueBlocked(f"run is already active: {run_id}") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
