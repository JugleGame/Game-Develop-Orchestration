"""Tests for the PixelLab routing wired into asset/server.py::_generate_image.

There is no fallback tier: a missing key or a failing call is a §03 code-3000
tool error, not a silent degrade to placeholder art.
"""

import random

import pytest
from PIL import Image

from asset import pixellab_client, server
from asset.server import _generate_image, _pixellab_palette, _pixellab_style_params, _size_for
from asset.style import derive


@pytest.fixture
def style():
    return derive("t-pixellab-branch", "pixel art")


@pytest.fixture
def rng():
    return random.Random(1234)


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
# imagesGenerated surfaces through the §03 tool result
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

    monkeypatch.setattr(pixellab_client, "generate_image", _fake_generate)

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        await client.initialize()
        result = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-1", "prompt": "a rock", "gameId": "t-pixellab-images"},
        )

    assert result.isError is False
    assert result.structuredContent["generatedBy"] == "pixellab"
    assert result.structuredContent["imagesGenerated"] == 1
    assert "costUsd" not in result.structuredContent


async def test_generate_2d_sprite_without_key_is_a_tool_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.delenv("PIXELLAB_API_KEY", raising=False)

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server.mcp) as client:
        await client.initialize()
        result = await client.call_tool(
            "generate_2d_sprite",
            {"featureId": "f-1", "prompt": "a rock", "gameId": "t-pixellab-nokey"},
        )

    assert result.isError is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 3000' in text
