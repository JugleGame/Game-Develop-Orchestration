"""Tests for AssetGenMcpServer.

Driven through a real MCP session (SDK in-memory transport), so the MCP
contract — tool names, argument casing, structuredContent, error codes — is
exercised exactly as the orchestrator will exercise it.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp import Client
from PIL import Image

from asset import pixellab_client
from asset.render import classify
from asset.server import DEFAULT_ASSET_ROOT, _configured_asset_root, mcp
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

    def _fake_prototype(*, prompt, width, height, seed, **kwargs):
        colour = (seed & 0xFF, (seed >> 8) & 0xFF, (seed >> 16) & 0xFF, 255)
        return (
            Image.new("RGBA", (width, height), colour),
            {"type": "generations", "generations": 1.0},
            "create_image",
        )

    monkeypatch.setattr(pixellab_client, "generate_image", _fake_generate)
    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with Client(mcp) as client:
        yield client


# --------------------------------------------------------------------------
# Agent-first MCP contract
# --------------------------------------------------------------------------


async def test_exposes_every_contract_tool():
    async with session() as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert {
        "prepare_asset_prompt",
        "generate_2d_sprite",
        "generate_ui_asset",
        "inspect_asset",
        "list_assets",
    } <= names
    assert "generate_3d_placeholder" not in names


async def test_contract_tools_use_camel_case_argument_names():
    """The orchestrator sends featureId/prompt; snake_case would 400."""

    async with session() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in ("generate_2d_sprite", "generate_ui_asset"):
        properties = set(tools[name].input_schema["properties"])
        assert {"featureId", "prompt"} <= properties, name

    assert "assetKind" in tools["generate_2d_sprite"].input_schema["properties"]


async def test_prompt_preflight_returns_kind_specific_questions_when_incomplete():
    async with session() as client:
        result = await client.call_tool("prepare_asset_prompt", {"assetKind": "tile"})

    body = result.structured_content
    assert body["readyForPrototype"] is False
    assert body["prompt"] is None
    questions = {item["field"]: item for item in body["questions"]}
    assert {"subject", "purpose", "composition", "mustHave", "artStyle"} <= questions.keys()
    assert "repeat axis" in questions["composition"]["question"]
    assert questions["avoid"]["required"] is False


async def test_prompt_preflight_orders_structure_and_revision_feedback():
    async with session() as client:
        result = await client.call_tool(
            "prepare_asset_prompt",
            {
                "assetKind": "prop",
                "subject": "a mechanical lamp",
                "purpose": "a 32 px gameplay pickup",
                "composition": "side view with a broad base and narrow chimney",
                "mustHave": ["one connected body", "three support feet"],
                "avoid": ["floating parts"],
                "artStyle": "limited-palette industrial pixel art",
                "isRevision": True,
                "preserve": ["warm glass color"],
                "change": ["replace the flat base with three visible feet"],
            },
        )

    body = result.structured_content
    prompt = body["prompt"]
    assert body["readyForPrototype"] is True
    assert body["feedbackApplied"] is True
    assert prompt.index("Required visual structure") < prompt.index("Revision target")
    assert prompt.index("Revision target") < prompt.index("Readability target")
    assert body["artStyle"] not in prompt
    # Exclusions are recorded but never handed to PixelLab: it draws the noun
    # and drops the negation, so the list would summon what it forbids.
    assert body["exclusions"] == ["floating parts"]
    assert "floating parts" not in prompt
    assert "Exclude" not in prompt


async def test_explicit_asset_kind_overrides_ambiguous_prompt_keywords():
    async with session() as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-explicit-kind",
                "prompt": "a fire bowl standing above the ground",
                "gameId": "t-explicit-kind",
                "assetKind": "prop",
            },
        )

    assert result.structured_content["kind"] == "prop"


async def test_invalid_explicit_asset_kind_is_a_validation_error():
    async with session() as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-bad-kind",
                "prompt": "a lamp",
                "gameId": "t-bad-kind",
                "assetKind": "portrait",
            },
        )

    assert result.is_error is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 1000' in text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gameId", "../outside"),
        ("gameId", "game/name"),
        ("gameId", "game__name"),
        ("featureId", "..\\outside"),
        ("featureId", "feature name"),
    ],
)
async def test_generation_rejects_path_like_identifiers(field, value):
    arguments = {
        "featureId": "f-safe",
        "gameId": "g-safe",
        "prompt": "a lantern prop",
    }
    arguments[field] = value

    async with session() as client:
        result = await client.call_tool("generate_2d_sprite", arguments)

    assert result.is_error is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 1000' in text


def test_default_asset_root_is_repository_runtime_output():
    assert DEFAULT_ASSET_ROOT == Path(__file__).resolve().parents[3] / "var" / "assets"
    assert _configured_asset_root("./var/assets") == DEFAULT_ASSET_ROOT


async def test_generate_2d_sprite_returns_asset_path_in_structured_content():
    """assetgen.py reads body["assetPath"]; an empty structuredContent breaks it."""

    async with session() as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-1", "prompt": "player character", "gameId": "t-structured"},
        )

    assert result.is_error is False
    assert result.structured_content is not None, "annotate the return as dict[str, Any]"
    assert Path(result.structured_content["assetPath"]).exists()


def test_manifest_save_keeps_the_previous_file_if_replacement_fails(tmp_path, monkeypatch):
    from asset import server

    monkeypatch.setattr(server, "ROOT", tmp_path)
    path = tmp_path / "manifests" / "g-safe.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"game_id": "g-safe", "assets": {"old": {}}}', encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("replacement interrupted")

    monkeypatch.setattr(server.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement interrupted"):
        server._save_manifest({"game_id": "g-safe", "assets": {"new": {}}})

    assert json.loads(path.read_text(encoding="utf-8"))["assets"] == {"old": {}}
    assert not list(path.parent.glob("*.tmp"))


async def test_validation_failure_carries_error_code_1000():
    """Contract error codes must survive the MCP message prefix."""

    async with session() as client:
        result = await client.call_tool(
            "generate_2d_sprite", {"featureId": "", "prompt": "x", "gameId": "t-err"}
        )

    assert result.is_error is True
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

    assert first.structured_content["palette"] == second.structured_content["palette"]


async def test_same_inputs_regenerate_identical_bytes():
    """Reproducibility: a rejected asset can be regenerated exactly."""

    args = {"featureId": "f-repro", "prompt": "a tree prop", "gameId": "t-repro"}
    async with session() as client:
        first = await client.call_tool("generate_2d_sprite", args)
        first_bytes = Path(first.structured_content["assetPath"]).read_bytes()
        second = await client.call_tool("generate_2d_sprite", args)
        second_bytes = Path(second.structured_content["assetPath"]).read_bytes()

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
        ("a rectangular side-view terrain tile", "tile"),
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

    assert result.structured_content["kind"].startswith("ui_")


async def test_variation_batch_requires_an_approved_mcp_prototype(monkeypatch):
    async with session() as client:
        prototype = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-batch-source",
                "prompt": "treasure chest prop",
                "gameId": "t-batch-approval",
            },
        )
        result = await client.call_tool(
            "generate_2d_variations",
            {
                "featureId": "f-batch",
                "prototypeAssetId": prototype.structured_content["assetId"],
                "prompts": ["red treasure chest prop"],
                "gameId": "t-batch-approval",
            },
        )

    assert result.is_error is True
    assert "prototype asset must be approved" in "".join(
        getattr(block, "text", "") for block in result.content
    )


async def test_variation_batch_uses_approved_style_references(monkeypatch):
    captured = []

    def _fake_variations(**kwargs):
        captured.append(kwargs)
        # The provider deduces the output size from the style references, so
        # the fake answers at the reference's size rather than a requested one.
        width, height = kwargs["style_images"][0].size
        box = (width // 4, height // 4, width * 3 // 4, height * 3 // 4)
        first = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        first.paste((10, 20, 30, 255), box)
        second = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        second.paste((20, 30, 40, 255), box)
        return (
            [first, second],
            {"type": "generations", "generations": 2.0},
            f"job-{len(captured)}",
        )

    monkeypatch.setattr(pixellab_client, "generate_with_style", _fake_variations)

    async with session() as client:
        prototype = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-batch-source",
                "prompt": "treasure chest prop",
                "gameId": "t-batch-style",
                "artStyle": "dark fantasy pixel art",
            },
        )
        await client.call_tool(
            "review_asset",
            {"assetId": prototype.structured_content["assetId"], "approved": True},
        )
        second_anchor = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-batch-anchor",
                "prompt": "iron key prop",
                "gameId": "t-batch-style",
                "assetKind": "prop",
            },
        )
        await client.call_tool(
            "review_asset",
            {"assetId": second_anchor.structured_content["assetId"], "approved": True},
        )
        result = await client.call_tool(
            "generate_2d_variations",
            {
                "featureId": "f-batch",
                "prototypeAssetId": prototype.structured_content["assetId"],
                "prompts": ["red treasure chest prop", "blue treasure chest prop"],
                "gameId": "t-batch-style",
                "styleAssetIds": [second_anchor.structured_content["assetId"]],
            },
        )
        selected_id = result.structured_content["assets"][0]["assetId"]
        await client.call_tool("review_asset", {"assetId": selected_id, "approved": True})
        selected = await client.call_tool("inspect_asset", {"assetId": selected_id})

    body = result.structured_content
    assert result.is_error is False
    assert body["workflowStage"] == "variations"
    assert body["imagesGenerated"] == 4
    assert len(body["assets"]) == 4
    assert len(captured) == 2
    assert len(body["styleAssetIds"]) == 2
    assert len(captured[0]["style_images"]) == 2
    assert captured[0]["style_images"][0].tobytes() == captured[1]["style_images"][0].tobytes()
    assert "output_size" not in captured[0]
    assert all(Path(asset["assetPath"]).exists() for asset in body["assets"])
    assert selected.structured_content["semanticStatus"] == "approved"
    # An approved variation is itself a PixelLab image of this game, so it can
    # anchor the next batch. Anchor eligibility used to demand the exact
    # provenance string "pixellab-mcp", which excluded it and every REST asset.
    assert selected.structured_content["readyForVariations"] is True
    assert selected.structured_content["readyForImport"] is True
    assert selected.structured_content["nextAction"] == "import_asset"


async def test_variation_batch_rejects_unapproved_style_reference(monkeypatch):
    async with session() as client:
        prototype = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-approved",
                "prompt": "chest prop",
                "gameId": "t-style-gate",
            },
        )
        pending_anchor = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-pending",
                "prompt": "key prop",
                "gameId": "t-style-gate",
            },
        )
        await client.call_tool(
            "review_asset",
            {"assetId": prototype.structured_content["assetId"], "approved": True},
        )
        result = await client.call_tool(
            "generate_2d_variations",
            {
                "featureId": "f-batch",
                "prototypeAssetId": prototype.structured_content["assetId"],
                "styleAssetIds": [pending_anchor.structured_content["assetId"]],
                "prompts": ["closed chest prop"],
                "gameId": "t-style-gate",
            },
        )

    assert result.is_error is True
    assert "every style asset must be approved" in "".join(
        getattr(block, "text", "") for block in result.content
    )


async def test_variation_batch_accepts_a_character_through_bitforge(monkeypatch):
    """Every character is a 1:2 kind, so every character prototype is
    non-square. The square gate made the server's only style-reference path
    unusable for exactly the assets whose style matters most."""

    captured = []

    def _fake_bitforge(**kwargs):
        captured.append(kwargs)
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (7, 8, 9, 255)),
            {"type": "generations", "generations": 1.0},
        )

    def _unexpected_variations(**kwargs):
        raise AssertionError("a single reference must not go through style-v2")

    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _fake_bitforge)
    monkeypatch.setattr(pixellab_client, "generate_with_style", _unexpected_variations)
    async with session() as client:
        prototype = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-character",
                "prompt": "player character, no city",
                "gameId": "t-square-gate",
                "assetKind": "character",
            },
        )
        await client.call_tool(
            "review_asset",
            {"assetId": prototype.structured_content["assetId"], "approved": True},
        )
        result = await client.call_tool(
            "generate_2d_variations",
            {
                "featureId": "f-character-batch",
                "prototypeAssetId": prototype.structured_content["assetId"],
                "prompts": ["player character with a red cloak, no city"],
                "gameId": "t-square-gate",
                "styleStrength": 70,
                "coveragePercentage": 85.0,
                "negativeDescription": "text, watermark",
            },
        )

    body = result.structured_content
    assert result.is_error is False
    assert body["endpoint"] == "create-image-bitforge"
    assert len(captured) == 1
    call = captured[0]
    # Generated at the native canvas, not the 4x stored one.
    assert (call["width"], call["height"]) == (32, 64)
    assert call["style_strength"] == 70
    assert call["coverage_percentage"] == 85.0
    # Both sources land in the one field: the negation stripped out of the
    # description, and whatever the caller passed explicitly (an `avoid`
    # answer from prepare_asset_prompt is handed over this way).
    assert "city" in call["negative_description"]
    assert "watermark" in call["negative_description"]
    assert call["style_image"].size == (128, 256)
    # Stored at the same size as every other sprite in the game.
    with Image.open(body["assets"][0]["assetPath"]) as saved:
        assert saved.size == (128, 256)


async def test_variation_batch_rejects_bitforge_only_arguments_on_the_other_path(
    monkeypatch,
):
    """Several references cannot use bitforge, which takes exactly one. The
    controls that only exist there fail loudly rather than doing nothing."""

    def _unexpected(**kwargs):
        raise AssertionError("API must not be called for a rejected request")

    monkeypatch.setattr(pixellab_client, "generate_with_style", _unexpected)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _unexpected)
    async with session() as client:
        first = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-two-anchor",
                "prompt": "treasure chest prop",
                "gameId": "t-two-anchor",
                "assetKind": "prop",
            },
        )
        second = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-two-anchor-b",
                "prompt": "iron key prop",
                "gameId": "t-two-anchor",
                "assetKind": "prop",
            },
        )
        for created in (first, second):
            await client.call_tool(
                "review_asset",
                {"assetId": created.structured_content["assetId"], "approved": True},
            )
        result = await client.call_tool(
            "generate_2d_variations",
            {
                "featureId": "f-two-anchor-batch",
                "prototypeAssetId": first.structured_content["assetId"],
                "styleAssetIds": [second.structured_content["assetId"]],
                "prompts": ["red treasure chest prop"],
                "gameId": "t-two-anchor",
                "styleStrength": 70,
            },
        )

    assert result.is_error is True
    message = "".join(getattr(block, "text", "") for block in result.content)
    assert "styleStrength" in message
    assert "generate-with-style-v2" in message


async def test_inspect_asset_reports_technical_failure_and_next_action():
    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-inspect",
                "prompt": "lamp prop",
                "gameId": "t-inspect",
                "assetKind": "prop",
            },
        )
        inspected = await client.call_tool(
            "inspect_asset", {"assetId": created.structured_content["assetId"]}
        )

    body = inspected.structured_content
    assert body["technicalStatus"] == "fail"
    assert body["semanticStatus"] == "human_review_required"
    assert body["humanReviewStatus"] == "pending"
    assert body["readyForVariations"] is False
    assert "transparent_background_missing" in body["inspection"]["failures"]
    assert body["inspection"]["requiresSemanticReview"] is True
    assert body["nextAction"] == "regenerate_after_technical_fix"


def test_technical_inspection_warns_when_an_isolated_subject_touches_the_bottom_edge():
    from asset.quality import inspect

    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for x in range(8, 24):
        for y in range(8, 32):
            image.putpixel((x, y), (255, 255, 255, 255))

    inspection = inspect(image, "prop", (32, 32))

    assert inspection["technicalStatus"] == "pass"
    assert "subject_may_be_clipped" in inspection["warnings"]


async def test_inspect_asset_restores_feedback_and_escalates_after_three_rejections():
    latest = None
    async with session() as client:
        for index in range(3):
            latest = await client.call_tool(
                "generate_2d_sprite",
                {
                    "featureId": "f-retry",
                    "prompt": f"lamp prop revision {index}",
                    "gameId": "t-retry",
                    "assetKind": "prop",
                },
            )
            path = Path(latest.structured_content["assetPath"])
            with Image.open(path) as opened:
                width, height = opened.size
            replacement = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            for x in range(width // 4, width * 3 // 4):
                for y in range(height // 4, height * 3 // 4):
                    replacement.putpixel((x, y), (30, 40, 50, 255))
            replacement.save(path)
            await client.call_tool(
                "review_asset",
                {
                    "assetId": latest.structured_content["assetId"],
                    "approved": False,
                    "preserve": ["clear silhouette"],
                    "change": ["make the support wider"],
                },
            )

        inspected = await client.call_tool(
            "inspect_asset", {"assetId": latest.structured_content["assetId"]}
        )

    body = inspected.structured_content
    assert body["technicalStatus"] == "pass"
    assert body["humanReviewStatus"] == "rejected"
    assert body["readyForVariations"] is False
    assert body["feedback"]["preserve"] == ["clear silhouette"]
    assert body["rejectedPrototypeAttempts"] == 3
    assert body["escalationRequired"] is True
    assert body["nextAction"] == "prepare_revision_from_feedback"


async def test_list_assets_recovers_rejected_work_by_feature():
    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-resume",
                "prompt": "clock prop",
                "gameId": "t-resume",
                "assetKind": "prop",
            },
        )
        await client.call_tool(
            "review_asset",
            {
                "assetId": created.structured_content["assetId"],
                "approved": False,
                "change": ["make the hands readable"],
            },
        )
        listed = await client.call_tool(
            "list_assets",
            {
                "gameId": "t-resume",
                "status": "rejected",
                "featureId": "f-resume",
                "detail": True,
            },
        )

    assert listed.structured_content["count"] == 1
    assert listed.structured_content["assets"][0]["review_feedback"]["change"] == [
        "make the hands readable"
    ]


async def test_list_assets_defaults_to_compact_bounded_pages():
    async with session() as client:
        for index in range(3):
            await client.call_tool(
                "generate_2d_sprite",
                {
                    "featureId": f"f-page-{index}",
                    "prompt": f"clock prop {index}",
                    "gameId": "t-pages",
                    "assetKind": "prop",
                },
            )
        first = await client.call_tool("list_assets", {"gameId": "t-pages", "limit": 2})
        second = await client.call_tool(
            "list_assets",
            {"gameId": "t-pages", "limit": 2, "cursor": first.structured_content["nextCursor"]},
        )

    assert first.structured_content["count"] == 3
    assert first.structured_content["pageCount"] == 2
    assert first.structured_content["nextCursor"] == "2"
    assert first.structured_content["detail"] is False
    assert "prompt" not in first.structured_content["assets"][0]
    assert "provenance" not in first.structured_content["assets"][0]
    assert second.structured_content["pageCount"] == 1
    assert second.structured_content["nextCursor"] is None


async def test_inspect_solid_tile_passes_horizontal_seam_check():
    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-tile-inspect",
                "prompt": "stone tile",
                "gameId": "t-tile-inspect",
                "assetKind": "tile",
            },
        )
        inspected = await client.call_tool(
            "inspect_asset", {"assetId": created.structured_content["assetId"]}
        )

    body = inspected.structured_content["inspection"]
    assert body["technicalStatus"] == "pass"
    assert body["metrics"]["horizontalSeamMeanRgbDelta"] == 0


# --------------------------------------------------------------------------
# Human verification
# --------------------------------------------------------------------------


async def test_generated_assets_start_pending_and_can_be_reviewed():
    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-rev", "prompt": "player character", "gameId": "t-review"},
        )
        asset_id = created.structured_content["assetId"]
        assert created.structured_content["status"] == "pending"

        pending = await client.call_tool("list_pending_assets", {"gameId": "t-review"})
        assert asset_id in {a["asset_id"] for a in pending.structured_content["pending"]}

        approved = await client.call_tool(
            "review_asset", {"assetId": asset_id, "approved": True, "note": "good"}
        )
        assert approved.structured_content["status"] == "approved"
        assert Path(approved.structured_content["assetPath"]).exists()

        summary = await client.call_tool("asset_review_summary", {"gameId": "t-review"})
        assert summary.structured_content["approved"] == 1
        assert summary.structured_content["readyForBuild"] is True


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
                "assetId": created.structured_content["assetId"],
                "approved": False,
                "note": "off-palette",
            },
        )

    assert rejected.structured_content["status"] == "rejected"
    assert Path(rejected.structured_content["assetPath"]).exists()


async def test_reviewing_unknown_asset_is_a_validation_error():
    async with session() as client:
        result = await client.call_tool(
            "review_asset", {"assetId": "t-nope__f-1__prop", "approved": True}
        )

    assert result.is_error is True
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

    root = Path(created.structured_content["assetPath"]).parents[2]
    manifest = json.loads((root / "manifests" / "t-prov.json").read_text(encoding="utf-8"))
    provenance = manifest["assets"][created.structured_content["assetId"]]["provenance"]

    assert provenance["method"] == "pixellab-mcp"
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


@pytest.mark.parametrize(
    "prompt",
    [
        "a brass mechanical lantern prop",
        "a bronze statue prop",
        "a copper pipe prop",
    ],
)
def test_metal_props_keep_their_explicit_material_intent(prompt):
    from asset.render import material_for
    from asset.server import _pixellab_palette

    assert material_for(prompt, "prop") == "metal"
    assert _pixellab_palette(_style(), "prop", prompt) is None


def test_unknown_prop_does_not_force_a_foliage_palette():
    from asset.render import material_for
    from asset.server import _pixellab_palette

    assert material_for("a mechanical lantern prop", "prop") is None
    assert _pixellab_palette(_style(), "prop", "a mechanical lantern prop") is None


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
        imported_path = Path(created.structured_content["assetPath"])
        assert imported_path.exists()

        reviewed = await client.call_tool(
            "review_asset",
            {"assetId": created.structured_content["assetId"], "approved": approved},
        )

    assert reviewed.structured_content["assetPath"] == str(imported_path)
    assert imported_path.exists(), "the path Unity imported must still resolve"
    assert reviewed.structured_content["status"] == ("approved" if approved else "rejected")


async def test_review_status_is_recorded_in_the_manifest_not_the_path():
    import json

    async with session() as client:
        created = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-meta", "prompt": "a tree", "gameId": "t-meta"},
        )
        asset_id = created.structured_content["assetId"]
        reviewed = await client.call_tool(
            "review_asset",
            {
                "assetId": asset_id,
                "approved": False,
                "note": "off-palette",
                "preserve": ["readable silhouette"],
                "change": ["use the shared cool palette"],
                "artStyleFeedback": "reduce saturation",
            },
        )

    root = Path(created.structured_content["assetPath"]).parents[2]
    manifest = json.loads((root / "manifests" / "t-meta.json").read_text(encoding="utf-8"))
    record = manifest["assets"][asset_id]
    assert record["status"] == "rejected"
    assert record["review_note"] == "off-palette"
    assert record["review_feedback"] == {
        "preserve": ["readable silhouette"],
        "change": ["use the shared cool palette"],
        "artStyle": "reduce saturation",
    }
    assert reviewed.structured_content["feedback"] == record["review_feedback"]
    assert record["asset_path"] == created.structured_content["assetPath"]


async def test_omitting_game_id_collides_two_games_onto_one_project():
    """Documents the legacy behavior when gameId is omitted.

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

    assert first.structured_content["gameId"] == second.structured_content["gameId"] == "default"
    assert first.structured_content["assetId"] == second.structured_content["assetId"]
    assert first.structured_content["assetPath"] == second.structured_content["assetPath"]
    assert first.structured_content["styleSeed"] == second.structured_content["styleSeed"]


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
    """When artStyle is omitted, the server uses the project's locked style, so the
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

    assert result.is_error is False
    assert locked.structured_content["artStyle"] == "dark fantasy"


# --------------------------------------------------------------------------
# Animation frames (Issue #27)
# --------------------------------------------------------------------------


def _stub_animation(monkeypatch, calls, opaque: bool = False):
    """Return frames that differ per index so ordering is observable."""

    def _fake_animation(*, first_frame, action, frame_count, description=None, **kwargs):
        calls.append(
            {
                "action": action,
                "frame_count": frame_count,
                "size": first_frame.size,
                "description": description,
            }
        )
        frames = []
        for index in range(frame_count):
            frame = Image.new("RGBA", first_frame.size, (10 * index, 20, 30, 255 if opaque else 0))
            if not opaque:
                # A subject on transparent ground, the shape a sprite needs.
                box = (
                    first_frame.width // 4,
                    first_frame.height // 4,
                    first_frame.width * 3 // 4,
                    first_frame.height * 3 // 4,
                )
                frame.paste((10 * index, 20, 30, 255), box)
            frames.append(frame)
        return frames, {"type": "generations", "generations": float(frame_count)}, "job-anim"

    monkeypatch.setattr(pixellab_client, "create_animation", _fake_animation)


async def _approved_prototype(client, game_id: str, feature_id: str) -> str:
    prototype = await client.call_tool(
        "generate_2d_sprite",
        {
            "featureId": feature_id,
            "prompt": "treasure chest prop",
            "gameId": game_id,
            "artStyle": "dark fantasy pixel art",
        },
    )
    asset_id = prototype.structured_content["assetId"]
    await client.call_tool("review_asset", {"assetId": asset_id, "approved": True})
    return asset_id


async def test_animation_requires_an_approved_first_frame(monkeypatch):
    calls: list[dict] = []
    _stub_animation(monkeypatch, calls)

    async with session() as client:
        prototype = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-anim-source",
                "prompt": "treasure chest prop",
                "gameId": "t-anim-gate",
            },
        )
        result = await client.call_tool(
            "generate_2d_animation",
            {
                "featureId": "f-anim",
                "firstFrameAssetId": prototype.structured_content["assetId"],
                "action": "walk cycle",
                "gameId": "t-anim-gate",
            },
        )

    assert result.is_error is True
    assert "must be approved" in "".join(
        getattr(block, "text", "") for block in result.content
    )
    assert calls == [], "an unapproved frame must never reach the provider"


async def test_animation_saves_frames_in_play_order(monkeypatch):
    calls: list[dict] = []
    _stub_animation(monkeypatch, calls)

    async with session() as client:
        first_frame_id = await _approved_prototype(client, "t-anim-order", "f-anim-source")
        result = await client.call_tool(
            "generate_2d_animation",
            {
                "featureId": "f-anim",
                "firstFrameAssetId": first_frame_id,
                "action": "walk cycle",
                "gameId": "t-anim-order",
                "frameCount": 4,
            },
        )

    body = result.structured_content
    assert result.is_error is False
    assert body["frameCount"] == 4
    assert body["status"] == "pending", "frames wait for human review like every other asset"
    assert [frame["frameIndex"] for frame in body["frames"]] == [0, 1, 2, 3]

    names = [Path(frame["assetPath"]).name for frame in body["frames"]]
    assert names == sorted(names), "file names must sort into play order"

    index = json.loads(Path(body["indexPath"]).read_text(encoding="utf-8"))
    assert index["firstFrameAssetId"] == first_frame_id
    assert index["action"] == "walk cycle"
    assert index["jobId"] == "job-anim"
    assert index["usage"]["generations"] == 4.0
    assert calls[0]["frame_count"] == 4

    async with session() as client:
        inspected = await client.call_tool(
            "inspect_asset", {"assetId": body["frames"][0]["assetId"]}
        )

    # A generated frame is not importable until a human has looked at it.
    assert inspected.structured_content["readyForImport"] is False
    assert inspected.structured_content["humanReviewStatus"] == "pending"


async def test_animation_rejects_a_frame_count_the_provider_refuses(monkeypatch):
    calls: list[dict] = []
    _stub_animation(monkeypatch, calls)

    async with session() as client:
        first_frame_id = await _approved_prototype(client, "t-anim-range", "f-anim-source")
        result = await client.call_tool(
            "generate_2d_animation",
            {
                "featureId": "f-anim",
                "firstFrameAssetId": first_frame_id,
                "action": "walk cycle",
                "gameId": "t-anim-range",
                "frameCount": 5,
            },
        )

    assert result.is_error is True
    assert "frameCount" in "".join(getattr(block, "text", "") for block in result.content)
    assert calls == []


async def test_animation_reports_frames_that_come_back_on_a_plate(monkeypatch):
    """An opaque sequence cannot be used as a sprite; say so at generation time."""

    calls: list[dict] = []
    _stub_animation(monkeypatch, calls, opaque=True)

    async with session() as client:
        first_frame_id = await _approved_prototype(client, "t-anim-opaque", "f-anim-source")
        result = await client.call_tool(
            "generate_2d_animation",
            {
                "featureId": "f-anim",
                "firstFrameAssetId": first_frame_id,
                "action": "walk cycle",
                "gameId": "t-anim-opaque",
                "frameCount": 4,
            },
        )

    body = result.structured_content
    assert body["technicalStatus"] == "fail"
    assert "transparent_background_missing" in body["technicalFailures"]
    assert all(frame["technicalStatus"] == "fail" for frame in body["frames"])


async def test_animation_passes_inspection_when_frames_carry_alpha(monkeypatch):
    calls: list[dict] = []
    _stub_animation(monkeypatch, calls)

    async with session() as client:
        first_frame_id = await _approved_prototype(client, "t-anim-alpha", "f-anim-source")
        result = await client.call_tool(
            "generate_2d_animation",
            {
                "featureId": "f-anim",
                "firstFrameAssetId": first_frame_id,
                "action": "walk cycle",
                "gameId": "t-anim-alpha",
                "frameCount": 4,
            },
        )

    body = result.structured_content
    assert body["technicalStatus"] == "pass"
    assert body["technicalFailures"] == []


async def test_animation_reports_sequence_metrics_and_anchors(monkeypatch):
    """A sequence that shakes must be visible at generation time, not after import."""

    calls: list[dict] = []

    def _drifting(*, first_frame, action, frame_count, description=None, **kwargs):
        calls.append({"frame_count": frame_count})
        frames = []
        for index in range(frame_count):
            frame = Image.new("RGBA", first_frame.size, (0, 0, 0, 0))
            left = 8 + index * 10
            frame.paste((10, 20, 30, 255), (left, 40, left + 30, 110))
            frames.append(frame)
        return frames, {"type": "generations", "generations": 2.0}, "job-drift"

    monkeypatch.setattr(pixellab_client, "create_animation", _drifting)

    async with session() as client:
        first_frame_id = await _approved_prototype(client, "t-anim-drift", "f-anim-source")
        result = await client.call_tool(
            "generate_2d_animation",
            {
                "featureId": "f-anim",
                "firstFrameAssetId": first_frame_id,
                "action": "walk cycle",
                "gameId": "t-anim-drift",
                "frameCount": 4,
            },
        )

    body = result.structured_content
    assert "subject_drifts_between_frames" in body["sequenceWarnings"]
    assert body["sequenceMetrics"]["anchorDriftPixels"] >= 6
    # Every frame carries the anchor Unity needs to cancel that drift.
    assert all(len(frame["footAnchor"]) == 2 for frame in body["frames"])
    assert body["frames"][0]["footAnchor"] != body["frames"][-1]["footAnchor"]
