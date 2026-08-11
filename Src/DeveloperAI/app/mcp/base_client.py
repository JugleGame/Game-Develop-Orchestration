"""Common request/response handling for every MCP server client.

Speaks the Model Context Protocol (JSON-RPC 2.0) over the Streamable HTTP
transport using the official ``mcp`` SDK: each call opens a session, performs
the ``initialize`` handshake (protocol-version and capability negotiation),
issues ``tools/call``, and tears the session down.

Session-per-call is deliberate. The SDK's transports are anyio task-group
based and must be entered and exited from the same task; a session cached on
the client would be opened inside a per-game workflow task and closed from the
FastAPI lifespan — a different task — which raises at teardown. The handshake
costs milliseconds against tool calls that run for seconds to minutes, and it
keeps the client stateless, exactly like the HTTP client it replaces.
"""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.exceptions import MCPError as McpError
from mcp import types as mcp_types
from mcp.types import CallToolResult, TextContent

from app.mcp.exceptions import ToolCallError, ToolErrorCode
from app.utils import usage
from app.utils.logging import get_logger, log_extra
from app.utils.retry import call_with_retry

logger = get_logger(__name__)

# Transport-level faults worth another attempt. A ``McpError`` is a protocol or
# server-side failure and is never retried here — the server already saw it.
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.TransportError,
    TimeoutError,
    ConnectionError,
)

_ERROR_CODE_BY_VALUE = {code.value: code for code in ToolErrorCode}

# ClientSession raises MCPError with this code when ``read_timeout_seconds``
# elapses, rather than an asyncio/httpx timeout.
#
# mcp 2.0.0 이 값을 HTTP 408 에서 JSON-RPC 예약 대역의 -32001 로 바꿨다.
# 숫자를 다시 적어 두면 다음 변경 때 또 조용히 어긋나므로 SDK 상수를 직접 쓴다 —
# 어긋나면 타임아웃이 TIMEOUT 이 아니라 MCP_ERROR 로 분류돼 재시도 정책이 바뀐다.
_MCP_REQUEST_TIMEOUT_CODE = mcp_types.REQUEST_TIMEOUT

SessionFactory = Callable[[], AbstractAsyncContextManager[ClientSession]]


class BaseToolClient:
    """MCP client for a single tool server (§03 MCP Interface Specification)."""

    # Tools listed here mutate server-side state and are never retried: a
    # response lost *after* the server acted would otherwise be replayed
    # (e.g. a duplicate git commit). Subclasses override per server.
    _NON_IDEMPOTENT_TOOLS: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        server_name: str,
        url: str,
        timeout_seconds: float,
        max_retries: int,
        session_factory: SessionFactory | None = None,
    ) -> None:
        """``session_factory`` injects a ready session (tests use the SDK's
        in-memory transport); when omitted the client dials ``url``."""

        self._server_name = server_name
        self._url = url
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._session_factory = session_factory

    async def aclose(self) -> None:
        """No-op: sessions are scoped to a single call (see module docstring)."""

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[ClientSession]:
        """Open an initialized MCP session for the duration of one call."""

        if self._session_factory is not None:
            # 주입된 세션은 **이미 초기화되어** 들어온다. mcp 2.0.0 의
            # ``Client`` 는 ``__aenter__`` 에서 핸드셰이크를 끝내고 ``initialize()``
            # 메서드 자체가 없다 (v1 의 in-memory 헬퍼는 초기화 전 세션을 줬다).
            # 아래 직접 연결 경로만 raw ``ClientSession`` 이라 초기화가 필요하다.
            async with self._session_factory() as session:
                yield session
            return

        # mcp 2.0.0: 전송 함수가 timeout 인자를 직접 받지 않는다. 타임아웃은
        # httpx 클라이언트 쪽 관심사로 옮겨갔으므로 여기서 만들어 넘긴다.
        # (SSE 읽기 타임아웃도 같은 값을 쓴다 - v1 에서 두 인자에 같은 값을
        #  주던 것과 동작이 같다.)
        http_client = create_mcp_http_client(timeout=httpx.Timeout(self._timeout_seconds))
        async with streamable_http_client(self._url, http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=self._timeout_seconds
            ) as session:
                await session.initialize()
                yield session

    async def call_tool(self, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Call ``tool`` with ``payload`` and return its structured result.

        Raises ``ToolCallError`` on transport failure, timeout, a JSON-RPC
        error, or a tool result flagged ``isError``.
        """

        request_id = str(uuid.uuid4())
        logger.info(
            "MCP call started",
            extra=log_extra(server=self._server_name, tool=tool, request_id=request_id),
        )

        # Non-idempotent tools get exactly one attempt (see class docstring).
        max_attempts = 1 if tool in self._NON_IDEMPOTENT_TOOLS else self._max_retries

        async def _do_call() -> CallToolResult:
            try:
                async with self._connect() as session:
                    return await session.call_tool(
                        tool,
                        payload,
                        read_timeout_seconds=self._timeout_seconds,
                        meta={"requestId": request_id},
                    )
            except Exception as exc:
                # The SDK's transports run on anyio task groups, which wrap any
                # failure raised inside the session in an ExceptionGroup. Without
                # unwrapping, none of the handlers below ever match and every
                # fault escapes unmapped.
                normalized = _normalize(exc)
                if normalized is exc:
                    raise
                raise normalized from exc

        try:
            result = await call_with_retry(
                _do_call,
                operation=f"{self._server_name}:{tool}",
                max_attempts=max_attempts,
                retry_on=_RETRYABLE_EXCEPTIONS,
                logger=logger,
            )
        except (httpx.TimeoutException, TimeoutError) as exc:
            logger.error(
                "MCP call timed out",
                extra=log_extra(server=self._server_name, tool=tool, request_id=request_id),
            )
            raise ToolCallError(
                server=self._server_name,
                tool=tool,
                code=ToolErrorCode.TIMEOUT,
                message=str(exc),
                request_id=request_id,
            ) from exc
        except McpError as exc:
            # JSON-RPC level failure: unknown tool, invalid params, or a
            # session that could not be established.
            logger.error(
                "MCP protocol error",
                extra=log_extra(
                    server=self._server_name,
                    tool=tool,
                    request_id=request_id,
                    rpc_code=exc.error.code,
                ),
            )
            raise ToolCallError(
                server=self._server_name,
                tool=tool,
                code=_rpc_error_code(exc),
                message=exc.error.message,
                request_id=request_id,
            ) from exc
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
            logger.error(
                "MCP transport error",
                extra=log_extra(server=self._server_name, tool=tool, request_id=request_id),
            )
            raise ToolCallError(
                server=self._server_name,
                tool=tool,
                code=ToolErrorCode.MCP_ERROR,
                message=str(exc),
                request_id=request_id,
            ) from exc

        body = _structured_payload(result)

        if result.is_error:
            raw_error_code = body.get("errorCode")
            error_code = _ERROR_CODE_BY_VALUE.get(raw_error_code, ToolErrorCode.MCP_ERROR)
            message = body.get("message") or _text_payload(result) or "MCP tool call failed"
            logger.error(
                "MCP tool reported failure",
                extra=log_extra(
                    server=self._server_name,
                    tool=tool,
                    request_id=request_id,
                    error_code=error_code.value,
                ),
            )
            raise ToolCallError(
                server=self._server_name,
                tool=tool,
                code=error_code,
                message=message,
                request_id=request_id,
            )

        # Every tool response funnels through here, which makes this the one
        # place LLM spend can be attributed to the running job without any node
        # or client knowing about it. Servers that do no LLM work report nothing.
        usage.record(body)

        logger.info(
            "MCP call succeeded",
            extra=log_extra(server=self._server_name, tool=tool, request_id=request_id),
        )
        return body


def _leaf_exception(exc: BaseException) -> BaseException:
    """Return the first non-group exception inside a (possibly nested) group.

    anyio task groups — which every MCP transport is built on — raise an
    ``ExceptionGroup`` rather than the original error, so ``except McpError``
    silently stops matching once the failure happens inside a session.
    """

    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _normalize(exc: Exception) -> Exception:
    """Unwrap task-group noise and translate the SDK's timeout signal.

    ``ClientSession`` reports a ``read_timeout_seconds`` expiry as
    ``McpError(408)``; converting it to ``TimeoutError`` is what makes it map
    to TIMEOUT (2000) and become retryable like any other transport fault.
    """

    leaf = _leaf_exception(exc)
    if isinstance(leaf, McpError) and leaf.error.code == _MCP_REQUEST_TIMEOUT_CODE:
        return TimeoutError(leaf.error.message)
    return leaf if isinstance(leaf, Exception) else exc


def _rpc_error_code(exc: McpError) -> ToolErrorCode:
    """Map a JSON-RPC error code onto the §03 error-code table."""

    # -32602 Invalid params is the only JSON-RPC code with a direct analogue.
    if exc.error.code == -32602:
        return ToolErrorCode.VALIDATION_ERROR
    return ToolErrorCode.MCP_ERROR


def _text_payload(result: CallToolResult) -> str:
    """Concatenate the result's text content blocks."""

    return "".join(block.text for block in result.content if isinstance(block, TextContent))


def _embedded_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of ``text``, ignoring any prefix.

    FastMCP renders a failed tool as the text ``"Error executing tool <name>:
    <message>"``, so a server that raises with a JSON body produces text that
    is not itself valid JSON. Without this, the §03 ``errorCode`` channel is
    unreachable and every tool failure collapses to MCP_ERROR (3000).
    """

    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            decoded, _ = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return decoded if isinstance(decoded, dict) else None
    return None


def _structured_payload(result: CallToolResult) -> dict[str, Any]:
    """Return the tool result as a dict.

    Prefers ``structuredContent`` (the typed channel, populated when a server
    tool declares a serializable return type such as ``dict[str, Any]``); falls
    back to parsing the text content blocks as JSON, then to extracting a JSON
    object embedded in a longer message.
    """

    if result.structured_content is not None:
        return result.structured_content

    text = _text_payload(result)
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except ValueError:
        embedded = _embedded_json_object(text)
        return embedded if embedded is not None else {"text": text}
    return decoded if isinstance(decoded, dict) else {"result": decoded}
