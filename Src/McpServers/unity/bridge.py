"""Unity 공식 MCP 서버로 가는 클라이언트 브리지.

Unity 6 의 ``com.unity.ai.assistant`` 는 자체 MCP 서버를 제공하지만,
오케스트레이터가 쓰는 Streamable HTTP 가 아니라 **stdio 로 띄우는 릴레이
프로세스 + 명명 파이프**로 Editor 에 붙는다::

    orchestrator --HTTP--> [이 어댑터] --stdio--> relay_win.exe --pipe--> Unity Editor

릴레이는 100MB 짜리 바이너리이고 기동에 수 초가 걸리므로, 호출마다 새로
띄우면 감당이 안 된다. 그렇다고 세션을 그냥 필드에 캐시할 수도 없다 —
MCP SDK 의 전송 계층은 anyio 태스크 그룹 기반이라 **세션을 연 태스크와 닫는
태스크가 같아야** 하는데, lifespan 에서 열고 요청 핸들러에서 쓰면 그 보장이
깨진다.

그래서 세션을 **전담 소유자 태스크** 하나가 열고, 닫고, 그 안에서만 사용한다.
다른 코루틴은 큐로 요청을 넣고 Future 로 결과를 받는다. 진입·이탈이 모두
``_serve`` 한 곳에서 일어나므로 태스크 친화성 문제가 원천적으로 없다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

logger = logging.getLogger(__name__)

_SHUTDOWN = object()


def default_relay_path() -> Path:
    """릴레이 바이너리 기본 위치. Unity 가 기동 시 여기에 설치한다."""

    override = os.getenv("UNITY_RELAY_PATH")
    if override:
        return Path(override)
    name = "relay_win.exe" if os.name == "nt" else "relay"
    return Path.home() / ".unity" / "relay" / name


def _same_path(left: str | Path, right: str | Path) -> bool:
    """Compare local paths without requiring that either target still exists."""

    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def discover_editor_instance_id(project_path: str, registry_dir: Path | None = None) -> str | None:
    """Find the active Unity Editor PID recorded by its MCP bridge.

    Unity 6 writes one JSON discovery record per running editor under the current user's
    profile. Supplying that PID prevents Relay from selecting its legacy default instance.
    Invalid, stale, and unrelated records are ignored.
    """

    override = os.getenv("UNITY_EDITOR_PID", "").strip()
    if override:
        if not override.isdecimal() or int(override) <= 0:
            raise UnityBridgeError("UNITY_EDITOR_PID must be a positive integer.")
        return override

    directory = registry_dir or Path.home() / ".unity" / "mcp" / "connections"
    if not directory.is_dir():
        return None

    candidates: list[tuple[float, int]] = []
    for record in directory.glob("bridge-*.json"):
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
            pid = payload.get("editor_pid")
            recorded_project = payload.get("project_path")
            pipe = payload.get("connection_path")
            if (
                not isinstance(pid, int)
                or pid <= 0
                or not isinstance(recorded_project, str)
                or not isinstance(pipe, str)
                or not _same_path(recorded_project, project_path)
                or not pipe.startswith(r"\\.\pipe\unity-mcp-")
                or not pipe.endswith(f"-{pid}")
            ):
                continue
        except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
        candidates.append((record.stat().st_mtime, pid))

    if not candidates:
        return None
    return str(max(candidates)[1])


class UnityBridgeError(RuntimeError):
    """브리지 자체의 실패 (릴레이 없음, Editor 미접속 등)."""


@dataclass
class _Request:
    tool: str
    arguments: dict[str, Any]
    future: asyncio.Future[CallToolResult]
    timeout: float


@dataclass
class UnityBridge:
    """Unity MCP 세션을 소유하는 단일 태스크.

    ``start()`` 로 켜고 ``call()`` 로 도구를 부르고 ``stop()`` 으로 끈다.
    """

    relay_path: Path = field(default_factory=default_relay_path)
    project_path: str = ""
    startup_timeout: float = 60.0
    editor_instance_id: str | None = None

    _queue: asyncio.Queue[_Request | object] = field(default_factory=asyncio.Queue, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _ready: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _startup_error: BaseException | None = field(default=None, init=False)
    _tools: dict[str, Any] = field(default_factory=dict, init=False)

    # ------------------------------------------------------------------
    # 수명주기
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if not self.relay_path.exists():
            raise UnityBridgeError(
                f"Unity 릴레이 바이너리를 찾을 수 없습니다: {self.relay_path}. "
                "Unity Editor 를 한 번 실행하면 ~/.unity/relay/ 에 설치됩니다."
            )
        if not self.project_path:
            raise UnityBridgeError("project_path 가 비어 있습니다 (UNITY_PROJECT_PATH).")

        self._task = asyncio.create_task(self._serve(), name="unity-bridge")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.startup_timeout)
        except TimeoutError as exc:
            self._task.cancel()
            raise UnityBridgeError(
                f"Unity MCP 릴레이가 {self.startup_timeout}초 안에 준비되지 않았습니다. "
                "Unity Editor 가 실행 중이고 프로젝트 경로가 맞는지 확인하세요."
            ) from exc

        if self._startup_error is not None:
            raise UnityBridgeError(
                f"Unity MCP 연결 실패: {self._startup_error}"
            ) from self._startup_error

        logger.info("Unity bridge ready — %d tools discovered", len(self._tools))

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(_SHUTDOWN)
        try:
            await asyncio.wait_for(self._task, timeout=15)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    # ------------------------------------------------------------------
    # 소유자 태스크 — 세션 진입/이탈이 전부 여기서만 일어난다
    # ------------------------------------------------------------------
    async def _serve(self) -> None:
        instance_id = self.editor_instance_id or discover_editor_instance_id(self.project_path)
        args = ["--mcp", "--project-path", self.project_path]
        if instance_id is not None:
            args.extend(["--instance-id", instance_id])
            logger.info("Targeting Unity Editor PID %s from MCP discovery.", instance_id)
        else:
            logger.warning(
                "No Unity MCP discovery record found for %s; Relay will select an instance.",
                self.project_path,
            )
        params = StdioServerParameters(
            command=str(self.relay_path),
            args=args,
        )
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._tools = {tool.name: tool for tool in listed.tools}
                    self._ready.set()
                    await self._pump(session)
        except BaseException as exc:  # noqa: BLE001 — 기동 실패를 start() 로 전달
            self._startup_error = exc
            self._ready.set()
            self._drain_pending(exc)
            if not isinstance(exc, asyncio.CancelledError):
                logger.exception("Unity bridge terminated")

    async def _pump(self, session: ClientSession) -> None:
        while True:
            item = await self._queue.get()
            if item is _SHUTDOWN:
                return
            assert isinstance(item, _Request)
            if item.future.done():  # 호출자가 이미 타임아웃으로 포기함
                continue
            try:
                result = await asyncio.wait_for(
                    session.call_tool(item.tool, item.arguments), timeout=item.timeout
                )
            except BaseException as exc:  # noqa: BLE001 — 호출자에게 그대로 전달
                if not item.future.done():
                    item.future.set_exception(exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
            else:
                if not item.future.done():
                    item.future.set_result(result)

    def _drain_pending(self, exc: BaseException) -> None:
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, _Request) and not item.future.done():
                item.future.set_exception(exc)

    # ------------------------------------------------------------------
    # 호출
    # ------------------------------------------------------------------
    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    async def call(
        self, tool: str, arguments: dict[str, Any], timeout: float = 120.0
    ) -> CallToolResult:
        """Unity 도구를 호출한다. 소유자 태스크가 죽어 있으면 즉시 실패."""

        if self._task is None or self._task.done():
            raise UnityBridgeError("Unity 브리지가 실행 중이 아닙니다.")
        if not self.has_tool(tool):
            raise UnityBridgeError(
                f"Unity 에 '{tool}' 도구가 없습니다. "
                f"사용 가능: {', '.join(self.tool_names[:8])} …"
            )

        future: asyncio.Future[CallToolResult] = asyncio.get_running_loop().create_future()
        await self._queue.put(
            _Request(tool=tool, arguments=arguments, future=future, timeout=timeout)
        )
        # 큐 대기까지 감안해 여유를 둔다.
        return await asyncio.wait_for(future, timeout=timeout + 30)
