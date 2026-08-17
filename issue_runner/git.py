"""Small native-Git boundary for safe Issue branch preparation and diff evidence."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


BRANCH_PATTERN = re.compile(
    r"^(?P<number>[1-9][0-9]*)-(?P<type>feat|fix|refactor|test|docs|chore)-"
    r"(?P<description>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)


class GitError(RuntimeError):
    pass


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _run(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed: {message}")
        return completed.stdout.strip()

    def prepare_branch(self, issue_number: int, base_branch: str, work_branch: str) -> None:
        match = BRANCH_PATTERN.fullmatch(work_branch)
        if not match or int(match.group("number")) != issue_number:
            raise GitError(
                "work branch must match <issue-number>-<type>-<short-description>"
            )
        if base_branch != "dev":
            raise GitError("Issue work must start from base branch dev")
        if self._run("branch", "--show-current") != base_branch:
            raise GitError(f"current branch must be {base_branch}")
        if self._run("status", "--porcelain=v1", "--untracked-files=all"):
            raise GitError("worktree must be clean before creating the Issue branch")
        existing = self._run(
            "branch", "--all", "--list", work_branch, f"remotes/origin/{work_branch}"
        )
        if existing:
            raise GitError(f"work branch already exists: {work_branch}")
        self._run("switch", "-c", work_branch)

    def changed_files(self, base_branch: str) -> list[str]:
        output = self._run(
            "-c",
            "core.quotepath=false",
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=ACMRDTUXB",
            f"{base_branch}...HEAD",
        )
        staged_or_unstaged = self._run(
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        files = {path for path in output.split("\0") if path}
        entries = staged_or_unstaged.split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry:
                index += 1
                continue
            if len(entry) >= 4:
                status = entry[:2]
                files.add(entry[3:].replace("\\", "/"))
                if "R" in status or "C" in status:
                    index += 1
            index += 1
        return sorted(files)

    def write_diff(self, base_branch: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        diff = self._run("diff", "--binary", f"{base_branch}...HEAD")
        working = self._run("diff", "--binary")
        staged = self._run("diff", "--binary", "--cached")
        untracked = self._run(
            "-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard"
        )
        untracked_note = f"# Untracked files\n{untracked}" if untracked else ""
        destination.write_text(
            "\n".join(part for part in (diff, staged, working, untracked_note) if part),
            encoding="utf-8",
        )
