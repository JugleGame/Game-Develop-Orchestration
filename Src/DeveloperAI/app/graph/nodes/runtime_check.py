"""RuntimeCheck node: runs a Unity playmode pass so QA judges against real
runtime evidence instead of compile metadata alone (Doc/설계/06_3-4군_인수인계.md §3)."""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.unity_client import UnityClient
from app.models.schemas import JobStatus

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def build_runtime_check_node(client: UnityClient) -> NodeFn:
    async def runtime_check_node(state: GraphState) -> dict:
        runtime_check = await client.run_playmode_test(game_id=state["game_id"])
        return {
            "current_stage": Stage.RUNTIME_CHECK,
            "status": JobStatus.DEVELOPING.value,
            "runtime_check": runtime_check,
        }

    return runtime_check_node
