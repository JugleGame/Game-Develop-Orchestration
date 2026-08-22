"""CLI for deterministic, approval-gated repository Issue work."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .controller import IssueBlocked, IssueError, IssueRunner
from .executor import CodexExecExecutor
from .git import GitError


def _runner(args: argparse.Namespace) -> IssueRunner:
    root = Path(args.root).resolve()
    runs_root = Path(args.runs_root).resolve() if args.runs_root else None
    return IssueRunner(root, CodexExecExecutor(root), runs_root=runs_root)


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="orchestration repository root")
    parser.add_argument("--runs-root", help="override var/issue-runs (primarily for testing)")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="prepare a branch and execute Issue analysis")
    start.add_argument("--snapshot-file", required=True)
    start.add_argument("--branch", required=True)
    start.add_argument("--base", default="dev")
    start.add_argument("--run-id")

    for name in ("status", "approve", "resume", "retry"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
    reject = sub.add_parser("reject")
    reject.add_argument("run_id")
    reject.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    runner = _runner(args)
    try:
        if args.command == "start":
            snapshot = json.loads(Path(args.snapshot_file).read_text(encoding="utf-8"))
            _print(
                runner.start(
                    snapshot,
                    work_branch=args.branch,
                    base_branch=args.base,
                    run_id=args.run_id,
                )
            )
        elif args.command == "status":
            _print(runner.status(args.run_id))
        elif args.command == "approve":
            _print(runner.approve(args.run_id))
        elif args.command == "resume":
            _print(runner.resume(args.run_id))
        elif args.command == "retry":
            _print(runner.retry(args.run_id))
        else:
            _print(runner.reject(args.run_id, args.reason))
        return 0
    except IssueBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (GitError, IssueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
