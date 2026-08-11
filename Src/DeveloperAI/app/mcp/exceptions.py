"""Error codes and exception type shared by every tool-server client (§03 Error Code)."""

from enum import IntEnum


class ToolErrorCode(IntEnum):
    VALIDATION_ERROR = 1000
    TIMEOUT = 2000
    MCP_ERROR = 3000
    UNITY_BUILD_ERROR = 4000
    UNKNOWN = 5000


class ToolCallError(Exception):
    """Raised whenever an MCP server call fails, times out, or is rejected."""

    def __init__(
        self,
        *,
        server: str,
        tool: str,
        code: ToolErrorCode,
        message: str,
        request_id: str | None = None,
    ) -> None:
        self.server = server
        self.tool = tool
        self.code = code
        self.request_id = request_id
        super().__init__(f"[{server}:{tool}] ({code.name}) {message}")
