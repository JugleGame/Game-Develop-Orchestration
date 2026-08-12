"""세 MCP 서버의 공통 stdio 부트스트랩."""

import inspect
import logging
import os
from collections.abc import Callable
from typing import Any, get_type_hints

from mcp.server.mcpserver import MCPServer


def build(name: str) -> MCPServer:
    """프로젝트 공통 로깅을 적용한 stdio MCP 서버를 만든다."""

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return MCPServer(name)


# 이전 모듈이 가져가던 이름을 가볍게 유지한다.
FastMCP = MCPServer


def expects_dict_return(fn: Callable[..., Any]) -> Callable[..., Any]:
    """도구의 구조화 출력 채널을 보장한다."""

    annotation = get_type_hints(fn).get("return")
    if annotation is dict or annotation is None:
        raise TypeError(
            f"{fn.__name__}: 반환형은 dict[str, Any]로 선언해야 합니다."
        )
    return fn


def serve(mcp: MCPServer) -> None:
    """호스트 에이전트가 직접 연결하는 stdio 전송으로 실행한다."""

    mcp.run(transport="stdio")


def describe_tools(mcp: MCPServer) -> list[str]:
    """계약 검증에 사용하는 등록 도구 이름을 반환한다."""

    return sorted(tool.name for tool in mcp._tool_manager.list_tools())


__all__ = [
    "FastMCP",
    "MCPServer",
    "build",
    "describe_tools",
    "expects_dict_return",
    "inspect",
    "serve",
]
