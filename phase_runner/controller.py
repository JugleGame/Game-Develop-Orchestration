"""Deterministic phase state machine with human approval gates."""

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
from .schemas import MAX_CONTEXT_BYTES, MAX_RESULT_BYTES, phase_result_schema

FORMAT_VERSION = 1
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PHASE_PROFILES = {
    "planning": "research",
    "unity_implementation": "unity",
    "asset2d_generation": "asset2d",
    "asset3d_generation": "asset3d",
    "unity_integration": "unity",
}
GATES = ("planning", "asset-generation", "asset-review")


class PhaseError(RuntimeError):
    pass


class PhaseBlocked(PhaseError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PhaseRunner:
    def __init__(self, root: Path, executor: PhaseExecutor, *, runs_root: Path | None = None):
        self.root = root.resolve()
        self.executor = executor
        self.runs_root = (runs_root or self.root / "var" / "runs").resolve()

    def start(self, prompt: str, *, run_id: str | None = None) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt or len(prompt) > 20_000:
            raise PhaseError("initial prompt must contain 1-20000 characters")
        run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        self._validate_run_id(run_id)
        run_dir = self._run_dir(run_id)
        if run_dir.exists():
            raise PhaseError(f"run already exists: {run_id}")
        run_dir.mkdir(parents=True)
        state = {
            "formatVersion": FORMAT_VERSION,
            "runId": run_id,
            "createdAt": _now(),
            "updatedAt": _now(),
            "initialPrompt": prompt,
            "status": "ready",
            "currentPhase": None,
            "visualDimension": None,
            "assetsRequired": None,
            "approvals": {
                "planning": "pending",
                "asset-generation": "not-required",
                "asset-review": "not-required",
            },
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
            raise PhaseError(f"cannot read run state: {path}") from exc
        if state.get("formatVersion") != FORMAT_VERSION or state.get("runId") != run_id:
            raise PhaseError("unknown or mismatched run state format")
        self._validate_state(state)
        return state

    def status(self, run_id: str) -> dict[str, Any]:
        state = self.load(run_id)
        return self._public_state(state)

    def approve(self, run_id: str, gate: str) -> dict[str, Any]:
        if gate not in GATES:
            raise PhaseError(f"unknown approval gate: {gate}")
        with self._locked(run_id):
            state = self.load(run_id)
            expected = self._pending_gate(state)
            if expected != gate:
                raise PhaseBlocked(f"gate is not awaiting approval: {gate}")
            state["approvals"][gate] = "approved"
            state["status"] = "ready"
            self._save(state)
            return self._public_state(state)

    def reject(self, run_id: str, gate: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise PhaseError("rejection reason must contain 1-2000 characters")
        if gate not in GATES:
            raise PhaseError(f"unknown approval gate: {gate}")
        with self._locked(run_id):
            state = self.load(run_id)
            if self._pending_gate(state) != gate:
                raise PhaseBlocked(f"gate is not awaiting approval: {gate}")
            state["approvals"][gate] = "rejected"
            state["status"] = "rejected"
            state["lastError"] = {"phase": state["currentPhase"], "message": reason}
            self._save(state)
            return self._public_state(state)

    def resume(self, run_id: str, *, max_phases: int | None = None) -> dict[str, Any]:
        with self._locked(run_id):
            state = self.load(run_id)
            if state["status"] in {"failed", "rejected", "completed"}:
                raise PhaseBlocked(f"run is {state['status']}")
            if any(record.get("status") == "running" for record in state["phases"].values()):
                raise PhaseBlocked("a prior phase is still marked running; use retry explicitly")
            executed = 0
            while max_phases is None or executed < max_phases:
                try:
                    phase = self._next_phase(state)
                except PhaseBlocked:
                    if executed:
                        break
                    raise
                if phase is None:
                    state["status"] = "completed"
                    state["currentPhase"] = None
                    self._save(state)
                    break
                self._execute_phase(state, phase)
                executed += 1
            self._set_waiting_status(state)
            return self._public_state(state)

    def retry(self, run_id: str) -> dict[str, Any]:
        with self._locked(run_id):
            state = self.load(run_id)
            failed = [
                name
                for name, record in state["phases"].items()
                if record.get("status") in {"failed", "running"}
            ]
            if state["status"] != "failed" and not failed:
                raise PhaseBlocked("run has no failed or interrupted phase to retry")
            if len(failed) != 1:
                raise PhaseError("run must contain exactly one retryable phase")
            phase = failed[0]
            state["phases"][phase]["status"] = "pending"
            state["status"] = "ready"
            state["lastError"] = None
            self._save(state)
            self._execute_phase(state, phase)
            self._set_waiting_status(state)
            return self._public_state(state)

    def _execute_phase(self, state: dict[str, Any], phase: str) -> None:
        profile = PHASE_PROFILES[phase]
        record = state["phases"].setdefault(
            phase, {"status": "pending", "attempts": 0, "profile": profile}
        )
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
            request = PhaseRequest(
                run_id=state["runId"],
                phase=phase,
                profile=profile,
                prompt=self._prompt(state, phase),
                schema=phase_result_schema(phase),
                phase_dir=self._run_dir(state["runId"]) / "phases" / phase,
            )
            execution = self.executor.execute(request)
            self._validate_result(phase, execution)
            result_path = request.phase_dir / "result.json"
            self._atomic_json(result_path, execution.result)
            record.update(
                {
                    "status": "completed",
                    "completedAt": _now(),
                    "threadId": execution.thread_id,
                    "resultPath": str(result_path),
                }
            )
            if phase == "planning":
                state["visualDimension"] = execution.result["visualDimension"]
                state["assetsRequired"] = execution.result["assetsRequired"]
                if execution.result["assetsRequired"]:
                    state["approvals"]["asset-generation"] = "pending"
                    state["approvals"]["asset-review"] = "pending"
            state["lastError"] = None
            state["status"] = "ready"
            state["currentPhase"] = phase
            self._save(state)
        except Exception as exc:
            record.update({"status": "failed", "failedAt": _now(), "error": str(exc)})
            state["status"] = "failed"
            state["lastError"] = {"phase": phase, "message": str(exc)}
            self._save(state)
            raise PhaseError(f"phase failed: {phase}: {exc}") from exc

    def _next_phase(self, state: dict[str, Any]) -> str | None:
        if not self._completed(state, "planning"):
            return "planning"
        if state["approvals"]["planning"] != "approved":
            state["status"] = "awaiting-planning-approval"
            state["currentPhase"] = "planning"
            self._save(state)
            raise PhaseBlocked("planning approval is required")
        if not self._completed(state, "unity_implementation"):
            return "unity_implementation"
        if state["assetsRequired"]:
            if state["approvals"]["asset-generation"] != "approved":
                state["status"] = "awaiting-asset-generation-approval"
                self._save(state)
                raise PhaseBlocked("asset generation approval is required")
            dimension = state["visualDimension"]
            asset_phases = {
                "2D": ("asset2d_generation",),
                "3D": ("asset3d_generation",),
                "hybrid": ("asset2d_generation", "asset3d_generation"),
            }.get(dimension)
            if asset_phases is None:
                raise PhaseError(f"unknown visual dimension: {dimension}")
            for phase in asset_phases:
                if not self._completed(state, phase):
                    return phase
            if state["approvals"]["asset-review"] != "approved":
                state["status"] = "awaiting-asset-review"
                self._save(state)
                raise PhaseBlocked("asset review approval is required")
        if not self._completed(state, "unity_integration"):
            return "unity_integration"
        return None

    def _prompt(self, state: dict[str, Any], phase: str) -> str:
        if phase == "planning":
            context: dict[str, Any] = {"initialPrompt": state["initialPrompt"]}
        else:
            context = {"completedPhases": {}}
            for name, record in state["phases"].items():
                if record.get("status") == "completed":
                    path = Path(record["resultPath"]).resolve()
                    run_dir = self._run_dir(state["runId"])
                    if not path.is_relative_to(run_dir):
                        raise PhaseError(f"phase result path escapes run directory: {name}")
                    context["completedPhases"][name] = json.loads(path.read_text(encoding="utf-8"))
        packed = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(packed.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise PhaseError("bounded phase context exceeds 65536 bytes")
        instructions = {
            "planning": (
                "Plan the requested game work using only the research MCP. Produce the requested "
                "structured handoff and do not implement Unity or generate assets."
            ),
            "unity_implementation": (
                "Implement the approved Unity work using only the Unity MCP. Do not generate "
                "assets or perform final asset integration."
            ),
            "asset2d_generation": (
                "Generate and validate the approved 2D assets using only the 2D Asset MCP."
            ),
            "asset3d_generation": (
                "Generate and validate the approved 3D assets using only the 3D Asset MCP."
            ),
            "unity_integration": (
                "Perform final Unity asset integration and functional QA using only the Unity MCP."
            ),
        }
        return (
            f"You are executing phase '{phase}' for run '{state['runId']}'.\n"
            f"{instructions[phase]}\n"
            "Return only data conforming to the supplied JSON Schema. Artifact paths must point "
            "to durable local evidence. Context follows:\n" + packed
        )

    @staticmethod
    def _validate_result(phase: str, execution: ExecutionResult) -> None:
        raw = json.dumps(execution.result, ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_RESULT_BYTES:
            raise PhaseError(f"phase result exceeds {MAX_RESULT_BYTES} bytes")
        try:
            validate(execution.result, phase_result_schema(phase))
        except ValidationError as exc:
            raise PhaseError(f"invalid phase result: {exc.message}") from exc
        if not execution.thread_id.strip():
            raise PhaseError("executor returned an empty thread id")

    @staticmethod
    def _completed(state: dict[str, Any], phase: str) -> bool:
        return state["phases"].get(phase, {}).get("status") == "completed"

    @staticmethod
    def _pending_gate(state: dict[str, Any]) -> str | None:
        statuses = state["approvals"]
        if PhaseRunner._completed(state, "planning") and statuses["planning"] == "pending":
            return "planning"
        if (
            PhaseRunner._completed(state, "unity_implementation")
            and state["assetsRequired"]
            and statuses["asset-generation"] == "pending"
        ):
            return "asset-generation"
        asset_names = ("asset2d_generation", "asset3d_generation")
        has_asset = any(PhaseRunner._completed(state, name) for name in asset_names)
        required_done = (
            state["visualDimension"] == "2D"
            and PhaseRunner._completed(state, "asset2d_generation")
            or state["visualDimension"] == "3D"
            and PhaseRunner._completed(state, "asset3d_generation")
            or state["visualDimension"] == "hybrid"
            and all(PhaseRunner._completed(state, name) for name in asset_names)
        )
        if has_asset and required_done and statuses["asset-review"] == "pending":
            return "asset-review"
        return None

    def _set_waiting_status(self, state: dict[str, Any]) -> None:
        gate = self._pending_gate(state)
        if gate:
            state["status"] = {
                "planning": "awaiting-planning-approval",
                "asset-generation": "awaiting-asset-generation-approval",
                "asset-review": "awaiting-asset-review",
            }[gate]
            self._save(state)

    def _public_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in state.items() if key != "initialPrompt"}

    @staticmethod
    def _validate_state(state: dict[str, Any]) -> None:
        allowed_statuses = {
            "ready",
            "running",
            "awaiting-planning-approval",
            "awaiting-asset-generation-approval",
            "awaiting-asset-review",
            "failed",
            "rejected",
            "completed",
        }
        if state.get("status") not in allowed_statuses:
            raise PhaseError(f"unknown run status: {state.get('status')}")
        approvals = state.get("approvals")
        if not isinstance(approvals, dict) or set(approvals) != set(GATES):
            raise PhaseError("invalid approval state")
        known_approval_statuses = {"pending", "approved", "rejected", "not-required"}
        if any(value not in known_approval_statuses for value in approvals.values()):
            raise PhaseError("unknown approval status")
        phases = state.get("phases")
        if not isinstance(phases, dict) or any(name not in PHASE_PROFILES for name in phases):
            raise PhaseError("unknown phase in run state")
        if any(
            not isinstance(record, dict)
            or record.get("status") not in {"pending", "running", "completed", "failed"}
            for record in phases.values()
        ):
            raise PhaseError("unknown phase status")

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
            raise PhaseError("run id must be 1-64 safe filename characters")

    @contextmanager
    def _locked(self, run_id: str) -> Iterator[None]:
        self._validate_run_id(run_id)
        run_dir = self._run_dir(run_id)
        if not run_dir.is_dir():
            raise PhaseError(f"run does not exist: {run_id}")
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
                raise PhaseBlocked(f"run is already active: {run_id}") from exc
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
