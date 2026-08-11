"""The five tool servers, declared once.

This table used to exist in four places — ``app/config/settings.py``,
``scripts/dev_server.py``, ``verify_contract.py``, and the ``build(name, port)``
call at the top of each server module. Four copies is four chances for a port
to drift, and the symptom of drift is a connection refused several minutes into
a pipeline run.

``settings.py`` lives in a different distribution and cannot import this module,
so it keeps its own defaults; ``tests/test_server_registry.py`` on this side
reads that file and fails when the two disagree.
"""

from __future__ import annotations

import os
from typing import NamedTuple


class ServerSpec(NamedTuple):
    """One tool server: how to launch it, and where it listens."""

    key: str
    """Short name used on the command line and in ``.mcp.json``."""

    module: str
    """Import path, run as ``python -m <module>``."""

    server_name: str
    """The name the server registers with FastMCP, as §03 spells it."""

    port: int
    """Default loopback port for the Streamable HTTP transport."""

    @property
    def port_env_var(self) -> str:
        """Per-server port override.

        A single shared ``MCP_PORT`` cannot express five ports: exporting it
        once made all five servers try to bind the same one.
        """

        return f"MCP_PORT_{self.key.upper()}"

    def resolved_port(self) -> int:
        """``port``, unless overridden per server or by the legacy ``MCP_PORT``."""

        override = os.getenv(self.port_env_var) or os.getenv("MCP_PORT")
        return int(override) if override else self.port

    def url(self) -> str:
        """The server's single JSON-RPC endpoint (§03) — not a per-tool route."""

        host = os.getenv("MCP_HOST", "127.0.0.1")
        return f"http://{host}:{self.resolved_port()}/mcp"


SERVERS: tuple[ServerSpec, ...] = (
    ServerSpec("strategic", "strategic.server", "StrategicMcpServer", 9101),
    ServerSpec("unity", "unity.server", "UnityMcpServer", 9102),
    ServerSpec("qa", "qa.server", "QaMcpServer", 9103),
    ServerSpec("asset", "asset.server", "AssetGenMcpServer", 9104),
    ServerSpec("git", "gitmcp.server", "GitMcpServer", 9105),
)

BY_KEY: dict[str, ServerSpec] = {spec.key: spec for spec in SERVERS}


def get(key: str) -> ServerSpec:
    """Look up a server by its short name, failing with the valid set listed."""

    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown server {key!r}; expected one of {sorted(BY_KEY)}") from None


__all__ = ["BY_KEY", "SERVERS", "ServerSpec", "get"]
