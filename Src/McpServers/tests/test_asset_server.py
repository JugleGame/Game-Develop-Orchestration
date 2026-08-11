"""Tests for AssetGenMcpServer.

Driven through a real MCP session (SDK in-memory transport), so the §03
contract — tool names, argument casing, structuredContent, error codes — is
exercised exactly as the orchestrator will exercise it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from PIL import Image

from asset import pixellab_client
from asset.render import classify
from asset.server import mcp
from asset.style import derive


@pytest.fixture(autouse=True)
def _pixellab_stub(monkeypatch):
    """No procedural fallback exists anymore — every generation in this file
    goes through PixelLab, so stub the HTTP call with a deterministic fake
    (same seed -> same bytes) instead of hitting the real API."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_generate(*, prompt, width, height, seed, **kwargs):
        colour = (seed & 0xFF, (seed >> 8) & 0xFF, (seed >> 16) & 0xFF, 255)
        return Image.new("RGBA", (width, height), colour), {"type": "usd", "usd": 0.001}

    monkeypatch.setattr(pixellab_client, "generate_image", _fake_generate)


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        yield client


# --------------------------------------------------------------------------
# §03 contract
# --------------------------------------------------------------------------


async def test_exposes_every_contract_tool():
    async with session() as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert {"generate_2d_sprite", "generate_ui_asset", "generate_3d_placeholder"} <= names


async def test_contract_tools_use_camel_case_argument_names():
    """The orchestrator sends featureId/prompt; snake_case would 400."""

    async with session() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in ("generate_2d_sprite", "generate_ui_asset", "generate_3d_placeholder"):
        properties = set(tools[name].inputSchema["properties"])
        assert {"featureId", "prompt"} <= properties, name


async def test_generate_2d_sprite_returns_asset_path_in_structured_content():
    """assetgen.py reads body["assetPath"]; an empty structuredContent breaks it."""

    async with session() as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-1", "prompt": "player character", "gameId": "t-structured"},
        )

    assert result.isError is False
    assert result.structuredContent is not None, "annotate the return as dict[str, Any]"
    assert Path(result.structuredContent["assetPath"]).exists()


async def test_validation_failure_carries_error_code_1000():
    """§03 error codes must survive FastMCP's message prefix."""

    async with session() as client:
        result = await client.call_tool(
            "generate_2d_sprite", {"featureId": "", "prompt": "x", "gameId": "t-err"}
        )

    assert result.isError is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 1000' in text


# --------------------------------------------------------------------------
# Style consistency
# --------------------------------------------------------------------------


def test_palette_is_deterministic_per_game():
    assert derive("g1", "pixel art").palette == derive("g1", "pixel art").palette


def test_different_games_get_different_palettes():
    assert derive("g1", "pixel art").palette != derive("g2", "pixel art").palette


async def test_style_is_locked_after_first_use():
    """A game's palette must not drift once assets exist."""

    async with session() as client:
        first = await client.call_tool(
            "establish_art_style", {"gameId": "t-lock", "artStyle": "pixel art"}
        )
        # A later call naming a *different* style must not re-skin the game.
        second = await client.call_tool(
            "establish_art_style", {"gameId": "t-lock", "artStyle": "noir"}
        )

    assert first.structuredContent["palette"] == second.structuredContent["palette"]


async def test_same_inputs_regenerate_identical_bytes():
    """Reproducibility: a rejected asset can be regenerated exactly."""

    args = {"featureId": "f-repro", "prompt": "a tree prop", "gameId": "t-repro"}
    async with session() as client:
        first = await client.call_tool("generate_2d_sprite", args)
        first_bytes = Path(first.structuredContent["assetPath"]).read_bytes()
        second = await client.call_tool("generate_2d_sprite", args)
        second_bytes = Path(second.structuredContent["assetPath"]).read_bytes()

    assert first_bytes == second_bytes


# --------------------------------------------------------------------------
# Asset-kind classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Add left/right movement and a jump for the player", "character"),
        ("a wild slime creature", "monster"),
        ("an enemy monster", "monster"),
        ("grass terrain tile for the overworld", "tile"),
        ("a tall oak tree", "prop"),
        ("inventory panel", "ui_panel"),
        ("start button", "ui_button"),
        ("quest marker icon", "icon"),
        # "적" is a substring of ordinary words; only the standalone/compound
        # forms mean "enemy".
        ("가죽 갑옷을 입은 여성 도적 캐릭터", "character"),
        ("적군 병사", "monster"),
        ("something entirely unspecified", "prop"),
    ],
)
def test_classify_maps_prompts_to_asset_kinds(prompt: str, expected: str):
    assert classify(prompt) == expected


async def test_generate_ui_asset_always_produces_ui():
    """Whatever the wording, this tool must not emit a terrain tile."""

    async with session() as client:
        result = await client.call_tool(
            "generate_ui_asset",
            {"featureId": "f-ui", "prompt": "grass terrain", "gameId": "t-ui"},
        )

    assert result.structuredContent["kind"].startswith("ui_")


# --------------------------------------------------------------------------
# Human verification
# --------------------------------------------------------------------------


async def test_generated_assets_start_pending_and_can_be_reviewed():
    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-rev", "prompt": "player character", "gameId": "t-review"},
        )
        asset_id = created.structuredContent["assetId"]
        assert created.structuredContent["status"] == "pending"

        pending = await client.call_tool("list_pending_assets", {"gameId": "t-review"})
        assert asset_id in {a["asset_id"] for a in pending.structuredContent["pending"]}

        approved = await client.call_tool(
            "review_asset", {"assetId": asset_id, "approved": True, "note": "good"}
        )
        assert approved.structuredContent["status"] == "approved"
        assert Path(approved.structuredContent["assetPath"]).exists()

        summary = await client.call_tool("asset_review_summary", {"gameId": "t-review"})
        assert summary.structuredContent["approved"] == 1
        assert summary.structuredContent["readyForBuild"] is True


async def test_rejected_asset_is_kept_for_inspection():
    """Rejection must not delete the evidence a reviewer is pointing at."""

    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-rej", "prompt": "a rock", "gameId": "t-reject"},
        )
        rejected = await client.call_tool(
            "review_asset",
            {
                "assetId": created.structuredContent["assetId"],
                "approved": False,
                "note": "off-palette",
            },
        )

    assert rejected.structuredContent["status"] == "rejected"
    assert Path(rejected.structuredContent["assetPath"]).exists()


async def test_reviewing_unknown_asset_is_a_validation_error():
    async with session() as client:
        result = await client.call_tool(
            "review_asset", {"assetId": "t-nope__f-1__prop", "approved": True}
        )

    assert result.isError is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# Copyright provenance
# --------------------------------------------------------------------------


async def test_every_asset_records_pixellab_provenance():
    """Commercial-use safety is deferred to PixelLab's own terms, not claimed here."""

    import json

    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-prov", "prompt": "a bush", "gameId": "t-prov"},
        )

    root = Path(created.structuredContent["assetPath"]).parents[2]
    manifest = json.loads((root / "manifests" / "t-prov.json").read_text(encoding="utf-8"))
    provenance = manifest["assets"][created.structuredContent["assetId"]]["provenance"]

    assert provenance["method"] == "pixellab"
    assert provenance["commercial_use"] == "see PixelLab terms of service"


# --------------------------------------------------------------------------
# Prompt classification invariants
#
# render.py no longer draws pixels itself, but its prompt classifier still
# decides the grid size and the material PixelLab's forced_palette is built
# from — these lock in that the classifier stays sane.
# --------------------------------------------------------------------------


def _style():
    from asset.style import derive

    return derive("t-quality", "pixel art")


def test_grid_is_large_enough_for_a_readable_character():
    """A 16px grid leaves ~4 rows for a head, which cannot hold a face."""

    assert _style().pixel_grid >= 32


@pytest.mark.parametrize(
    ("prompt", "expected_material"),
    [
        ("grass terrain tile for the overworld", "grass"),
        ("water tile for the river", "water"),
        ("sand tile for the desert beach", "sand"),
        ("stone cobble floor tile", "stone"),
        ("snow ground tile", "snow"),
        ("lava terrain tile", "lava"),
    ],
)
def test_terrain_material_follows_the_prompt_not_the_game_hue(prompt, expected_material):
    from asset.render import material_for

    assert material_for(prompt, "tile") == expected_material


def test_character_and_ui_report_no_material():
    """Reporting "grass" for a UI panel would be false provenance metadata."""

    from asset.render import material_for

    assert material_for("the player character", "character") is None
    assert material_for("a wild slime", "monster") is None
    assert material_for("inventory panel", "ui_panel") is None


# --------------------------------------------------------------------------
# Stable asset paths
#
# assetgen.py hands assetPath straight to UnityMcpServer's import_asset, so a
# review decision must not move the file. Files used to be relocated into
# approved/ or rejected/, which invalidated Unity's reference on *both*
# outcomes - verified by reproducing the orchestrator's call order.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("approved", [True, False])
async def test_review_never_moves_the_file_unity_imported(approved):
    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-stable",
                "prompt": "a rock",
                "gameId": f"t-stable-{approved}",
            },
        )
        imported_path = Path(created.structuredContent["assetPath"])
        assert imported_path.exists()

        reviewed = await client.call_tool(
            "review_asset",
            {"assetId": created.structuredContent["assetId"], "approved": approved},
        )

    assert reviewed.structuredContent["assetPath"] == str(imported_path)
    assert imported_path.exists(), "the path Unity imported must still resolve"
    assert reviewed.structuredContent["status"] == ("approved" if approved else "rejected")


async def test_review_status_is_recorded_in_the_manifest_not_the_path():
    import json

    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-meta", "prompt": "a tree", "gameId": "t-meta"},
        )
        asset_id = created.structuredContent["assetId"]
        await client.call_tool(
            "review_asset", {"assetId": asset_id, "approved": False, "note": "off-palette"}
        )

    root = Path(created.structuredContent["assetPath"]).parents[2]
    manifest = json.loads((root / "manifests" / "t-meta.json").read_text(encoding="utf-8"))
    record = manifest["assets"][asset_id]
    assert record["status"] == "rejected"
    assert record["review_note"] == "off-palette"
    assert record["asset_path"] == created.structuredContent["assetPath"]


async def test_omitting_game_id_collides_two_games_onto_one_project():
    """Documents the cost of the missing gameId argument (§03 contract gap).

    Two games with the same feature id and prompt land on the same assetId,
    palette and file. This test asserts the *current* behaviour so that wiring
    gameId through the orchestrator visibly changes it. The prompt is held
    identical on purpose — assetId now also keys off a prompt digest (fixes the
    separate same-kind-different-prompt collision), so only a same-prompt pair
    isolates the gameId gap this test documents.
    """

    async with session() as client:
        first = await client.call_tool(
            "generate_2d_sprite", {"featureId": "f-collide", "prompt": "a knight"}
        )
        second = await client.call_tool(
            "generate_2d_sprite", {"featureId": "f-collide", "prompt": "a knight"}
        )

    assert first.structuredContent["gameId"] == second.structuredContent["gameId"] == "default"
    assert first.structuredContent["assetId"] == second.structuredContent["assetId"]
    assert first.structuredContent["assetPath"] == second.structuredContent["assetPath"]
    assert first.structuredContent["styleSeed"] == second.structuredContent["styleSeed"]


@pytest.mark.parametrize(
    "art_style",
    [
        "monochrome silhouette 2D",
        "monochrome silhouette 2D pixel art",
        "흑백 픽셀 아트",
        "grayscale platformer art",
    ],
)
def test_achromatic_art_style_produces_a_colourless_palette(art_style: str):
    """Measured 2026-07-30: a "monochrome silhouette 2D" brief came back with
    ``character_primary: #45d35d``. The palette was built from the seed alone,
    so the one word in the brief that constrains colour did nothing — and the
    rendering-style keyword ("pixel") won the profile lookup outright."""

    style = derive("t-monochrome", art_style)

    for role, value in style.palette.items():
        r, g, b = int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
        assert r == g == b, f"{role} is not grey: {value}"

    # Materials and characters derive their own colours; they must go grey too.
    assert all(r == g == b for r, g, b in style.material_ramp("grass").values())
    assert all(r == g == b for r, g, b in style.character_ramp().values())


def test_colourful_art_style_is_left_colourful():
    """The achromatic check must not bleach every other style."""

    style = derive("t-monochrome", "dark fantasy")
    assert any(len(set(style.rgb(role))) > 1 for role in style.palette)


async def test_art_style_argument_reaches_the_palette():
    """The design document's art_style has no §03 route to this server, so the
    optional artStyle argument is the only way it can arrive."""

    async with session() as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-style",
                "prompt": "a knight",
                "gameId": "t-artstyle",
                "artStyle": "dark fantasy",
            },
        )
        locked = await client.call_tool("establish_art_style", {"gameId": "t-artstyle"})

    assert result.isError is False
    assert locked.structuredContent["artStyle"] == "dark fantasy"
