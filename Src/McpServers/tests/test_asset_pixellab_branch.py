"""Tests for the PixelLab routing wired into asset/server.py.

There is no fallback tier: a missing key or a failing call is an MCP code-3000
tool error, not a silent degrade to placeholder art. Those guarantees are
exercised through the live tool, ``generate_2d_sprite``; the unreachable REST
helper that used to carry them has been removed.
"""

import json
import random
from dataclasses import replace

import pytest
from PIL import Image

from asset import pixellab_client, prompting, server
from asset.server import _pixellab_palette, _pixellab_style_params, _size_for
from asset.style import derive, load_or_create


@pytest.fixture
def style():
    return derive("t-pixellab-branch", "pixel art")


@pytest.fixture
def rng():
    return random.Random(1234)


@pytest.mark.parametrize(
    ("kind", "prompt", "framing"),
    [
        (
            "character",
            "2D pixel art, transparent background, side view, medium detail, "
            "medium shading, 64x64, player character",
            "full body centered",
        ),
        (
            "monster",
            "pixel art, no background, side view, medium detail, medium shading, "
            "64x64, slime monster",
            "single centered creature",
        ),
        (
            "tile",
            "pixel art, high top-down view, flat shading, medium detail, 32x32, grass tile",
            "edge-to-edge tile",
        ),
        (
            "prop",
            "pixel art, transparent background, side view, flat shading, medium detail, "
            "32x32, treasure chest prop",
            "single centered isolated object",
        ),
        (
            "ui_panel",
            "2D pixel art, transparent background, flat shading, medium detail, "
            "128x64, inventory panel",
            "text-free panel",
        ),
        (
            "icon",
            "2D pixel art, transparent background, flat shading, medium detail, "
            "32x32, quest marker icon",
            "single centered item",
        ),
    ],
)
def test_prompt_composition_removes_structured_duplication(kind, prompt, framing):
    plan = prompting.compose(prompt, kind)

    assert framing in plan.prompt
    # Style wording is kept, not deleted: every field that would carry it is
    # documented "(weakly guiding)" in PixelLab's schema, so removing it from
    # the description traded the strong signal for the weak one. The prompt
    # therefore grows by the framing rather than shrinking.
    assert plan.composed_characters > plan.original_characters
    assert plan.structured_clauses
    for clause in plan.structured_clauses:
        assert clause in plan.prompt
    assert plan.metadata()["structuredClauses"] == list(plan.structured_clauses)


def test_a_prepared_brief_still_gets_its_framing():
    """The framing used to be skipped whenever the prompt carried both
    "Composition:" and "Required visual structure:" — which is exactly what
    ``prepare`` writes. Following the intake procedure was therefore the one
    reliable way to lose the framing."""

    prepared = prompting.prepare(
        "prop",
        subject="a brass lantern",
        purpose="a 32 px pickup",
        composition="centered with a broad base and narrow top handle",
        must_have=["one connected silhouette", "three support feet"],
        art_style="pixel art",
    )
    assert prepared["readyForPrototype"] is True

    plan = prompting.compose(prepared["prompt"], "prop")

    assert "single centered isolated object" in plan.prompt
    assert "a brass lantern" in plan.prompt


# --------------------------------------------------------------------------
# The live path carries the locked style, the palette, and the native size.
#
# These assertions used to be made against an unreachable REST helper. Moving
# them onto ``generate_2d_sprite`` keeps the guarantee and tests the path that
# actually runs.
# --------------------------------------------------------------------------


async def test_the_prototype_request_carries_the_locked_style_and_palette(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}

    def _fake_prototype(**kwargs):
        captured.update(kwargs)
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)

    from mcp import Client

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-style",
                "prompt": "a rock",
                "assetKind": "prop",
                "gameId": "t-style-reaches",
            },
        )

    assert result.is_error is False
    locked = load_or_create(tmp_path, "t-style-reaches", server.DEFAULT_ART_STYLE)
    # "prop" is a 1:1 kind, so the native canvas is the grid on both axes.
    assert captured["width"] == locked.pixel_grid
    assert captured["height"] == locked.pixel_grid
    assert isinstance(captured["seed"], int)
    assert captured["style_params"] == _pixellab_style_params(locked, "prop")
    assert captured["style_params"]["shading"] == "flat shading"
    assert captured["style_params"]["view"] == locked.camera_view
    expected = _pixellab_palette(locked, "prop", "a rock") or []
    assert captured["palette"] == [
        f"#{red:02x}{green:02x}{blue:02x}" for red, green, blue in expected
    ]
    saved = Image.open(result.structured_content["assetPath"])
    assert saved.size == (locked.pixel_grid * server._PIXELLAB_UPSCALE,) * 2


# --------------------------------------------------------------------------
# forced_palette: locks PixelLab to this game's own colours, per kind
# --------------------------------------------------------------------------


def test_pixellab_palette_is_anchored_to_the_locked_style(style):
    """Same game -> every kind's palette is built from that one ``ArtStyle``,
    not picked ad hoc per call — this is what makes a PixelLab character and
    a PixelLab icon from the same game share a hue family."""

    icon = _pixellab_palette(style, "icon", "a sword icon")
    prop = _pixellab_palette(style, "prop", "a rock")

    assert len(icon) == 4
    assert len(prop) == 5

    other_style = derive("t-pixellab-branch-other", "pixel art")
    assert _pixellab_palette(style, "prop", "a rock") != _pixellab_palette(
        other_style, "prop", "a rock"
    )


def test_living_things_get_a_wider_palette_rather_than_none(style):
    """They used to get no palette at all.

    The recorded reason was range: the five-swatch ramp read as "too green, no
    character" (5/10) while dropping it scored 7/10 — a tie. The alternative
    that was assumed to cover the gap does not exist: ``style_image`` carries
    the reference's subject, not its look (measured 2026-08-22), so it cannot
    make two different subjects share a game's colours. The range objection is
    answered by widening the palette, not by removing it.
    """

    for kind in ("character", "monster"):
        palette = _pixellab_palette(style, kind, "a hero")
        assert palette == style.character_palette()
        # Wider than the five a tile or prop gets, which is the whole point.
        assert len(palette) > len(_pixellab_palette(style, "tile", "grass tile"))


def test_the_palette_lock_can_be_turned_off_for_one_asset(style):
    """A boss with its own scheme, or a colour-coded pickup."""

    for kind in ("character", "monster", "tile", "icon"):
        assert _pixellab_palette(style, kind, "grass tile") is not None
        assert _pixellab_palette(style, kind, "grass tile", palette_lock=False) is None


def test_the_character_palette_leads_with_the_games_identity(style):
    """The first four swatches are the same ramp a sprite was always drawn
    from; skin, metal, and leather follow so a face, a blade, and a strap do
    not all have to borrow the cloth hue."""

    palette = style.character_palette()
    ramp = style.character_ramp()

    assert palette[:4] == [ramp["shadow"], ramp["base"], ramp["light"], ramp["highlight"]]
    assert style.rgb("outline") in palette
    assert len(palette) == len(set(palette)), "duplicate swatches say nothing"


def test_the_character_palette_is_deterministic_and_per_game():
    """Derived from seed and art_style, both persisted — so it needs no new
    stored field and a style.json written before it existed still resolves."""

    first = derive("t-palette", "pixel art")
    again = derive("t-palette", "pixel art")
    other = derive("t-palette-other", "pixel art")

    assert first.character_palette() == again.character_palette()
    assert first.character_palette() != other.character_palette()


def test_a_monochrome_game_gets_no_tinted_character_swatches():
    """The palette must not put colour back into a game that asked for none."""

    mono = derive("t-mono-character", "monochrome pixel art")

    for red, green, blue in mono.character_palette():
        assert red == green == blue, (red, green, blue)


def test_pixellab_style_params_flattens_shading_for_inanimate_kinds(style):
    """Measured (12문서 §10-7, 2026-08-02): "medium shading" reads fine on a
    character/creature body but made a boxy prop (treasure chest) look like a
    3D render instead of flat pixel art (scored 3/5). Character and monster
    keep the richer shading; nothing inanimate does."""

    for kind in ("character", "monster"):
        assert _pixellab_style_params(style, kind)["shading"] == "medium shading"
    for kind in ("prop", "tile", "icon", "ui_panel", "ui_button"):
        assert _pixellab_style_params(style, kind)["shading"] == "flat shading"


def test_pixellab_style_params_view_is_locked_to_the_game_not_the_kind():
    """Camera view must come from the locked ``ArtStyle``, not be picked per
    call — a side-scroller and a top-down game mixing views per asset reads
    as broken, the same way mixing palettes per call did before
    ``color_image`` locked colour."""

    topdown_style = derive("t-pixellab-branch-topdown", "top-down pixel art")
    assert topdown_style.camera_view == "high top-down"
    assert _pixellab_style_params(topdown_style, "character")["view"] == "high top-down"
    assert _pixellab_style_params(topdown_style, "prop")["view"] == "high top-down"


# --------------------------------------------------------------------------
# Native generation size varies by kind (no fixed square, no downsample)
# --------------------------------------------------------------------------


def test_size_ratio_is_square_for_tile_so_it_still_tiles(style):
    assert _size_for(style, "tile") == (style.pixel_grid, style.pixel_grid)


def test_size_ratio_is_taller_than_square_for_character(style):
    """Measured 2026-08-02 (prompt-eval round 5): a 1:1 canvas crops a
    humanoid at the knee. A character needs headroom a square grid doesn't
    give it. Round 11 measured 1.5 as still too short — the figure cropped
    below the thigh — and settled on 2.0 (8/10)."""

    width, height = _size_for(style, "character")
    assert width == style.pixel_grid
    assert height == style.pixel_grid * 2


def test_size_ratio_is_square_for_monster_unlike_character(style):
    """Measured 2026-08-02 (prompt-eval round 6): the character's tall ratio
    distorted a round creature (a slime) — scored 4/10 vs. 8/10 square. A
    monster is not assumed humanoid."""

    assert _size_for(style, "monster") == (style.pixel_grid, style.pixel_grid)


def test_no_kind_generates_above_pixellabs_400px_ceiling():
    """Every configured ratio must stay inside Pixflux's 32-400px range even
    at the larger 48px grid, or a call silently 400s instead of generating."""

    from asset.server import _KIND_SIZE_RATIO

    style48 = derive("t-pixellab-branch-grid48", "isometric fantasy")
    assert style48.pixel_grid == 48
    for kind in _KIND_SIZE_RATIO:
        width, height = _size_for(style48, kind)
        assert 32 <= width <= 400
        assert 32 <= height <= 400


# --------------------------------------------------------------------------
# Key configured, call fails -> hard error, no generation
# --------------------------------------------------------------------------


async def test_a_failing_provider_call_is_a_code_3000_tool_error(monkeypatch, tmp_path):
    """No fallback tier: a failed call fails loudly rather than degrading to
    placeholder art, so a broken account never silently ships wrong assets."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _boom(**kwargs):
        raise pixellab_client.PixelLabUnavailable("quota exceeded")

    monkeypatch.setattr(pixellab_client, "generate_prototype", _boom)

    from mcp import Client

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-boom",
                "prompt": "a rock",
                "assetKind": "prop",
                "gameId": "t-boom",
            },
        )

    assert result.is_error is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 3000' in text
    assert "quota exceeded" in text


# --------------------------------------------------------------------------
# imagesGenerated surfaces through the MCP tool result
# --------------------------------------------------------------------------


async def test_generate_2d_sprite_reports_the_images_it_consumed(monkeypatch, tmp_path):
    """The shape PixelLab v2 actually returns (measured 2026-08-03).

    ``generations`` is one per image, and images are what the account's
    monthly quota is denominated in — no dollar figure is invented from it.
    """

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_generate(**kwargs):
        return Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255)), {
            "type": "generations",
            "generations": 1.0,
        }

    def _fake_prototype(**kwargs):
        image, usage = _fake_generate(**kwargs)
        return image, usage, "create_image"

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)

    from mcp import Client

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-1", "prompt": "a rock", "gameId": "t-pixellab-images", "assetKind": "prop"},
        )

    assert result.is_error is False
    assert result.structured_content["generatedBy"] == "pixellab-mcp"
    assert result.structured_content["imagesGenerated"] == 1
    assert "costUsd" not in result.structured_content


async def test_generate_2d_sprite_without_key_is_a_tool_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.delenv("PIXELLAB_API_KEY", raising=False)

    from mcp import Client

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-1", "prompt": "a rock", "gameId": "t-pixellab-nokey", "assetKind": "prop"},
        )

    assert result.is_error is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 3000' in text


# --------------------------------------------------------------------------
# detail/shading belong to the game's frozen style (#59)
# --------------------------------------------------------------------------


def test_style_detail_and_shading_reach_pixellab(style):
    lowered = replace(style, detail="low detail", shading="flat shading")

    params = _pixellab_style_params(lowered, "character")

    assert params["detail"] == "low detail"
    assert params["shading"] == "flat shading"


def test_style_shading_is_still_flattened_for_inanimate_kinds(style):
    heavy = replace(style, shading="highly detailed shading")

    assert _pixellab_style_params(heavy, "prop")["shading"] == "flat shading"
    assert _pixellab_style_params(heavy, "monster")["shading"] == "highly detailed shading"


def test_defaults_reproduce_the_previous_hardcoded_values(style):
    assert _pixellab_style_params(style, "character") == {
        "outline": "single color black outline",
        "shading": "medium shading",
        "detail": "medium detail",
        "view": style.camera_view,
        "direction": style.direction,
        "isometric": style.isometric,
    }


def test_a_per_asset_direction_overrides_only_that_call(style):
    """A game locks which way its sprites face; one asset may need another
    (a door on the west wall, an NPC turned toward the player)."""

    assert _pixellab_style_params(style, "character")["direction"] == style.direction
    assert _pixellab_style_params(style, "character", "west")["direction"] == "west"
    assert style.direction == "east"


def test_a_style_json_written_before_these_fields_still_loads(tmp_path):
    """The frozen-style guarantee: adding a field must not orphan a game."""

    legacy = {
        "game_id": "t-legacy",
        "art_style": "pixel art",
        "seed": 7,
        "palette": derive("t-legacy", "pixel art").palette,
        "pixel_grid": 32,
        "outline": True,
    }
    path = tmp_path / "styles" / "t-legacy.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = load_or_create(tmp_path, "t-legacy", "pixel art")

    assert loaded.detail == "medium detail"
    assert loaded.shading == "medium shading"
    assert loaded.camera_view == "side"


async def test_establish_art_style_freezes_detail_and_shading(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)

    from mcp import Client

    async with Client(server.mcp) as client:
        created = await client.call_tool(
            "establish_art_style",
            {
                "gameId": "t-detail",
                "artStyle": "pixel art dusk silhouette",
                "detail": "low detail",
                "shading": "flat shading",
            },
        )
        # Idempotent: a frozen game keeps the look it was created with.
        again = await client.call_tool(
            "establish_art_style",
            {"gameId": "t-detail", "artStyle": "pixel art", "detail": "highly detailed"},
        )

    assert created.structured_content["detail"] == "low detail"
    assert created.structured_content["shading"] == "flat shading"
    assert again.structured_content["detail"] == "low detail"


# --------------------------------------------------------------------------
# Negations never reach the provider (#59)
# --------------------------------------------------------------------------


def test_compose_drops_leading_negations_and_reports_them():
    plan = prompting.compose(
        "side view teenage boy protagonist, no city, no buildings, without a street", "character"
    )

    assert "city" not in plan.prompt
    assert "buildings" not in plan.prompt
    assert "street" not in plan.prompt
    assert "teenage boy protagonist" in plan.prompt
    assert plan.metadata()["removedNegations"] == ["no city", "no buildings", "without a street"]


def test_compose_keeps_a_subject_that_merely_starts_with_those_letters():
    plan = prompting.compose("nose ring detail, notched blade", "prop")

    assert "nose ring detail" in plan.prompt
    assert "notched blade" in plan.prompt
    assert plan.metadata()["removedNegations"] == []


def test_compose_keeps_an_inline_negation_rather_than_losing_the_subject():
    """Dropping the clause would drop the knight with the helmet."""

    plan = prompting.compose("a knight with no helmet", "character")

    assert "a knight with no helmet" in plan.prompt
    assert plan.metadata()["removedNegations"] == []


# --------------------------------------------------------------------------
# Per-asset canvas size (issue #57)
# --------------------------------------------------------------------------


def test_grid_override_changes_the_canvas_without_touching_the_style(style):
    """A boss three times the player's height needs its own canvas, not an
    edited game style — the style is shared and locked."""

    from asset.server import _size_for as size_for

    assert size_for(style, "character") == (32, 64)
    assert size_for(style, "character", 56) == (56, 112)
    assert style.pixel_grid == 32


def test_the_floor_follows_the_schema_not_one_unexplained_failure(style):
    """16px is inside ``CreateImagePixfluxRequest.image_size`` (minimum 16).

    The floor previously sat at 32 because one 16x32 request through the MCP
    path failed with a TaskGroup exception that named no cause. The schema
    says 16, so a 16-grid request is accepted rather than pre-rejected.
    """

    from asset.server import _resolve_size

    assert _resolve_size(style, "character", 16, "f-1") == (16, 32)


def test_size_below_pixellabs_floor_is_rejected_before_the_call(style):
    from asset.server import _resolve_size

    with pytest.raises(Exception) as excinfo:
        _resolve_size(style, "character", 8, "f-1")

    message = str(excinfo.value)
    assert '"errorCode": 1000' in message
    assert "8x16" in message
    assert "16-400" in message


def test_size_above_pixellabs_ceiling_is_rejected_before_the_call(style):
    from asset.server import _resolve_size

    with pytest.raises(Exception) as excinfo:
        _resolve_size(style, "character", 240, "f-1")

    assert "240x480" in str(excinfo.value)


def test_omitting_the_grid_keeps_the_existing_size(style):
    from asset.server import _resolve_size

    assert _resolve_size(style, "character", None, "f-1") == _size_for(style, "character")


async def test_grid_size_reaches_pixellab_and_leaves_the_style_grid_alone(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    seen: list[tuple[int, int]] = []

    def _fake_prototype(**kwargs):
        seen.append((kwargs["width"], kwargs["height"]))
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)

    from mcp import Client

    async with Client(server.mcp) as client:
        for grid in (None, 56):
            arguments = {
                "featureId": "f-1",
                "prompt": "a lone figure",
                "gameId": "t-grid-size",
                "assetKind": "character",
            }
            if grid is not None:
                arguments["gridSize"] = grid
            result = await client.call_tool("generate_2d_sprite", arguments)
            assert result.is_error is False

    assert seen == [(32, 64), (56, 112)]
    from asset.style import load_or_create

    assert load_or_create(tmp_path, "t-grid-size", "pixel art").pixel_grid == 32


async def test_grid_size_below_the_floor_fails_the_tool_call(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _never(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("PixelLab was called for an out-of-range size")

    monkeypatch.setattr(pixellab_client, "generate_prototype", _never)

    from mcp import Client

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-1",
                "prompt": "a lone figure",
                "gameId": "t-grid-floor",
                "assetKind": "character",
                "gridSize": 16,
            },
        )

    assert result.is_error is True


# --------------------------------------------------------------------------
# A failed generation must not hold the prompt hostage (#58)
# --------------------------------------------------------------------------


async def test_failure_before_billing_lets_the_same_prompt_retry(monkeypatch, tmp_path):
    """The retry must reuse the prompt verbatim, not a reworded one.

    ``render.rng_for`` seeds off the prompt text, so rewording to dodge the
    claim would regenerate the asset with a different seed.
    """

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    seeds = []

    def _boom(**kwargs):
        seeds.append(kwargs["seed"])
        raise pixellab_client.PixelLabUnavailable(
            "PixelLab MCP request failed: ValueError: image size too small"
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _boom)

    from mcp import Client

    arguments = {"featureId": "f-1", "prompt": "a rock", "gameId": "t-retry", "assetKind": "prop"}
    async with Client(server.mcp) as client:
        failed = await client.call_tool("generate_2d_sprite", arguments)
    assert failed.is_error is True
    assert "image size too small" in "".join(
        getattr(block, "text", "") for block in failed.content
    )

    def _succeed(**kwargs):
        seeds.append(kwargs["seed"])
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255)),
            {"type": "generations", "generations": 1.0},
            "create_image",
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _succeed)

    async with Client(server.mcp) as client:
        retried = await client.call_tool("generate_2d_sprite", arguments)

    assert retried.is_error is False
    assert retried.structured_content["status"] != "duplicate_blocked"
    assert retried.structured_content["assetPath"]
    assert seeds[0] == seeds[1]


async def test_failure_after_a_job_started_keeps_the_claim_and_says_what_to_do(
    monkeypatch, tmp_path
):
    """PixelLab may already have charged for it, so the block stays — but the
    response has to name the file that lifts it."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    calls = []

    def _boom(**kwargs):
        calls.append(kwargs)
        raise pixellab_client.PixelLabUnavailable(
            "PixelLab MCP job abc did not finish within 300 seconds", job_started=True
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _boom)

    from mcp import Client

    arguments = {"featureId": "f-1", "prompt": "a rock", "gameId": "t-billed", "assetKind": "prop"}
    async with Client(server.mcp) as client:
        failed = await client.call_tool("generate_2d_sprite", arguments)
        blocked = await client.call_tool("generate_2d_sprite", arguments)

    assert failed.is_error is True
    assert blocked.is_error is False
    body = blocked.structured_content
    assert body["status"] == "duplicate_blocked"
    assert body["recoveryRequired"] is True
    assert body["claimPath"].endswith(".json")
    assert "did not finish" in body["reason"]
    assert "claimPath" in body["recovery"]
    # The block did its job: no second paid attempt was made.
    assert len(calls) == 1


async def test_a_completed_claim_still_returns_the_existing_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    calls = []

    def _succeed(**kwargs):
        calls.append(kwargs)
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255)),
            {"type": "generations", "generations": 1.0},
            "create_image",
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _succeed)

    from mcp import Client

    arguments = {"featureId": "f-1", "prompt": "a rock", "gameId": "t-completed", "assetKind": "prop"}
    async with Client(server.mcp) as client:
        first = await client.call_tool("generate_2d_sprite", arguments)
        second = await client.call_tool("generate_2d_sprite", arguments)

    assert second.structured_content["duplicateBlocked"] is True
    assert second.structured_content["assetPath"] == first.structured_content["assetPath"]
    assert len(calls) == 1


# --------------------------------------------------------------------------
# Endpoint style contracts at the server boundary (issue #63)
# --------------------------------------------------------------------------


def test_map_object_style_params_speak_the_endpoints_own_vocabulary(style):
    """``/map-objects`` declares narrower enums than pixflux, so the locked
    style has to be translated rather than passed through: it has no
    "single color black outline" and spells the top level "high detail"."""

    from asset.server import _map_object_style_params

    params = _map_object_style_params(style)
    allowed = pixellab_client.STYLE_ENUMS["map-objects"]

    assert params["view"] == style.camera_view
    for field, value in params.items():
        assert value in allowed[field], (field, value)

    detailed = replace(style, detail="highly detailed")
    assert _map_object_style_params(detailed)["detail"] == "high detail"


def test_generate_map_object_sends_the_locked_style(monkeypatch, tmp_path):
    """Sending nothing let the endpoint default ``view`` to "high top-down",
    which drew a side-view game's decorations from above."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    monkeypatch.setattr(server, "ROOT", tmp_path)
    captured = {}

    def _fake_map_object(**kwargs):
        captured.update(kwargs)
        return Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255)), {}

    monkeypatch.setattr(pixellab_client, "create_map_object", _fake_map_object)

    result = server.generate_map_object(
        featureId="f-decor", prompt="a mossy rock", gameId="t-decor", size=32
    )

    style = load_or_create(tmp_path, "t-decor", server.DEFAULT_ART_STYLE)
    assert captured["view"] == style.camera_view
    assert captured["color_palette"] == _pixellab_palette(style, "prop", "a mossy rock")
    assert result["status"] == "pending"


def test_generate_map_object_rejects_a_size_below_the_endpoints_floor(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "ROOT", tmp_path)

    with pytest.raises(Exception) as excinfo:
        server.generate_map_object(
            featureId="f-decor", prompt="a mossy rock", gameId="t-decor", size=16
        )

    assert "32" in str(excinfo.value)


@pytest.mark.parametrize("tile_size", [24, 48, 64])
def test_generate_tileset_rejects_a_tile_size_outside_the_enum(
    monkeypatch, tmp_path, tile_size
):
    """``TileSize`` is an enum, so 24 and 48 are 422s despite sitting inside
    the 16-64 range this used to accept."""

    monkeypatch.setattr(server, "ROOT", tmp_path)

    with pytest.raises(Exception) as excinfo:
        server.generate_tileset(
            featureId="f-tiles",
            lowerDescription="grass",
            upperDescription="stone",
            gameId="t-tiles",
            tileSize=tile_size,
        )

    assert "tileSize" in str(excinfo.value)


def test_generate_tileset_style_values_are_accepted_by_the_endpoint(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    monkeypatch.setattr(server, "ROOT", tmp_path)
    captured = {}

    def _fake_tileset(**kwargs):
        captured.update(kwargs)
        return (
            [{"name": "wang_0", "image": Image.new("RGBA", (32, 32)), "corners": {}}],
            {},
            {},
        )

    monkeypatch.setattr(pixellab_client, "create_tileset", _fake_tileset)

    server.generate_tileset(
        featureId="f-tiles",
        lowerDescription="grass",
        upperDescription="stone",
        gameId="t-tiles",
        tileSize=32,
    )

    allowed = pixellab_client.STYLE_ENUMS["tilesets"]
    for field in ("view", "outline", "shading", "detail"):
        assert captured[field] in allowed[field], (field, captured[field])


@pytest.mark.parametrize(
    "field,value",
    [("detail", "minimal detail"), ("shading", "heavy shading")],
)
def test_establish_art_style_rejects_a_value_outside_the_schema(
    monkeypatch, tmp_path, field, value
):
    """Checked before freezing: these are written to the game's style.json and
    read by every later asset, so a wrong value would 422 the whole game."""

    monkeypatch.setattr(server, "ROOT", tmp_path)

    with pytest.raises(Exception) as excinfo:
        server.establish_art_style(gameId="t-enum", **{field: value})

    message = str(excinfo.value)
    assert '"errorCode": 1000' in message
    assert field in message
    assert not (tmp_path / "styles" / "t-enum.json").exists()


def test_establish_art_style_accepts_the_schema_values(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)

    result = server.establish_art_style(
        gameId="t-enum-ok", detail="low detail", shading="basic shading"
    )

    assert result["detail"] == "low detail"
    assert result["shading"] == "basic shading"


# --------------------------------------------------------------------------
# Negations recovered as negative_description (issue #64)
# --------------------------------------------------------------------------


def test_removed_negations_become_a_negative_description():
    """Dropping a negation from the description is right for pixflux, which
    marks the field ``(Deprecated)`` and draws the noun anyway. Discarding the
    information was not: bitforge's ``negative_description`` is live."""

    plan = prompting.compose(
        "a lone knight on a hill, no city, without buildings, avoid text", "character"
    )

    assert "city" not in plan.prompt
    assert plan.negative_description == "city, buildings, text"
    assert plan.metadata()["negativeDescription"] == "city, buildings, text"


def test_a_prompt_without_negations_has_an_empty_negative_description():
    plan = prompting.compose("a lone knight on a hill", "character")

    assert plan.negative_description == ""


def test_an_inline_negation_stays_in_the_subject():
    """"a knight with no helmet" must not lose the knight, so the clause is
    left alone and contributes nothing to the negative description."""

    plan = prompting.compose("a knight with no helmet", "character")

    assert "knight" in plan.prompt
    assert plan.negative_description == ""


def test_variation_endpoint_prefers_bitforge_only_where_it_fits():
    """One reference, square, and small enough gets the controls; anything else
    falls back to the multi-reference endpoint, which has none of them."""

    from asset.server import _BITFORGE, _STYLE_V2, _variation_endpoint

    assert _variation_endpoint(1, (32, 32)) == _BITFORGE
    assert _variation_endpoint(1, (200, 200)) == _BITFORGE
    # 201 per side is past CreateImageBitforgeRequest.image_size's maximum.
    assert _variation_endpoint(1, (201, 201)) == _STYLE_V2
    # bitforge takes exactly one style_image.
    assert _variation_endpoint(2, (32, 32)) == _STYLE_V2
    # Non-square is where bitforge falls apart (measured: a 32x64 character
    # came back as a detached hat above a body). A batch cannot grow its canvas
    # the way the prototype path does, because the output has to stay the size
    # of the reference it varies — so it takes the endpoint that works there.
    assert _variation_endpoint(1, (32, 64)) == _STYLE_V2


def test_pixellab_asset_check_accepts_every_generation_path():
    """Anchor eligibility used to demand the exact string "pixellab-mcp"."""

    from asset.server import _is_pixellab_asset

    for method in ("pixellab", "pixellab-mcp", "pixellab-api"):
        assert _is_pixellab_asset({"provenance": {"method": method}}) is True
    assert _is_pixellab_asset({"provenance": {"method": "placeholder"}}) is False
    assert _is_pixellab_asset({}) is False


# --------------------------------------------------------------------------
# isometric is a projection, not a camera view (issue #65)
# --------------------------------------------------------------------------


def test_isometric_is_its_own_field_not_a_camera_view():
    """"isometric" used to be a keyword in the CameraView table, so a game
    asking for it was sent "high top-down" — a vertical axis, not a diagonal
    one — and no field ever carried the actual request."""

    from asset.style import derive as derive_style

    iso = derive_style("t-iso", "isometric pixel art")
    assert iso.isometric is True
    # Still a downward-looking camera, which is what such a game already got.
    assert iso.camera_view == "high top-down"

    plain = derive_style("t-plain", "pixel art")
    assert plain.isometric is False
    assert plain.camera_view == "side"

    side_iso = derive_style("t-side-iso", "side-scroll isometric")
    assert side_iso.isometric is True
    assert side_iso.camera_view == "side"


def test_direction_defaults_per_game_and_is_locked():
    from asset.style import derive as derive_style

    assert derive_style("t-dir", "pixel art").direction == "east"
    assert derive_style("t-dir2", "pixel art, front-facing").direction == "south"
    assert derive_style("t-dir3", "pixel art, 뒷모습").direction == "north"


def test_a_style_json_without_the_new_fields_still_loads(tmp_path):
    """The frozen-style guarantee again: both new fields are defaulted, so a
    game locked before they existed keeps working."""

    import json

    from asset.style import ArtStyle, derive as derive_style, load_or_create as load_style

    reference = derive_style("t-old", "pixel art")
    stored = {
        "game_id": "t-old",
        "art_style": "pixel art",
        "seed": reference.seed,
        "palette": reference.palette,
        "pixel_grid": 32,
        "outline": True,
        "camera_view": "side",
        "detail": "medium detail",
        "shading": "medium shading",
    }
    path = tmp_path / "styles" / "t-old.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored), encoding="utf-8")

    loaded = load_style(tmp_path, "t-old", "pixel art")
    assert isinstance(loaded, ArtStyle)
    assert loaded.isometric is False
    assert loaded.direction == "east"


def test_server_rejects_a_direction_outside_the_enum():
    from asset.server import _direction

    assert _direction(None, "f-1") is None
    assert _direction("  West ", "f-1") == "west"

    with pytest.raises(Exception) as excinfo:
        _direction("left", "f-1")

    assert '"errorCode": 1000' in str(excinfo.value)


def test_structured_style_wording_survives_composition():
    """The fields that would carry it are all "(weakly guiding)", so deleting
    it from the description left only the weak signal."""

    plan = prompting.compose("flat shading, a mossy rock, side view", "prop")

    assert "flat shading" in plan.prompt
    assert "side view" in plan.prompt
    assert plan.metadata()["structuredClauses"] == ["flat shading", "side view"]


def test_an_exact_repeat_is_still_collapsed():
    plan = prompting.compose("a mossy rock, a mossy rock, flat shading", "prop")

    assert plan.prompt.count("a mossy rock") == 1


# --------------------------------------------------------------------------
# The keyword table no longer needs per-keyword exceptions (issue #71)
# --------------------------------------------------------------------------


def test_a_one_syllable_korean_keyword_no_longer_forces_a_special_rule():
    """``적`` matched inside ordinary words such as ``도적``, so ``_matches``
    carried a third branch just for it. The keyword is gone instead."""

    from asset import render as render_module

    assert render_module.classify("도적 캐릭터") == "character"
    assert render_module.classify("적군 무리") == "monster"

    monster_keywords = dict(render_module._KIND_KEYWORDS)["monster"]
    assert "적" not in monster_keywords
    assert "적군" in monster_keywords
    assert not any(len(word) == 1 for word in monster_keywords)


def test_english_keywords_still_match_on_word_boundaries():
    """``cta`` was dropped, but the rule it motivated is kept: English
    keywords appear inside unrelated words (``tile`` in ``volatile``)."""

    from asset import render as render_module

    assert render_module.classify("a volatile potion") == "prop"
    assert render_module.classify("a mossy tile") == "tile"
    assert "cta" not in dict(render_module._KIND_KEYWORDS)["ui_button"]


def test_matches_takes_only_the_keyword_and_the_prompt():
    """The word list argument existed solely for the one-syllable branch."""

    import inspect

    from asset import render as render_module

    assert list(inspect.signature(render_module._matches).parameters) == [
        "keyword",
        "lowered",
    ]


def test_prompt_metrics_keys_say_what_they_hold():
    """``removedStructuredClauses`` outlived what it described.

    Once composition stopped deleting style wording (#65) it was always an
    empty list, and a field named for a removal that reports nothing reads as
    "this feature is off" rather than "nothing was removed".
    """

    plan = prompting.compose("flat shading, a mossy rock", "prop")

    assert set(plan.metadata()) == {
        "originalCharacters",
        "composedCharacters",
        "characterDelta",
        "structuredClauses",
        "removedNegations",
        "negativeDescription",
    }
    assert plan.metadata()["structuredClauses"] == ["flat shading"]
    assert not hasattr(plan, "removed_structured_clauses")


async def test_palette_lock_reaches_the_prototype_request(monkeypatch, tmp_path):
    """On by default, and off for exactly the one asset that asks."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    seen: list[list[str]] = []

    def _fake_prototype(**kwargs):
        seen.append(kwargs["palette"])
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)

    from mcp import Client

    async with Client(server.mcp) as client:
        for feature, lock in (("f-locked", True), ("f-unlocked", False)):
            result = await client.call_tool(
                "generate_2d_sprite",
                {
                    "featureId": feature,
                    "prompt": "a hero",
                    "assetKind": "character",
                    "gameId": "t-palette-lock",
                    "paletteLock": lock,
                },
            )
            assert result.is_error is False

    locked_style = load_or_create(tmp_path, "t-palette-lock", server.DEFAULT_ART_STYLE)
    expected = [
        f"#{red:02x}{green:02x}{blue:02x}"
        for red, green, blue in locked_style.character_palette()
    ]
    assert seen[0] == expected
    assert seen[1] == []


# --------------------------------------------------------------------------
# Pose and starting image at the server boundary (issue #69)
# --------------------------------------------------------------------------


async def _approved_sprite(client, *, game, feature, kind, prompt):
    created = await client.call_tool(
        "generate_2d_sprite",
        {"featureId": feature, "prompt": prompt, "assetKind": kind, "gameId": game},
    )
    assert created.is_error is False, created.content
    await client.call_tool(
        "review_asset", {"assetId": created.structured_content["assetId"], "approved": True}
    )
    return created.structured_content["assetId"]


async def test_a_pose_reference_reaches_bitforge_as_coordinates(monkeypatch, tmp_path):
    """The one control that states a pose outright instead of describing it."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}

    def _fake_prototype(**kwargs):
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    def _fake_skeleton(image):
        captured["skeleton_source_size"] = image.size
        return (
            [{"x": 0.5, "y": 0.25, "label": "NOSE", "z_index": 0}],
            {"type": "generations", "generations": 1.0},
        )

    def _fake_bitforge(**kwargs):
        captured.update(kwargs)
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (7, 7, 7, 255)),
            {"type": "generations", "generations": 1.0},
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)
    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _fake_skeleton)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _fake_bitforge)

    from mcp import Client

    async with Client(server.mcp) as client:
        anchor = await _approved_sprite(
            client, game="t-pose", feature="f-anchor", kind="prop", prompt="a rock"
        )
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-posed",
                "prompt": "a mossy rock",
                "assetKind": "prop",
                "gameId": "t-pose",
                "poseFromAssetId": anchor,
                "skeletonGuidance": 3.0,
            },
        )

    body = result.structured_content
    assert result.is_error is False
    # Estimated on the stored 128x128 sprite and sent unchanged for a 32x32
    # canvas: the coordinates are normalised to 0-1, so the reference's own
    # size is irrelevant. Scaling them by the size ratio put every joint in the
    # top-left corner and the generation came back as noise (measured).
    assert captured["skeleton_source_size"] == (128, 128)
    assert captured["skeleton_keypoints"] == [
        {"x": 0.5, "y": 0.25, "label": "NOSE", "z_index": 0}
    ]
    assert captured["skeleton_guidance_scale"] == 3.0
    assert body["skeletonKeypoints"] == 1
    # 32x32 prop: a friendly keypoint canvas, so nothing to warn about.
    assert "warnings" not in body
    # The skeleton call is billed separately from the generation.
    assert body["imagesGenerated"] == 2


async def test_a_posed_character_is_grown_to_a_square_canvas(monkeypatch, tmp_path):
    """Every keypoint-friendly canvas is square and a character is 1:2.

    Rather than run on a canvas the provider warns about — measured to return
    noise — the request is grown to the smallest square that still contains it.
    A character's 64 rows are why 48 was rejected in the first place, and
    64x64 keeps every one of them; only the width changes.
    """

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_prototype(**kwargs):
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    def _fake_skeleton(image):
        return [{"x": 1.0, "y": 2.0, "label": "NECK", "z_index": 0}], {}

    captured_size: dict[str, tuple[int, int]] = {}

    def _fake_bitforge(**kwargs):
        captured_size["size"] = (kwargs["width"], kwargs["height"])
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (7, 7, 7, 255)),
            {"type": "generations", "generations": 1.0},
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)
    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _fake_skeleton)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _fake_bitforge)

    from mcp import Client

    async with Client(server.mcp) as client:
        anchor = await _approved_sprite(
            client,
            game="t-pose-char",
            feature="f-anchor",
            kind="character",
            prompt="a knight",
        )
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-posed-char",
                "prompt": "a mage",
                "assetKind": "character",
                "gameId": "t-pose-char",
                "poseFromAssetId": anchor,
            },
        )

    body = result.structured_content
    assert result.is_error is False
    assert body["requestedCanvas"] == [32, 64]
    assert body["canvas"] == [64, 64]
    assert any("64x64" in warning for warning in body["warnings"])
    # The generated canvas is the square one, and the stored sprite follows it.
    assert captured_size["size"] == (64, 64)
    with Image.open(body["assetPath"]) as saved:
        assert saved.size == (64 * server._PIXELLAB_UPSCALE,) * 2


async def test_an_unapproved_reference_is_refused_before_any_call(monkeypatch, tmp_path):
    """A reference becomes part of the next asset, so an unreviewed one would
    launder an unapproved image into approved work."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_prototype(**kwargs):
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    def _unexpected(*a, **k):
        raise AssertionError("nothing should be spent on a rejected reference")

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)
    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _unexpected)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _unexpected)

    from mcp import Client

    async with Client(server.mcp) as client:
        pending = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-unapproved",
                "prompt": "a rock",
                "assetKind": "prop",
                "gameId": "t-unapproved",
            },
        )
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-uses-it",
                "prompt": "a mossy rock",
                "assetKind": "prop",
                "gameId": "t-unapproved",
                "poseFromAssetId": pending.structured_content["assetId"],
            },
        )
        foreign = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-foreign",
                "prompt": "a mossy rock",
                "assetKind": "prop",
                "gameId": "t-unapproved",
                "initAssetId": "other-game__f__prop__abcd1234",
            },
        )

    for refused, expected in ((result, "approved"), (foreign, "belong to gameId")):
        assert refused.is_error is True
        assert expected in "".join(getattr(b, "text", "") for b in refused.content)


async def test_a_canvas_too_large_for_bitforge_is_refused_not_silently_posed(
    monkeypatch, tmp_path
):
    """Generating without the pose that was asked for is the worst outcome of
    the three, so the request fails instead."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_prototype(**kwargs):
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    def _unexpected(*a, **k):
        raise AssertionError("nothing should be spent on an impossible request")

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)
    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _unexpected)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _unexpected)

    from mcp import Client

    async with Client(server.mcp) as client:
        anchor = await _approved_sprite(
            client, game="t-too-big", feature="f-anchor", kind="prop", prompt="a rock"
        )
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-huge",
                "prompt": "a mossy rock",
                "assetKind": "prop",
                "gameId": "t-too-big",
                "gridSize": 240,
                "poseFromAssetId": anchor,
            },
        )

    assert result.is_error is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "200px per side" in text
    assert "240x240" in text



def test_the_posable_canvas_rule_is_about_canvases_not_kinds():
    """Stated as a rule about canvases so it holds for any kind, including ones
    this repository has not defined yet."""

    from asset.server import _posable_canvas

    # Already square and friendly: nothing to do, for every such kind.
    assert _posable_canvas(32, 32) == (32, 32)
    assert _posable_canvas(64, 64) == (64, 64)
    # Grown to the smallest square that still contains the request, so every
    # row the original canvas had survives.
    assert _posable_canvas(32, 64) == (64, 64)
    assert _posable_canvas(16, 32) == (32, 32)
    # Past the largest friendly square there is nothing to grow to.
    assert _posable_canvas(96, 64) is None
    assert _posable_canvas(64, 128) is None


def test_growing_the_canvas_never_drops_a_row():
    """The 1:2 ratio exists because 48 rows cropped a humanoid below the thigh.
    A rule that shrank the canvas would bring that back."""

    from asset.server import _posable_canvas

    for width, height in ((32, 64), (16, 32), (32, 32), (64, 64)):
        grown = _posable_canvas(width, height)
        assert grown is not None
        assert grown[0] >= width and grown[1] >= height


async def test_a_square_kind_is_not_resized_when_posed(monkeypatch, tmp_path):
    """A no-op for the kinds that were already posable, so nothing that worked
    before changes shape."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured: dict[str, Any] = {}

    def _fake_prototype(**kwargs):
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    def _fake_skeleton(image):
        return [{"x": 0.5, "y": 0.5, "label": "NECK", "z_index": 0}], {}

    def _fake_bitforge(**kwargs):
        captured.update(kwargs)
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (7, 7, 7, 255)),
            {"type": "generations", "generations": 1.0},
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)
    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _fake_skeleton)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _fake_bitforge)

    from mcp import Client

    async with Client(server.mcp) as client:
        anchor = await _approved_sprite(
            client, game="t-square-kind", feature="f-anchor", kind="prop", prompt="a rock"
        )
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-posed-prop",
                "prompt": "a mossy rock",
                "assetKind": "prop",
                "gameId": "t-square-kind",
                "poseFromAssetId": anchor,
            },
        )

    body = result.structured_content
    assert result.is_error is False
    assert (captured["width"], captured["height"]) == (32, 32)
    assert "canvas" not in body
    assert "warnings" not in body


async def test_an_init_only_request_is_grown_too(monkeypatch, tmp_path):
    """Keypoints were the reason to look at the canvas, but the control showed
    the canvas is the problem by itself: bitforge with no keypoints at all
    still came back broken at 32x64. So every bitforge request is grown, not
    only the posed ones."""

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured: dict[str, Any] = {}

    def _fake_prototype(**kwargs):
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    def _unexpected_skeleton(image):
        raise AssertionError("no pose was requested, so no skeleton call")

    def _fake_bitforge(**kwargs):
        captured.update(kwargs)
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (7, 7, 7, 255)),
            {"type": "generations", "generations": 1.0},
        )

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)
    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _unexpected_skeleton)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _fake_bitforge)

    from mcp import Client

    async with Client(server.mcp) as client:
        anchor = await _approved_sprite(
            client,
            game="t-init-grow",
            feature="f-anchor",
            kind="character",
            prompt="a knight",
        )
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-init-grow",
                "prompt": "a mage",
                "assetKind": "character",
                "gameId": "t-init-grow",
                "initAssetId": anchor,
            },
        )

    body = result.structured_content
    assert result.is_error is False
    assert (captured["width"], captured["height"]) == (64, 64)
    assert body["requestedCanvas"] == [32, 64]
    assert body["canvas"] == [64, 64]
    assert "skeletonKeypoints" not in body


async def test_a_concept_art_reference_reaches_bitforge_as_init_image(monkeypatch, tmp_path):
    """Concept art is the drawing the asset is meant to match, so it reaches the
    generator as pixels instead of as prose."""

    monkeypatch.setattr(server, "ROOT", tmp_path / "assets")
    monkeypatch.setattr(server, "CONCEPT_ART_ROOT", tmp_path / "concept-art")
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured: dict[str, object] = {}

    concept_dir = tmp_path / "concept-art" / "t-concept"
    concept_dir.mkdir(parents=True)
    Image.new("RGBA", (66, 161), (200, 100, 40, 255)).save(concept_dir / "hero.png")

    def _unexpected_skeleton(image):
        raise AssertionError("no pose was requested, so no skeleton call")

    def _fake_bitforge(**kwargs):
        captured.update(kwargs)
        return (
            Image.new("RGBA", (kwargs["width"], kwargs["height"]), (7, 7, 7, 255)),
            {"type": "generations", "generations": 1.0},
        )

    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _unexpected_skeleton)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _fake_bitforge)

    from mcp import Client

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-concept",
                "prompt": "the protagonist of the concept art",
                "assetKind": "character",
                "gameId": "t-concept",
                "initAssetId": "concept:hero.png",
            },
        )

    assert result.is_error is False, result.content
    assert captured["init_image"].size == (66, 161)
    assert result.structured_content["status"] == "pending"


async def test_a_concept_reference_outside_its_game_directory_is_refused(monkeypatch, tmp_path):
    """The directory is the whole gate, so a name that escapes it is refused
    rather than clamped — and nothing is billed."""

    monkeypatch.setattr(server, "ROOT", tmp_path / "assets")
    monkeypatch.setattr(server, "CONCEPT_ART_ROOT", tmp_path / "concept-art")
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    (tmp_path / "concept-art" / "t-escape").mkdir(parents=True)
    other = tmp_path / "concept-art" / "other-game"
    other.mkdir(parents=True)
    Image.new("RGBA", (32, 32), (1, 2, 3, 255)).save(other / "secret.png")

    def _unexpected(*args, **kwargs):
        raise AssertionError("nothing should be spent on a refused reference")

    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _unexpected)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _unexpected)

    from mcp import Client

    async with Client(server.mcp) as client:
        escaped = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-escape",
                "prompt": "a mage",
                "assetKind": "character",
                "gameId": "t-escape",
                "initAssetId": "concept:../other-game/secret.png",
            },
        )
        missing = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-missing",
                "prompt": "a mage",
                "assetKind": "character",
                "gameId": "t-escape",
                "poseFromAssetId": "concept:absent.png",
            },
        )

    for refused, expected in ((escaped, "must stay within"), (missing, "unknown")):
        assert refused.is_error is True
        assert expected in "".join(getattr(b, "text", "") for b in refused.content)


async def test_an_asset_id_reference_still_needs_approval_and_pixellab(monkeypatch, tmp_path):
    """The concept path is an addition, not a hole: an asset id is gated exactly
    as before."""

    monkeypatch.setattr(server, "ROOT", tmp_path / "assets")
    monkeypatch.setattr(server, "CONCEPT_ART_ROOT", tmp_path / "concept-art")
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_prototype(**kwargs):
        image = Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255))
        return image, {"type": "generations", "generations": 1.0}, "create_image"

    def _unexpected(*args, **kwargs):
        raise AssertionError("nothing should be spent on a rejected reference")

    monkeypatch.setattr(pixellab_client, "generate_prototype", _fake_prototype)
    monkeypatch.setattr(pixellab_client, "estimate_skeleton", _unexpected)
    monkeypatch.setattr(pixellab_client, "create_image_bitforge", _unexpected)

    from mcp import Client

    async with Client(server.mcp) as client:
        pending = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-still-gated",
                "prompt": "a rock",
                "assetKind": "prop",
                "gameId": "t-still-gated",
            },
        )
        refused = await client.call_tool(
            "generate_2d_sprite",
            {
                "featureId": "f-uses-it",
                "prompt": "a mossy rock",
                "assetKind": "prop",
                "gameId": "t-still-gated",
                "initAssetId": pending.structured_content["assetId"],
            },
        )

    assert refused.is_error is True
    assert "approved" in "".join(getattr(b, "text", "") for b in refused.content)
