"""저장소 루트의 ``.env``를 MCP 프로세스 환경에 적용한다.

실제 프로세스 환경이 항상 우선하며 빈 예시 값은 적용하지 않는다. 테스트는
``GDAI_SKIP_DOTENV``로 로컬 설정의 영향을 차단할 수 있다. 별도 dotenv 의존성 없이
이 저장소의 단순한 ``KEY=VALUE`` 형식만 읽는다.
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
