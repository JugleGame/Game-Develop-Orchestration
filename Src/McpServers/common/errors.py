"""§03 error-code signalling for MCP tool servers.

The orchestrator maps a failed tool call onto the §03 numeric error codes by
reading an ``errorCode`` **integer** out of the error payload. Getting that
value across is not automatic:

* FastMCP renders any raised exception as the text
  ``"Error executing tool <name>: <message>"``, so a JSON body raised with
  ``ToolError`` is no longer valid JSON on its own.
* ``structuredContent`` is *not* populated on the error path at all.

The orchestrator therefore extracts the first embedded JSON object from that
text (``app/mcp/base_client.py::_embedded_json_object``). ``tool_error`` below
produces exactly the shape that extraction expects — use it instead of raising
bare exceptions whenever the caller should see a specific code.
"""

import json

from mcp.server.fastmcp.exceptions import ToolError

# Mirrors app/mcp/exceptions.py::ToolErrorCode. Kept as plain ints so servers
# do not need to import the orchestrator package.
VALIDATION_ERROR = 1000
TIMEOUT = 2000
MCP_ERROR = 3000
UNITY_BUILD_ERROR = 4000
UNKNOWN = 5000


def tool_error(code: int, message: str, **extra: object) -> ToolError:
    """Build a ToolError carrying a §03 error code the orchestrator can read.

    Raise the result::

        raise tool_error(VALIDATION_ERROR, "repoName must not be empty")

    ``code`` must be an int — the orchestrator looks it up in an int-keyed
    table, so the string ``"1000"`` silently degrades to MCP_ERROR (3000).
    """

    if not isinstance(code, int):  # guard the exact trap described above
        raise TypeError(f"errorCode must be an int, got {type(code).__name__}")
    payload: dict[str, object] = {"errorCode": code, "message": message}
    payload.update(extra)
    return ToolError(json.dumps(payload, ensure_ascii=False))
