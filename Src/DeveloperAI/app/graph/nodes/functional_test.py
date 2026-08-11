"""FunctionalTest node: runs the QA policy against the build and records the verdict."""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.nodes.qa_common import build_reference
from app.graph.state import GraphState, Stage
from app.mcp.qa_client import QaClient
from app.models.schemas import GameDesignDocument, JobStatus, QAPolicy

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def build_functional_test_node(client: QaClient) -> NodeFn:
    async def functional_test_node(state: GraphState) -> dict:
        design = GameDesignDocument.model_validate(state["game_design"])
        policy = QAPolicy.model_validate(state["qa_policy"])
        build = build_reference(state)

        passed, error_report = await client.run_functional_verification(
            game_design=design, build=build, qa_policy=policy
        )

        update: dict[str, Any] = {
            "current_stage": Stage.FUNCTIONAL_TEST,
            "qa_passed": passed,
        }
        if passed:
            update["status"] = JobStatus.QA_REVIEW.value
        else:
            update["status"] = JobStatus.DEVELOPING.value
            update["qa_iteration_count"] = state.get("qa_iteration_count", 0) + 1
            update["last_error"] = error_report.model_dump(mode="json") if error_report else None
        return update

    return functional_test_node
