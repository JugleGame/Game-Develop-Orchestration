"""StructureCompare node: establishes the QA policy and diffs prototype structure."""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.nodes.qa_common import build_reference
from app.graph.state import GraphState, Stage
from app.mcp.qa_client import QaClient
from app.models.schemas import GameDesignDocument, JobStatus

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def build_structure_compare_node(client: QaClient) -> NodeFn:
    async def structure_compare_node(state: GraphState) -> dict:
        design = GameDesignDocument.model_validate(state["game_design"])
        build = build_reference(state)

        policy = await client.establish_qa_policy(game_design=design)
        structure_check = await client.verify_prototype_structure(game_design=design, build=build)

        return {
            "current_stage": Stage.STRUCTURE_COMPARE,
            "status": JobStatus.QA_REVIEW.value,
            "qa_policy": policy.model_dump(mode="json"),
            "structure_check": structure_check,
        }

    return structure_compare_node
