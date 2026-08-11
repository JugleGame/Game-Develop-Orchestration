"""Planning node: asks StrategicMcpServer for the game design + feature list."""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.strategic_client import StrategicClient
from app.models.schemas import JobStatus
from app.utils.naming import build_repo_name

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def build_planning_node(client: StrategicClient) -> NodeFn:
    async def planning_node(state: GraphState) -> dict:
        # ConceptGate may have replaced the raw intake prompt with an idea the
        # user reviewed and edited; that is what was approved, so that is what
        # gets planned. Falls back to the intake prompt when the gate is off.
        prompt = state.get("approved_idea") or state["prompt"]
        feedback = state.get("planning_feedback")
        if feedback:
            # A rejected design comes back here with the user's objection; a
            # re-plan must address it, not re-roll the same request.
            prompt += (
                "\n\n[User feedback on the rejected design — must be addressed]\n" + feedback
            )

        design, feature_prompts = await client.generate_game_design(prompt=prompt)

        # repo_name is computed once per game and then carried forward across
        # re-runs (e.g. a "revise existing game" request) so Deployment keeps
        # pushing to the same Git repository instead of naming a new one.
        repo_name = state.get("repo_name") or build_repo_name(
            genre=design.genre, prompt=state["prompt"], game_id=state["game_id"]
        )

        return {
            "current_stage": Stage.PLANNING,
            "status": JobStatus.AWAITING_APPROVAL.value,
            "game_design": design.model_dump(mode="json"),
            "feature_prompts": [fp.model_dump(mode="json") for fp in feature_prompts],
            "repo_name": repo_name,
            # Consume the feedback so it never leaks into an unrelated re-plan.
            "planning_feedback": None,
        }

    return planning_node
