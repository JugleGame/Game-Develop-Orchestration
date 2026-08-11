"""``.env.example`` has to stay the complete list, or it stops being useful.

The file it replaced documented one variable (``GEMINI_API_KEY``) that no
module ever read, while the twenty-odd variables the servers actually consult
were spread across three READMEs. A setup file that is only mostly true costs
more than none: the reader stops checking it.

So this asserts both directions — every ``os.getenv`` name in the server
packages and every ``Settings`` field appears in ``.env.example``, and nothing
in ``.env.example`` is unread. It also pins the loader semantics the file
depends on (blank means unset, exported wins).
"""

from __future__ import annotations

import re
from pathlib import Path

from common.env import parse_env_file

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_SERVERS = REPO_ROOT / "Src" / "McpServers"
SETTINGS_PY = REPO_ROOT / "Src" / "DeveloperAI" / "app" / "config" / "settings.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

GETENV = re.compile(r"os\.getenv\(\s*[\"'](?P<name>[A-Z][A-Z0-9_]*)[\"']")
SETTING = re.compile(r"^    (?P<name>[a-z][a-z0-9_]*)\s*:", re.MULTILINE)

# Read, but deliberately absent from the file:
#   MCP_PORT*        - per-server overrides; the registry table is the source
#   NEON_DSN         - the legacy spelling of RESEARCH_DSN, documented in-line
#   GDAI_*           - read before .env exists, so they cannot come from it
DOCUMENTED_ELSEWHERE = {
    "MCP_PORT",
    "NEON_DSN",
    "GDAI_ENV_FILE",
    "GDAI_SKIP_DOTENV",
    "GDAI_PYTHON",
}

# Settings fields that are not environment configuration.
NOT_ENV_FIELDS = {"model_config"}


def _example_keys() -> set[str]:
    return set(parse_env_file(ENV_EXAMPLE.read_text(encoding="utf-8")))


SKIP_DIRS = {"tests", "site-packages", "node_modules"}


def _server_env_names() -> set[str]:
    names: set[str] = set()
    for path in MCP_SERVERS.rglob("*.py"):
        parts = path.relative_to(MCP_SERVERS).parts
        if any(part.startswith(".") or part in SKIP_DIRS for part in parts):
            continue
        if path.name in {"env.py", "serve_all.py"}:
            continue
        names |= {m.group("name") for m in GETENV.finditer(path.read_text(encoding="utf-8"))}
    return names


def _orchestrator_env_names() -> set[str]:
    body = SETTINGS_PY.read_text(encoding="utf-8")
    body = body.split("@field_validator")[0]
    return {
        m.group("name").upper()
        for m in SETTING.finditer(body)
        if m.group("name") not in NOT_ENV_FIELDS
    }


def test_every_server_variable_is_documented() -> None:
    missing = _server_env_names() - _example_keys() - DOCUMENTED_ELSEWHERE
    assert not missing, f".env.example 에 빠진 MCP 서버 환경변수: {sorted(missing)}"


def test_every_orchestrator_setting_is_documented() -> None:
    missing = _orchestrator_env_names() - _example_keys()
    assert not missing, f".env.example 에 빠진 오케스트레이터 설정: {sorted(missing)}"


def test_example_documents_nothing_unread() -> None:
    known = _server_env_names() | _orchestrator_env_names() | DOCUMENTED_ELSEWHERE
    stale = _example_keys() - known
    assert not stale, f"아무도 읽지 않는 .env.example 항목: {sorted(stale)}"


def test_blank_entries_do_not_override_code_defaults() -> None:
    """A placeholder line must not become an empty environment variable."""

    parsed = parse_env_file("FILLED=value\nBLANK=\n")
    assert parsed == {"FILLED": "value", "BLANK": ""}


def test_parser_handles_export_comments_and_quotes() -> None:
    parsed = parse_env_file(
        "# comment\n"
        "\n"
        "export DSN='postgresql://u:p@h/db?sslmode=require'\n"
        'PATH_WITH_SPACE="C:/Program Files/Unity/Project"\n'
        'CORS_ALLOW_ORIGINS=["http://localhost:5173"]\n'
    )
    assert parsed["DSN"] == "postgresql://u:p@h/db?sslmode=require"
    assert parsed["PATH_WITH_SPACE"] == "C:/Program Files/Unity/Project"
    assert parsed["CORS_ALLOW_ORIGINS"] == '["http://localhost:5173"]'
