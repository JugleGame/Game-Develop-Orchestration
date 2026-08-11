"""Start every MCP tool server on Streamable HTTP, the way path A dials them.

The orchestrator (``app/mcp/base_client.py``) talks to these servers over
Streamable HTTP at the ports in ``common/registry.py``. Nothing in the
repository actually started them that way: ``docker-compose.yml`` brings up
Postgres and Redis only, and ``scripts/dev_server.py`` points at
``scripts/mock_mcp_servers.py``, which are stand-ins. Path B (Claude Code)
spawns the same modules over stdio via ``.mcp.json``, so it never exercised
this transport either.

That is what this script is for. It is also the prerequisite for
``verify_contract.py``, which dials the same URLs.

Usage::

    python serve_all.py                  # all five
    python serve_all.py qa git           # only some
    python serve_all.py --list           # show the table and exit

Configuration comes from the repository-root ``.env`` (applied by
``common/env.py`` when each server imports ``common``); anything exported in
this shell overrides it and is inherited by the children. Ctrl-C stops all of
them.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from common import registry
from common.console import use_utf8_output

use_utf8_output()

HERE = Path(__file__).resolve().parent

# strategic refuses to start without a research DB; the others have no such
# requirement. Naming it here turns a stack trace on a background process into
# one line of advice before anything launches.
REQUIRED_ENV: dict[str, tuple[str, ...]] = {"strategic": ("RESEARCH_DSN", "NEON_DSN")}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "keys",
        nargs="*",
        choices=[spec.key for spec in registry.SERVERS] or None,
        help="servers to start (default: all)",
    )
    parser.add_argument("--list", action="store_true", help="print the server table and exit")
    return parser.parse_args()


def _warn_missing_env(keys: list[str]) -> None:
    for key in keys:
        names = REQUIRED_ENV.get(key, ())
        if names and not any(os.getenv(name) for name in names):
            joined = " or ".join(names)
            print(
                f"  ! {key}: {joined} is not set — this server will exit on startup.",
                file=sys.stderr,
            )


def _launch(key: str) -> subprocess.Popen[bytes]:
    spec = registry.get(key)
    return subprocess.Popen(
        [sys.executable, "-m", spec.module, "--transport", "streamable-http"],
        cwd=HERE,
        # The servers import `common`, `qa`, ... as top-level packages, which
        # only resolve when this directory is on the path.
        env={**os.environ, "PYTHONPATH": str(HERE)},
    )


def main() -> int:
    args = _parse_args()

    if args.list:
        for spec in registry.SERVERS:
            print(f"{spec.key:<10} {spec.server_name:<20} {spec.url()}")
        return 0

    keys = list(args.keys) or [spec.key for spec in registry.SERVERS]
    _warn_missing_env(keys)

    processes: dict[str, subprocess.Popen[bytes]] = {}
    for key in keys:
        processes[key] = _launch(key)
        print(f"  -> {key} pid={processes[key].pid} {registry.get(key).url()}")

    print("\nCtrl-C to stop all.\n")

    try:
        while processes:
            for key, proc in list(processes.items()):
                code = proc.poll()
                if code is None:
                    continue
                # A server that dies on its own is the interesting case — it
                # means the pipeline would fail on its first call to that tool.
                print(f"  !! {key} exited with code {code}", file=sys.stderr)
                processes.pop(key)
            time.sleep(0.5)
        print("All servers exited.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopping ...", file=sys.stderr)
        for proc in processes.values():
            proc.send_signal(signal.SIGTERM)
        for proc in processes.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
