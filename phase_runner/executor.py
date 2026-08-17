"""Replaceable phase executors; production uses Codex non-interactive mode."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

CODEX_COMMAND_ENV = "GDAI_CODEX_COMMAND"
CODEX_INSTALL_GUIDANCE = (
    "Run `npm install -g @openai/codex`, sign in with `codex`, or set GDAI_CODEX_COMMAND "
    "to an executable Codex CLI path. The Codex desktop app's protected WindowsApps binary "
    "is not a standalone CLI installation."
)
MAX_FAILURE_DETAIL_CHARS = 1200


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
        codex_command: str | None = None,
        profile_applier: Callable[[str], int] | None = None,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        environment: Mapping[str, str] | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.root = root.resolve()
        self.codex_command = codex_command
        self.profile_applier = profile_applier or self._default_profile_applier
        self.process_runner = process_runner or subprocess.run
        self.environment = environment if environment is not None else os.environ
        self.platform_name = platform_name or os.name
        self._resolved_command: tuple[str, ...] | None = None

    @staticmethod
    def _default_profile_applier(profile: str) -> int:
        from scripts.bootstrap import repair_mcp_config

        return repair_mcp_config(profile)

    def execute(self, request: PhaseRequest) -> ExecutionResult:
        codex_command = self.validate_command()
        if self.profile_applier(request.profile):
            raise CodexExecError(f"failed to apply MCP profile: {request.profile}")

        request.phase_dir.mkdir(parents=True, exist_ok=True)
        schema_path = request.phase_dir / "output-schema.json"
        result_path = request.phase_dir / "result.json"
        events_path = request.phase_dir / "events.jsonl"
        stderr_path = request.phase_dir / "stderr.log"
        # A retry reuses the phase directory. Never accept an artifact left by
        # an earlier attempt when the current Codex process fails to replace it.
        for stale_path in (result_path, events_path, stderr_path):
            stale_path.unlink(missing_ok=True)
        schema_path.write_text(
            json.dumps(request.schema, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        command = [
            *codex_command,
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
            completed = self.process_runner(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                **self._background_process_options(),
            )
        except OSError as exc:
            raise CodexExecError(f"cannot start codex exec: {exc}") from exc
        events_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            detail = _execution_failure_detail(completed.stdout, completed.stderr)
            raise CodexExecError(
                f"codex exec failed with exit code {completed.returncode}: {detail}; "
                f"logs: {events_path}, {stderr_path}"
            )
        thread_id = self._thread_id(completed.stdout)
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexExecError(f"invalid Codex result file: {result_path}") from exc
        if not isinstance(result, dict):
            raise CodexExecError("Codex phase result must be a JSON object")
        return ExecutionResult(thread_id=thread_id, result=result)

    def validate_command(self, *, force: bool = False) -> tuple[str, ...]:
        """Return a runnable Codex CLI command prefix or fail with actionable guidance."""

        if self._resolved_command is not None and not force:
            return self._resolved_command
        self._resolved_command = None
        failures: list[str] = []
        for candidate in self._command_candidates():
            command = (candidate,)
            try:
                result = self.process_runner(
                    [*command, "--version"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    **self._background_process_options(),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{candidate}: {exc}")
                continue
            if result.returncode == 0:
                self._resolved_command = command
                return command
            detail = (result.stderr or result.stdout or "no diagnostic output").strip()
            failures.append(f"{candidate}: exit {result.returncode}: {detail[:500]}")
        detail = "; ".join(failures[:4]) or "no codex executable was found on PATH"
        raise CodexExecError(f"Codex CLI is not executable: {detail}. {CODEX_INSTALL_GUIDANCE}")

    def _command_candidates(self) -> list[str]:
        explicit = self.codex_command or self.environment.get(CODEX_COMMAND_ENV)
        if explicit:
            return [explicit]
        if self.platform_name != "nt":
            discovered = shutil.which("codex", path=self.environment.get("PATH"))
            return [discovered] if discovered else []

        candidates: list[str] = []
        seen: set[str] = set()
        suffixes = (".com", ".exe", ".bat", ".cmd", "")
        raw_directories = self.environment.get("PATH", "").split(os.pathsep)
        npm_prefix = self.environment.get("NPM_CONFIG_PREFIX")
        app_data = self.environment.get("APPDATA")
        if npm_prefix:
            raw_directories.append(npm_prefix)
        if app_data:
            raw_directories.append(str(Path(app_data) / "npm"))
        seen_directories: set[str] = set()
        for raw_directory in raw_directories:
            directory = raw_directory.strip().strip('"')
            directory_key = directory.casefold()
            if not directory or directory_key in seen_directories:
                continue
            seen_directories.add(directory_key)
            for suffix in suffixes:
                path = Path(directory) / f"codex{suffix}"
                try:
                    if not path.is_file():
                        continue
                    value = str(path.resolve())
                except OSError:
                    continue
                key = value.casefold()
                if key not in seen:
                    seen.add(key)
                    candidates.append(value)
        return candidates

    def _background_process_options(self) -> dict[str, int]:
        """Prevent CLI child processes from opening a console beside the GUI."""

        if self.platform_name != "nt":
            return {}
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


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


def _execution_failure_detail(events: str, stderr: str) -> str:
    """Extract a bounded, user-facing failure from Codex JSONL or stderr."""

    for line in reversed(events.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        detail = _nested_error_message(event.get("error") or event.get("message"))
        if detail:
            return _safe_detail(detail)

    stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if stderr_lines:
        return _safe_detail(stderr_lines[-1])
    return "Codex produced no diagnostic message"


def _nested_error_message(value: Any) -> str:
    """Unwrap error objects and JSON-encoded error strings emitted by Codex."""

    for _depth in range(4):
        if isinstance(value, dict):
            error = value.get("error")
            if error is not None and error is not value:
                value = error
                continue
            code = value.get("code")
            message = value.get("message")
            if message:
                if code:
                    return f"{code}: {message}"
                if isinstance(message, str):
                    try:
                        decoded_message = json.loads(message.strip())
                    except json.JSONDecodeError:
                        return message
                    if decoded_message != message:
                        value = decoded_message
                        continue
                return str(message)
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str):
            stripped = value.strip()
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
            if decoded == value:
                return stripped
            value = decoded
            continue
        if value is not None:
            return str(value)
        return ""
    return str(value)


def _safe_detail(detail: str) -> str:
    """Keep dialogs readable and avoid echoing common credential formats."""

    normalized = " ".join(detail.split())
    normalized = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", normalized)
    normalized = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", "Bearer [REDACTED]", normalized
    )
    if len(normalized) > MAX_FAILURE_DETAIL_CHARS:
        return normalized[: MAX_FAILURE_DETAIL_CHARS - 1] + "…"
    return normalized
