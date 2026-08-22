"""Tests for the PixelLab routing wired into asset/server.py::_generate_image.

There is no fallback tier: a missing key or a failing call is an MCP code-3000
tool error, not a silent degrade to placeholder art.
"""

import random

import pytest
from PIL import Image

from asset import pixellab_client, prompting, server
from asset.server import _generate_image, _pixellab_palette, _pixellab_style_params, _size_for
from asset.style import derive


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
    assert plan.composed_characters <= plan.original_characters
    assert plan.removed_structured_clauses


def test_prompt_composition_does_not_repeat_a_complete_prepared_brief():
    prompt = (
        "a brass lantern. Composition: centered with a broad base and narrow top handle. "
        "Required visual structure: one connected silhouette; three support feet. "
        "Readability target: a 32 px pickup. Exclude: text."
    )

    plan = prompting.compose(prompt, "prop")

    assert "single centered isolated object" not in plan.prompt
    assert plan.composed_characters <= plan.original_characters


# --------------------------------------------------------------------------
# No key configured -> hard error, no generation
# --------------------------------------------------------------------------


def test_no_api_key_raises_error(style, rng, monkeypatch):
    monkeypatch.delenv("PIXELLAB_API_KEY", raising=False)

    with pytest.raises(Exception) as excinfo:
        _generate_image(style, "prop", rng, "a rock", "f-1")

    assert '"errorCode": 3000' in str(excinfo.value)


# --------------------------------------------------------------------------
# Key configured, call succeeds -> PixelLab path
# --------------------------------------------------------------------------


def test_configured_and_successful_uses_pixellab(style, rng, monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    captured = {}

    def _fake_generate(**kwargs):
        captured.update(kwargs)
        return Image.new("RGBA", (kwargs["width"], kwargs["height"]), (5, 5, 5, 255)), {
            "type": "usd",
            "usd": 0.004,
        }

    monkeypatch.setattr(pixellab_client, "generate_image", _fake_generate)

    image, provenance = _generate_image(style, "prop", rng, "a rock", "f-1")

    # "prop" is a 1:1 kind, so native size == pixel_grid on both axes.
    assert isinstance(image, Image.Image)
    assert image.size == (style.pixel_grid * server._PIXELLAB_UPSCALE,) * 2
    assert provenance["method"] == "pixellab"
    assert provenance["usage"] == {"type": "usd", "usd": 0.004}
    assert captured["prompt"] == "a rock"
    assert captured["width"] == style.pixel_grid
    assert captured["height"] == style.pixel_grid
    assert isinstance(captured["seed"], int)
    assert captured["forced_palette"] == _pixellab_palette(style, "prop", "a rock")
    assert captured["outline"] == "single color black outline"
    assert captured["shading"] == "flat shading"
    assert captured["detail"] == "medium detail"
    assert captured["view"] == style.camera_view


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


def test_pixellab_palette_is_unforced_for_living_things(style):
    """Measured (prompt-eval 2026-08-02, rounds 7-9): the locked game ramp
    read as "too green, no character" on a humanoid (5/10). Dropping the
    forced palette scored no worse (7/10) while letting the subject show its
    own colours — living things keep their tone from the ``medium shading``
    style param instead."""

    assert _pixellab_palette(style, "character", "a hero") is None
    assert _pixellab_palette(style, "monster", "a wild slime") is None


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


def test_configured_but_failing_raises_error(style, rng, monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _boom(**kwargs):
        raise pixellab_client.PixelLabUnavailable("quota exceeded")

    monkeypatch.setattr(pixellab_client, "generate_image", _boom)

    with pytest.raises(Exception) as excinfo:
        _generate_image(style, "prop", rng, "a rock", "f-1")

    assert '"errorCode": 3000' in str(excinfo.value)


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
            {"featureId": "f-1", "prompt": "a rock", "gameId": "t-pixellab-images"},
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
            {"featureId": "f-1", "prompt": "a rock", "gameId": "t-pixellab-nokey"},
        )

    assert result.is_error is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 3000' in text


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


def test_size_below_pixellabs_floor_is_rejected_before_the_call(style):
    from asset.server import _resolve_size

    with pytest.raises(Exception) as excinfo:
        _resolve_size(style, "character", 16, "f-1")

    message = str(excinfo.value)
    assert '"errorCode": 1000' in message
    assert "16x32" in message
    assert "32-400" in message


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
