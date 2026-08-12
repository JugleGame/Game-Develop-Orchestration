"""MCP 계약의 오류 코드 전달.

The host maps a failed tool call onto the numeric error codes by
reading an ``errorCode`` **integer** out of the error payload. Getting that
value across is not automatic:

* FastMCP renders any raised exception as the text
  ``"Error executing tool <name>: <message>"``, so a JSON body raised with
  ``ToolError`` is no longer valid JSON on its own.
* ``structuredContent`` is *not* populated on the error path at all.

The host can extract the first embedded JSON object from that text. ``tool_error``
produces a stable shape instead of relying on exception prose.
"""

import json

from mcp.server.mcpserver.exceptions import ToolError

# ``docs/contracts.md``의 공통 계약. 클라이언트 종류와 무관하도록 정수로 유지한다.
VALIDATION_ERROR = 1000
TIMEOUT = 2000
MCP_ERROR = 3000
UNITY_BUILD_ERROR = 4000
UNKNOWN = 5000


def tool_error(code: int, message: str, **extra: object) -> ToolError:
    """Build a ToolError carrying a contract error code the host can read.

    Raise the result::

        raise tool_error(VALIDATION_ERROR, "repoName must not be empty")

    ``code`` must be an int so every host observes the same payload type.
    """

    if not isinstance(code, int):  # guard the exact trap described above
        raise TypeError(f"errorCode must be an int, got {type(code).__name__}")
    payload: dict[str, object] = {"errorCode": code, "message": message}
    payload.update(extra)
    return ToolError(json.dumps(payload, ensure_ascii=False))
