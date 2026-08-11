"""Client for StrategicMcpServer (§2.2)."""

from typing import Any

from app.mcp.base_client import BaseToolClient
from app.models.schemas import FeatureImplementationPrompt, GameDesignDocument


class StrategicClient(BaseToolClient):
    """Wraps the planning tools exposed by StrategicMcpServer."""

    # ``decide_concept`` advances a stored proposal's state machine and appends
    # to its decision log, so a replayed call would either double-log the
    # decision or fail against the already-advanced status. ``propose_concept``
    # is likewise versioned per call.
    _NON_IDEMPOTENT_TOOLS = frozenset({"propose_concept", "decide_concept"})

    async def generate_game_design(
        self, *, prompt: str
    ) -> tuple[GameDesignDocument, list[FeatureImplementationPrompt]]:
        body = await self.call_tool("generate_game_design", {"prompt": prompt})
        design = GameDesignDocument.model_validate(body["gameDesign"])
        feature_prompts = [
            FeatureImplementationPrompt.model_validate(item)
            for item in body.get("featurePrompts", [])
        ]
        return design, feature_prompts

    async def propose_concept(self, *, game_id: str, idea: str) -> dict[str, Any]:
        """Gather evidence for ``idea`` and park it for human review (§3.1).

        No LLM call happens here — this is the cheap step that exists so the
        expensive one (``generate_game_design``) is never spent on a direction
        the user did not want.
        """

        return await self.call_tool("propose_concept", {"gameId": game_id, "idea": idea})

    async def decide_concept(
        self,
        *,
        game_id: str,
        decision: str,
        note: str = "",
        edited_idea: str = "",
    ) -> dict[str, Any]:
        """Record the human decision on a pending proposal (§3.1).

        ``decision`` is ``approve`` | ``revise`` | ``reject``. Optional fields
        are sent only when set: the server is written to the §03 signature and
        an empty ``editedIdea`` means "keep the current idea", which is not the
        same as omitting it.
        """

        payload: dict[str, Any] = {"gameId": game_id, "decision": decision}
        if note:
            payload["note"] = note
        if edited_idea:
            payload["editedIdea"] = edited_idea
        return await self.call_tool("decide_concept", payload)
