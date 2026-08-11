"""Client for AssetGenMcpServer (§2.5).

``gameId`` and ``artStyle`` are sent only when set. §03's asset table defines
these tools as ``(featureId, prompt)``, and both extra arguments are optional
on the server, so omitting them keeps a server built strictly to the contract
working while a server that accepts them gets the identity it needs — the same
arrangement ``UnityClient.create_script`` uses for ``contents``.

Sending ``gameId`` is not cosmetic. Without it every game resolves to
``ASSET_DEFAULT_GAME_ID`` and, because Strategic AI reuses feature ids like
``f-1``, the second game generated overwrites the first one's sprites and
manifest entries on disk. That was reproduced and measured against this
client's previous call shape (``05_계약_변경_제안서`` §3.2).
"""

from typing import Any

from app.mcp.base_client import BaseToolClient


class AssetClient(BaseToolClient):
    """Wraps 2D/3D/UI asset generation, plus the per-game style lock."""

    @staticmethod
    def _payload(feature_id: str, prompt: str, game_id: str, art_style: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"featureId": feature_id, "prompt": prompt}
        if game_id:
            payload["gameId"] = game_id
        if art_style:
            payload["artStyle"] = art_style
        return payload

    async def generate_2d_sprite(
        self, *, feature_id: str, prompt: str, game_id: str = "", art_style: str = ""
    ) -> dict[str, Any]:
        return await self.call_tool(
            "generate_2d_sprite", self._payload(feature_id, prompt, game_id, art_style)
        )

    async def generate_ui_asset(
        self, *, feature_id: str, prompt: str, game_id: str = "", art_style: str = ""
    ) -> dict[str, Any]:
        return await self.call_tool(
            "generate_ui_asset", self._payload(feature_id, prompt, game_id, art_style)
        )

    async def generate_3d_placeholder(
        self, *, feature_id: str, prompt: str, game_id: str = "", art_style: str = ""
    ) -> dict[str, Any]:
        return await self.call_tool(
            "generate_3d_placeholder", self._payload(feature_id, prompt, game_id, art_style)
        )

    async def establish_art_style(self, *, game_id: str, art_style: str = "") -> dict[str, Any]:
        """Freeze ``game_id``'s palette and return it. Idempotent.

        Order matters: AssetGenMcpServer locks a game's style on the *first*
        call that touches it and ignores later changes, so this must run ahead
        of every ``generate_*`` call. Called after them it is a no-op and the
        game silently keeps ``ASSET_ART_STYLE``'s default instead of the design
        document's ``art_style`` (§05 §5.2, measured).
        """

        payload: dict[str, Any] = {"gameId": game_id}
        if art_style:
            payload["artStyle"] = art_style
        return await self.call_tool("establish_art_style", payload)

    async def asset_review_summary(self, *, game_id: str) -> dict[str, Any]:
        """Counts of pending/approved/rejected assets for ``game_id``, plus
        ``readyForBuild`` (no pending, no rejected). Used by AssetReview."""

        return await self.call_tool("asset_review_summary", {"gameId": game_id})
