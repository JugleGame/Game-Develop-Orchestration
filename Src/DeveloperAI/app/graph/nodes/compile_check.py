"""CompileCheck node: fetches compiler diagnostics for the last build."""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.unity_client import UnityClient
from app.models.schemas import JobStatus

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def build_compile_check_node(client: UnityClient) -> NodeFn:
    async def compile_check_node(state: GraphState) -> dict:
        errors = await client.get_compile_errors(game_id=state["game_id"])
        return {
            "current_stage": Stage.COMPILE_CHECK,
            "status": JobStatus.DEVELOPING.value,
            "compile_errors": [error.model_dump(mode="json") for error in errors],
        }

    return compile_check_node


def compile_succeeded(state: GraphState) -> bool:
    """Predicate used by the conditional edge after CompileCheck."""

    return len(state.get("compile_errors", [])) == 0
