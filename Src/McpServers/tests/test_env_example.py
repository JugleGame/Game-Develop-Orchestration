"""공개 환경변수 예시와 실제 MCP 코드가 어긋나지 않는지 확인한다."""

import re
from pathlib import Path

from common.env import parse_env_file
from strategic.research_repo import DEFAULT_EMBEDDING_MODEL

ROOT = Path(__file__).resolve().parents[3]
MCP_ROOT = ROOT / "Src" / "McpServers"
ENV_EXAMPLE = ROOT / ".env.example"
GETENV = re.compile(r"os\.getenv\(\s*[\"'](?P<name>[A-Z][A-Z0-9_]*)[\"']")
LOADER_ONLY = {"GDAI_ENV_FILE", "GDAI_SKIP_DOTENV", "NEON_DSN"}


def example_keys() -> set[str]:
    return set(parse_env_file(ENV_EXAMPLE.read_text(encoding="utf-8")))


def source_keys() -> set[str]:
    keys: set[str] = set()
    for path in MCP_ROOT.rglob("*.py"):
        if "tests" in path.parts or path.name == "env.py":
            continue
        keys.update(match.group("name") for match in GETENV.finditer(path.read_text("utf-8")))
    return keys


def test_every_server_variable_is_documented() -> None:
    assert source_keys() - example_keys() - LOADER_ONLY == set()


def test_example_has_no_stale_variable() -> None:
    assert example_keys() - source_keys() == set()


def test_example_uses_the_research_corpus_embedding_model() -> None:
    values = parse_env_file(ENV_EXAMPLE.read_text(encoding="utf-8"))
    assert values["RESEARCH_EMBEDDING_MODEL"] == DEFAULT_EMBEDDING_MODEL == "BAAI/bge-m3"


def test_parser_handles_blank_export_and_quotes() -> None:
    parsed = parse_env_file(
        "# comment\nBLANK=\nexport DSN='postgresql://u:p@h/db'\n"
        'PATH_WITH_SPACE="C:/Program Files/Unity/Project"\n'
    )
    assert parsed == {
        "BLANK": "",
        "DSN": "postgresql://u:p@h/db",
        "PATH_WITH_SPACE": "C:/Program Files/Unity/Project",
    }
