"""Replaceable phase executors; production uses Codex non-interactive mode."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class PhaseRequest:
    run_id: str
    phase: str
    profile: str
    prompt: str
    schema: dict[str, Any]
    phase_dir: Path


@dataclass(frozen=True)
class ExecutionResult:
    thread_id: str
    result: dict[str, Any]


class PhaseExecutor(Protocol):
    def execute(self, request: PhaseRequest) -> ExecutionResult: ...


class CodexExecError(RuntimeError):
    """Raised when Codex cannot produce a valid phase result file."""


class CodexExecExecutor:
    """Run every phase as a fresh ``codex exec`` thread.

    Profile application is injected so tests never alter local MCP configuration.
    """

    def __init__(
        self,
        root: Path,
        *,
        codex_command: str = "codex",
        profile_applier: Callable[[str], int] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.codex_command = codex_command
        self.profile_applier = profile_applier or self._default_profile_applier

    @staticmethod
    def _default_profile_applier(profile: str) -> int:
        from scripts.bootstrap import repair_mcp_config

        return repair_mcp_config(profile)

    def execute(self, request: PhaseRequest) -> ExecutionResult:
        if self.profile_applier(request.profile):
            raise CodexExecError(f"failed to apply MCP profile: {request.profile}")

        request.phase_dir.mkdir(parents=True, exist_ok=True)
        schema_path = request.phase_dir / "output-schema.json"
        result_path = request.phase_dir / "result.json"
        events_path = request.phase_dir / "events.jsonl"
        stderr_path = request.phase_dir / "stderr.log"
        schema_path.write_text(
            json.dumps(request.schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        command = [
            self.codex_command,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
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
            )
        except OSError as exc:
            raise CodexExecError(f"cannot start codex exec: {exc}") from exc
        events_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            raise CodexExecError(
                f"codex exec failed with exit code {completed.returncode}; see {stderr_path}"
            )
        thread_id = self._thread_id(completed.stdout)
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexExecError(f"invalid Codex result file: {result_path}") from exc
        if not isinstance(result, dict):
            raise CodexExecError("Codex phase result must be a JSON object")
        return ExecutionResult(thread_id=thread_id, result=result)

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
