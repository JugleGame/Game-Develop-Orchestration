"""AssetReview node: blocks Deployment only on an explicitly rejected asset.

Assets default to ``pending`` on generation and nothing marks them approved
automatically (AssetGenMcpServer's module docstring, Doc/설계/05_계약_변경_제안서.md
§6, `Src/McpServers/README.md`). Gating on ``pending == 0`` would therefore
stall every single job before a human ever gets a chance to look, so this
checks the rejection count directly rather than the stricter
``asset_review_summary.readyForBuild`` (which also requires no pending
assets) — pending assets ship as-is, only a ``review_asset(approved=False)``
call halts the pipeline.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.asset_client import AssetClient
from app.models.schemas import JobStatus

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def build_asset_review_node(client: AssetClient) -> NodeFn:
    async def asset_review_node(state: GraphState) -> dict:
        summary = await client.asset_review_summary(game_id=state["game_id"])
        return {
            "current_stage": Stage.ASSET_REVIEW,
            "status": JobStatus.QA_REVIEW.value,
            "asset_review": summary,
        }

    return asset_review_node


def asset_review_blocked(state: GraphState) -> bool:
    """Predicate used by the conditional edge after AssetReview."""

    summary = state.get("asset_review") or {}
    return int(summary.get("rejected", 0)) > 0
