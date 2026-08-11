"""The registry is only a single source of truth if nothing else disagrees.

``app/config/settings.py`` lives in the DeveloperAI distribution and cannot
import ``common.registry`` — the two packages are never on the path together
outside this test. So the link between them is asserted here instead: this test
reads that file and fails when a port drifts.

Reading the source rather than importing it keeps the test free of any
DeveloperAI dependency (pydantic-settings, sqlalchemy, langgraph), which is
what lets the McpServers suite run on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from common import registry

SETTINGS_PATH = (
    Path(__file__).resolve().parents[2] / "DeveloperAI" / "app" / "config" / "settings.py"
)

# strategic_mcp_url: str = "http://localhost:9101/mcp"
_URL_SETTING = re.compile(
    r"^\s*(?P<key>\w+)_mcp_url:\s*str\s*=\s*\"http://[^:]+:(?P<port>\d+)/mcp\"",
    re.MULTILINE,
)


def _settings_ports() -> dict[str, int]:
    source = SETTINGS_PATH.read_text(encoding="utf-8")
    return {m.group("key"): int(m.group("port")) for m in _URL_SETTING.finditer(source)}


def test_settings_file_is_where_we_think_it_is() -> None:
    """A moved file would make every assertion below vacuously pass."""

    assert SETTINGS_PATH.is_file(), f"expected orchestrator settings at {SETTINGS_PATH}"


def test_registry_covers_every_orchestrator_url() -> None:
    assert set(_settings_ports()) == set(registry.BY_KEY), (
        "the orchestrator dials a server the registry does not list, or vice versa"
    )


@pytest.mark.parametrize("spec", registry.SERVERS, ids=lambda s: s.key)
def test_port_matches_the_orchestrator_default(spec: registry.ServerSpec) -> None:
    ports = _settings_ports()
    assert ports.get(spec.key) == spec.port, (
        f"{spec.key}: registry says {spec.port}, "
        f"app/config/settings.py says {ports.get(spec.key)}. "
        f"A mismatch surfaces as a connection refused mid-pipeline."
    )


def test_ports_are_unique() -> None:
    ports = [spec.port for spec in registry.SERVERS]
    assert len(set(ports)) == len(ports), "two servers would bind the same port"


def test_per_server_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old shared ``MCP_PORT`` moved all five servers at once."""

    spec = registry.get("qa")
    monkeypatch.setenv(spec.port_env_var, "19103")
    assert spec.resolved_port() == 19103
    assert registry.get("git").resolved_port() == registry.get("git").port


def test_unknown_server_names_the_valid_set() -> None:
    with pytest.raises(KeyError, match="unknown server"):
        registry.get("nope")
