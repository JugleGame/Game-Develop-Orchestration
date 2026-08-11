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

from mcp.server.mcpserver import MCPServer

from common import registry

Transport = Literal["stdio", "sse", "streamable-http"]

# 이름 -> (host, port). ``build`` 가 채우고 ``serve`` 가 읽는다.
#
# mcp 2.0.0 부터 ``MCPServer.__init__`` 이 host/port 를 받지 않는다 — 주소는
# 전송 계층의 관심사로 옮겨가 ``run_streamable_http_async(host=, port=)`` 로
# 전달된다. 서버 객체에 주소가 실려 있지 않으므로 여기서 기억해 둔다.
# (인스턴스에 속성을 몰래 붙이는 것보다 표가 낫다 — 어디서 온 값인지 보인다.)
_ADDRESSES: dict[str, tuple[str, int]] = {}

_DEFAULT_HOST = "127.0.0.1"


def register_address(name: str, port: int) -> tuple[str, int]:
    """Remember where ``name`` should listen, and return it.

    ``build`` uses this. A server that constructs ``MCPServer`` itself — because
    it needs ``lifespan``, which ``build`` does not take — must call this too,
    or ``serve`` will fall back to port 8000.
    """

    spec = next((s for s in registry.SERVERS if s.server_name == name), None)
    resolved = spec.resolved_port() if spec else int(os.getenv("MCP_PORT", str(port)))
    address = (os.getenv("MCP_HOST", _DEFAULT_HOST), resolved)
    _ADDRESSES[name] = address
    return address


def build(name: str, port: int) -> MCPServer:
    """Create an MCPServer with project-standard defaults.

    ``port`` is the fallback for a server not listed in ``common.registry``;
    a listed server takes its port from there, so the table stays the single
    source of truth and a per-server ``MCP_PORT_<KEY>`` override works.
    """

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    register_address(name, port)
    return MCPServer(name)


# 과거 이름. 타입 힌트로 쓰던 곳이 조용히 깨지지 않게 남긴다.
FastMCP = MCPServer


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


def serve(mcp: MCPServer, transport: Transport | None = None) -> None:
    """Run the server on the selected transport.

    stdio 는 주소가 없다. HTTP 계열만 host/port 를 넘긴다 — mcp 2.0.0 에서
    주소가 생성자에서 전송 계층으로 옮겨갔기 때문이다(``_ADDRESSES`` 주석 참조).
    """

    selected = transport or parse_transport()
    if selected == "stdio":
        mcp.run(transport="stdio")
        return

    host, port = _ADDRESSES.get(mcp.name, (_DEFAULT_HOST, 8000))
    logging.getLogger(mcp.name).info(
        "%s listening on http://%s:%s/mcp (%s)", mcp.name, host, port, selected
    )
    mcp.run(transport=selected, host=host, port=port)


def describe_tools(mcp: MCPServer) -> list[str]:
    """Tool names registered on ``mcp`` — used by the contract verifier."""

    return sorted(tool.name for tool in mcp._tool_manager.list_tools())


__all__ = [
    "FastMCP",
    "MCPServer",
    "Transport",
    "build",
    "describe_tools",
    "expects_dict_return",
    "inspect",
    "parse_transport",
    "serve",
]
