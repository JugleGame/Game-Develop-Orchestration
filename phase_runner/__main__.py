"""Command line interface for the local deterministic Phase Runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .controller import PhaseBlocked, PhaseError, PhaseRunner
from .executor import CodexExecExecutor


def _runner(args: argparse.Namespace) -> PhaseRunner:
    root = Path(args.root).resolve()
    runs_root = Path(args.runs_root).resolve() if args.runs_root else None
    return PhaseRunner(root, CodexExecExecutor(root), runs_root=runs_root)


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="orchestration repository root")
    parser.add_argument("--runs-root", help="override var/runs (primarily for testing)")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="create a run and execute its planning phase")
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt")
    source.add_argument("--prompt-file")
    start.add_argument("--run-id")

    for name in ("status", "resume", "retry"):
        command = sub.add_parser(name)
        command.add_argument("run_id")

    for name in ("approve", "reject"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("gate", choices=("planning", "asset-generation", "asset-review"))
        if name == "reject":
            command.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    runner = _runner(args)
    try:
        if args.command == "start":
            prompt = args.prompt
            if args.prompt_file:
                prompt = Path(args.prompt_file).read_text(encoding="utf-8")
            _print(runner.start(prompt, run_id=args.run_id))
        elif args.command == "status":
            _print(runner.status(args.run_id))
        elif args.command == "resume":
            _print(runner.resume(args.run_id))
        elif args.command == "retry":
            _print(runner.retry(args.run_id))
        elif args.command == "approve":
            _print(runner.approve(args.run_id, args.gate))
        else:
            _print(runner.reject(args.run_id, args.gate, args.reason))
        return 0
    except PhaseBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, PhaseError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
