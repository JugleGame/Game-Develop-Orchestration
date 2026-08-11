"""ApprovalGate node: pauses the graph until the user approves or rejects.

Resumed via ``Command(resume={"approved": bool, "feedback": str | None})``
from ``GameService.approve`` (POST /games/{id}/approve).
"""

from langgraph.types import interrupt

from app.graph.state import GraphState, Stage
from app.models.schemas import JobStatus


async def approval_gate_node(state: GraphState) -> dict:
    decision = interrupt(
        {
            "stage": Stage.APPROVAL_GATE,
            "game_design": state.get("game_design"),
            "feature_prompts": state.get("feature_prompts"),
        }
    )
    approved = bool(decision.get("approved", False))
    if approved:
        return {
            "current_stage": Stage.APPROVAL_GATE,
            "status": JobStatus.DEVELOPING.value,
            "approved": True,
            "planning_feedback": None,
        }
    # Rejection: hand the user's objection to Planning and count the round so
    # the replan loop is bounded like the dev/QA loops (§3.3).
    return {
        "current_stage": Stage.PLANNING,
        "status": JobStatus.PLANNING.value,
        "approved": False,
        "planning_feedback": decision.get("feedback"),
        "replan_count": state.get("replan_count", 0) + 1,
    }
