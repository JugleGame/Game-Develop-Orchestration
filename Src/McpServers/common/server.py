"""Shared bootstrap for every MCP tool server in this project.

Two hard-won rules are encoded here so each server does not rediscover them:

1. **Return type must be ``dict[str, Any]``, never a bare ``dict``.**
   A bare ``dict`` annotation produces *no* ``outputSchema`` and leaves
   ``structuredContent`` empty — results then only survive via the
   orchestrator's JSON-text fallback. ``dict[str, Any]`` populates the typed
   channel properly. (``@expects_dict_return`` below asserts this at import.)

2. **Bind to loopback, not 0.0.0.0.** Only the orchestrator's own API is meant
   to be externally reachable (§7.3); tool servers stay on the internal
   network.
"""

import argparse
import inspect
import logging
import os
from collections.abc import Callable
from typing import Any, Literal, get_type_hints

from mcp.server.fastmcp import FastMCP

from common import registry

Transport = Literal["stdio", "sse", "streamable-http"]


def build(name: str, port: int) -> FastMCP:
    """Create a FastMCP server with project-standard defaults.

    ``port`` is the fallback for a server not listed in ``common.registry``;
    a listed server takes its port from there, so the table stays the single
    source of truth and a per-server ``MCP_PORT_<KEY>`` override works.
    """

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    spec = next((s for s in registry.SERVERS if s.server_name == name), None)
    resolved = spec.resolved_port() if spec else int(os.getenv("MCP_PORT", str(port)))
    return FastMCP(
        name,
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=resolved,
    )


def expects_dict_return(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Fail fast if a tool does not annotate ``-> dict[str, Any]``.

    Catches rule 1 above at import time rather than as a silently empty
    ``structuredContent`` at runtime.
    """

    hints = get_type_hints(fn)
    annotation = hints.get("return")
    if annotation is dict or annotation is None:
        raise TypeError(
            f"{fn.__name__}: annotate the return as dict[str, Any]; a bare "
            f"'dict' (or no annotation) yields an empty structuredContent."
        )
    return fn


def parse_transport(default: Transport = "streamable-http") -> Transport:
    """Read the run mode from ``--transport`` or ``MCP_TRANSPORT``.

    The orchestrator always dials Streamable HTTP; ``stdio`` is for local
    inspection (e.g. the MCP Inspector or an editor integration) and ``sse``
    for clients that only speak the legacy stream transport.
    """

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.getenv("MCP_TRANSPORT", default),
        help="MCP transport to serve on (default: %(default)s)",
    )
    args, _unknown = parser.parse_known_args()
    return args.transport  # type: ignore[return-value]


def serve(mcp: FastMCP, transport: Transport | None = None) -> None:
    """Run the server on the selected transport."""

    selected = transport or parse_transport()
    if selected != "stdio":
        logging.getLogger(mcp.name).info(
            "%s listening on http://%s:%s/mcp (%s)",
            mcp.name,
            mcp.settings.host,
            mcp.settings.port,
            selected,
        )
    mcp.run(transport=selected)


def describe_tools(mcp: FastMCP) -> list[str]:
    """Tool names registered on ``mcp`` — used by the contract verifier."""

    return sorted(tool.name for tool in mcp._tool_manager.list_tools())


__all__ = [
    "FastMCP",
    "Transport",
    "build",
    "describe_tools",
    "expects_dict_return",
    "inspect",
    "parse_transport",
    "serve",
]
