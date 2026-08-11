"""Load the repository-root ``.env`` into ``os.environ``.

Every setting in this package is read with ``os.getenv`` at import time, so
until now the only way to configure a tool server was to export the variable
in the shell *before* launching it. That works for path A (``serve_all.py``
inherits the shell), but not for path B: Claude Code spawns the servers from
``.mcp.json`` and passes on whatever environment it was started with, so a
forgotten export surfaces minutes later as an empty ``RESEARCH_DSN``.

``common/__init__.py`` calls :func:`load_repo_env` before any server module
body runs, which makes the committed ``.env.example`` -> ``.env`` file the one
place a new machine has to fill in.

Two rules keep this from surprising anyone:

* **A real environment variable always wins.** Anything already exported —
  CI secrets, a one-off ``GIT_ROOT=... python -m gitmcp.server`` — is left
  alone. An empty value counts as unset, because ``.mcp.json``-style
  ``${VAR:-}`` expansion produces empty strings for variables nobody set.
* **Tests opt out** via ``GDAI_SKIP_DOTENV``, so a developer's local ``.env``
  cannot change what the suite asserts.

No dependency is added for this: ``python-dotenv`` belongs to the
orchestrator's distribution, not this one, and the format below is the subset
of it that a ``.env`` written from ``.env.example`` actually uses.
"""

from __future__ import annotations

import os
from pathlib import Path

# common/env.py -> common -> McpServers -> Src -> repository root
REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_ENV_FILE = REPO_ROOT / ".env"


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, skipping comments and blanks.

    ``export`` prefixes are tolerated so the same file can be sourced from a
    shell, and matching outer quotes are stripped so values containing ``#``
    or spaces (a DSN, a Windows path) survive intact.
    """

    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_repo_env(path: Path | None = None) -> dict[str, str]:
    """Apply the repository ``.env`` to ``os.environ``; return what was set.

    ``GDAI_ENV_FILE`` overrides the location, which is how a second checkout
    or a CI runner points at its own file without editing anything.
    """

    if os.getenv("GDAI_SKIP_DOTENV"):
        return {}

    target = path or Path(os.getenv("GDAI_ENV_FILE") or DEFAULT_ENV_FILE)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A missing .env is the normal case for a checkout that configures
        # everything through the shell; it must not stop a server from booting.
        return {}

    applied: dict[str, str] = {}
    for key, value in parse_env_file(text).items():
        # An empty entry means "not configured on this machine". Exporting it
        # as "" would defeat every os.getenv(key, default) in the servers, so
        # the placeholder lines in .env.example stay inert.
        if not value or os.environ.get(key):
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


__all__ = ["DEFAULT_ENV_FILE", "REPO_ROOT", "load_repo_env", "parse_env_file"]
