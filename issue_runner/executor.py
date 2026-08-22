"""Replaceable executors; production starts a fresh Codex thread per Issue phase."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class PhaseRequest:
    run_id: str
    phase: str
    prompt: str
    schema: dict[str, Any]
    phase_dir: Path
    sandbox: str = "workspace-write"


@dataclass(frozen=True)
class ExecutionResult:
    thread_id: str
    result: dict[str, Any]


class PhaseExecutor(Protocol):
    def execute(self, request: PhaseRequest) -> ExecutionResult: ...


class CodexExecError(RuntimeError):
    pass


class CodexExecExecutor:
    def __init__(
        self,
        root: Path,
        *,
        codex_command: str = "codex",
        timeout_seconds: int = 3600,
    ) -> None:
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self.root = root.resolve()
        self.codex_command = codex_command
        self.timeout_seconds = timeout_seconds

    def execute(self, request: PhaseRequest) -> ExecutionResult:
        request.phase_dir.mkdir(parents=True, exist_ok=True)
        schema_path = request.phase_dir / "output-schema.json"
        result_path = request.phase_dir / "result.json"
        events_path = request.phase_dir / "events.jsonl"
        stderr_path = request.phase_dir / "stderr.log"
        result_path.unlink(missing_ok=True)
        schema_path.write_text(
            json.dumps(request.schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        command = [
            self.codex_command,
            "exec",
            "--json",
            "--sandbox",
            request.sandbox,
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            request.prompt,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            events_path.write_text(self._timeout_output(exc.stdout), encoding="utf-8")
            stderr_path.write_text(self._timeout_output(exc.stderr), encoding="utf-8")
            raise CodexExecError(
                f"codex exec timed out after {self.timeout_seconds} seconds; see {stderr_path}"
            ) from exc
        except OSError as exc:
            raise CodexExecError(f"cannot start codex exec: {exc}") from exc
        events_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            raise CodexExecError(
                f"codex exec failed with exit code {completed.returncode}; see {stderr_path}"
            )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexExecError(f"invalid Codex result file: {result_path}") from exc
        if not isinstance(result, dict):
            raise CodexExecError("Codex phase result must be a JSON object")
        return ExecutionResult(self._thread_id(completed.stdout), result)

    @staticmethod
    def _timeout_output(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    @staticmethod
    def _thread_id(events: str) -> str:
        for line in events.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
        raise CodexExecError("codex exec did not report a thread.started event")
