"""AssetGen node: generates placeholder assets per feature and imports them
into the Unity project.

Generation alone leaves the asset orphaned on AssetGenMcpServer's side; the
follow-up ``import_asset`` call (§2.4) is what makes it part of the project
before BuildPrototype runs.

Two identities travel with every call and are what keep separate games apart:

* ``gameId`` — without it the server falls back to one shared project, so the
  second game generated overwrites the first one's sprites and manifest.
* ``artStyle`` — the design document's look. It is locked once, up front, via
  ``establish_art_style``; the server freezes a game's palette on first touch,
  so locking it after the first ``generate_*`` call does nothing.

## What gets asked for

When a feature carries ``assets_needed`` (the spec's ``unityHints.assetsNeeded``,
§4.4 of ``05_계약_변경_제안서``), each named item becomes its own asset request
with that item as the prompt. Otherwise the whole ``description`` is sent as one
2D sprite request, which is what this node always did.

The distinction matters more than it looks. ``description`` is the rendered spec
— goal, scope, acceptance criteria, hints — and the asset server classifies by
scanning the prompt for the *first* keyword it recognises, checking UI terms
before world terms. So a platformer spec that mentions a "재시작 버튼" anywhere
produced a button sprite and nothing else. Naming the assets fixes that at the
source; routing them to the right tool is the rest of this file.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.asset_client import AssetClient
from app.mcp.unity_client import UnityClient
from app.models.schemas import JobStatus

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]

# 3D wording. The pipeline targets 2D games (Doc/설계/07), so these are rare —
# but when a plan does ask for one, ``generate_3d_placeholder`` degrades it to a
# flat prop silhouette on purpose, whereas the 2D tool would guess some unrelated
# kind from the leftover words.
_3D_MARKERS = ("3d", "3-d", "mesh", "메시", "메쉬", "입체")

# UI wording. Deliberately coarser than ``asset/render.py::_KIND_KEYWORDS`` — this
# only answers "is this UI?", and the server still picks button vs panel vs icon.
# A disagreement between the two lists degrades rather than breaks: the UI tool
# forces ``ui_panel`` for wording it does not recognise, and the 2D tool's own
# classifier routes UI wording to a UI kind anyway.
_UI_MARKERS = (
    "ui",
    "hud",
    "button",
    "panel",
    "menu",
    "dialog",
    "inventory",
    "icon",
    "cursor",
    "badge",
    "버튼",
    "패널",
    "메뉴",
    "아이콘",
    "인벤토리",
    "커서",
    "대화창",
)


def select_asset_tool(description: str) -> str:
    """Which ``generate_*`` tool a named asset belongs to.

    Keyword matching, not an LLM: this runs on every asset and must give the
    same answer every run, or two builds of the same plan differ for no reason
    the logs explain. 3D is checked first because "3D 인벤토리 아이콘" is a 3D
    request that happens to mention UI.
    """

    lowered = description.lower()
    if any(marker in lowered for marker in _3D_MARKERS):
        return "generate_3d_placeholder"
    if any(marker in lowered for marker in _UI_MARKERS):
        return "generate_ui_asset"
    return "generate_2d_sprite"


def build_assetgen_node(asset_client: AssetClient, unity_client: UnityClient) -> NodeFn:
    async def assetgen_node(state: GraphState) -> dict:
        game_id = state.get("game_id") or ""
        design = state.get("game_design") or {}
        art_style = design.get("art_style") or ""

        # Ahead of every generate_* call, never after — see the module
        # docstring. Skipped only when there is no game to lock (hand-built
        # states in tests), where the server's fallback is the same value.
        if game_id:
            await asset_client.establish_art_style(game_id=game_id, art_style=art_style)

        generators = {
            "generate_2d_sprite": asset_client.generate_2d_sprite,
            "generate_ui_asset": asset_client.generate_ui_asset,
            "generate_3d_placeholder": asset_client.generate_3d_placeholder,
        }

        generated_assets = []
        for feature in state.get("feature_prompts", []):
            feature_id = feature["feature_id"]
            for prompt, tool in _requests_for(feature):
                asset = await generators[tool](
                    feature_id=feature_id,
                    prompt=prompt,
                    game_id=game_id,
                    art_style=art_style,
                )
                asset_path = asset.get("assetPath")
                if asset_path:
                    await unity_client.import_asset(feature_id=feature_id, asset_path=asset_path)
                generated_assets.append(asset)

        return {
            "current_stage": Stage.ASSET_GEN,
            "status": JobStatus.DEVELOPING.value,
            "generated_assets": generated_assets,
        }

    return assetgen_node


def _requests_for(feature: dict[str, Any]) -> list[tuple[str, str]]:
    """``(prompt, tool)`` pairs for one feature.

    Blank entries are dropped rather than sent: an empty prompt makes the server
    classify against nothing and emit a default silhouette that no one asked
    for. If that leaves nothing, fall back to the description — a feature with a
    malformed hint list should still get its sprite.
    """

    named = [item.strip() for item in feature.get("assets_needed") or [] if item and item.strip()]
    if named:
        return [(item, select_asset_tool(item)) for item in named]
    return [(feature["description"], "generate_2d_sprite")]
