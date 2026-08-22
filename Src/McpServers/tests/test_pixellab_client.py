"""Tests for asset/pixellab_client.py — the PixelLab HTTP call in isolation.

No real network access: every test mocks ``httpx.post``. Schema fields match
what 12문서 §3 confirmed against PixelLab's own openapi.json (2026-08-02).
"""

import base64
import io
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from asset import pixellab_client


def _png_bytes(size=(4, 4), color=(10, 20, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._json_body

    @property
    def text(self):
        return str(self._json_body)


# --------------------------------------------------------------------------
# is_configured
# --------------------------------------------------------------------------


def test_is_configured_reflects_env_var(monkeypatch):
    monkeypatch.delenv("PIXELLAB_API_KEY", raising=False)
    assert pixellab_client.is_configured() is False

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    assert pixellab_client.is_configured() is True


# --------------------------------------------------------------------------
# generate_image — missing key
# --------------------------------------------------------------------------


def test_generate_image_without_key_raises_unavailable(monkeypatch):
    monkeypatch.delenv("PIXELLAB_API_KEY", raising=False)

    with pytest.raises(pixellab_client.PixelLabUnavailable):
        pixellab_client.generate_image(prompt="a rock", width=32, height=32)


# --------------------------------------------------------------------------
# generate_image — success path
# --------------------------------------------------------------------------


def test_generate_image_success_returns_image_and_usage(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    image_b64 = base64.b64encode(_png_bytes()).decode()
    body = {"image": {"type": "base64", "base64": image_b64}, "usage": {"type": "usd", "usd": 0.004}}

    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(body)

    monkeypatch.setattr(httpx, "post", _fake_post)

    image, usage = pixellab_client.generate_image(prompt="a rock", width=32, height=32, seed=7)

    assert isinstance(image, Image.Image)
    assert image.mode == "RGBA"
    assert usage == {"type": "usd", "usd": 0.004}
    assert captured["url"] == "https://api.pixellab.ai/v2/create-image-pixflux"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["description"] == "a rock"
    assert captured["json"]["image_size"] == {"width": 32, "height": 32}
    assert captured["json"]["seed"] == 7
    assert captured["json"]["no_background"] is True


def test_generate_image_forced_palette_sends_color_image(monkeypatch):
    """``CreateImagePixfluxRequest`` has no array-of-colours field (verified
    against the live ``v2/openapi.json``, 2026-08-02) — only ``color_image``,
    a ``Base64Image`` PixelLab samples colours from. This pins the payload
    shape so a future edit can't silently regress back to the wrong
    (rejected-by-the-API) ``forced_palette`` array shape."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    image_b64 = base64.b64encode(_png_bytes()).decode()
    body = {"image": {"base64": image_b64}, "usage": {"type": "usd", "usd": 0.002}}
    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["json"] = json
        return _FakeResponse(body)

    monkeypatch.setattr(httpx, "post", _fake_post)

    pixellab_client.generate_image(
        prompt="a rock",
        width=32,
        height=32,
        forced_palette=[(10, 20, 30), (200, 210, 220)],
    )

    color_image = captured["json"]["color_image"]
    assert color_image["type"] == "base64"
    assert color_image["format"] == "png"
    swatch = Image.open(io.BytesIO(base64.b64decode(color_image["base64"])))
    assert swatch.size == (2, 1)
    assert swatch.convert("RGB").getpixel((0, 0)) == (10, 20, 30)
    assert swatch.convert("RGB").getpixel((1, 0)) == (200, 210, 220)


def test_generate_image_without_palette_omits_color_image(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    image_b64 = base64.b64encode(_png_bytes()).decode()
    body = {"image": {"base64": image_b64}, "usage": {"type": "usd", "usd": 0.002}}
    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["json"] = json
        return _FakeResponse(body)

    monkeypatch.setattr(httpx, "post", _fake_post)

    pixellab_client.generate_image(prompt="a rock", width=32, height=32)

    assert "color_image" not in captured["json"]


def test_generate_image_without_seed_omits_it_from_payload(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    image_b64 = base64.b64encode(_png_bytes()).decode()
    body = {"image": {"base64": image_b64}, "usage": {"type": "usd", "usd": 0.002}}
    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["json"] = json
        return _FakeResponse(body)

    monkeypatch.setattr(httpx, "post", _fake_post)

    pixellab_client.generate_image(prompt="a rock", width=32, height=32)

    assert "seed" not in captured["json"]


# --------------------------------------------------------------------------
# generate_image — failure modes must all raise PixelLabUnavailable
# --------------------------------------------------------------------------


def test_generate_image_http_error_raises_unavailable(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_post(url, json, headers, timeout):
        return _FakeResponse({"detail": "quota exceeded"}, status_code=422)

    monkeypatch.setattr(httpx, "post", _fake_post)

    with pytest.raises(pixellab_client.PixelLabUnavailable):
        pixellab_client.generate_image(prompt="a rock", width=32, height=32)


def test_generate_image_network_error_raises_unavailable(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_post(url, json, headers, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _fake_post)

    with pytest.raises(pixellab_client.PixelLabUnavailable):
        pixellab_client.generate_image(prompt="a rock", width=32, height=32)


def test_generate_image_malformed_response_raises_unavailable(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_post(url, json, headers, timeout):
        return _FakeResponse({"unexpected": "shape"})

    monkeypatch.setattr(httpx, "post", _fake_post)

    with pytest.raises(pixellab_client.PixelLabUnavailable):
        pixellab_client.generate_image(prompt="a rock", width=32, height=32)


def test_generate_image_bad_base64_raises_unavailable(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    def _fake_post(url, json, headers, timeout):
        return _FakeResponse({"image": {"base64": "not-valid-base64!!"}, "usage": {}})

    monkeypatch.setattr(httpx, "post", _fake_post)

    with pytest.raises(pixellab_client.PixelLabUnavailable):
        pixellab_client.generate_image(prompt="a rock", width=32, height=32)


def test_official_mcp_prototype_arguments_keep_style_structured():
    tool = SimpleNamespace(
        name="create_image",
        input_schema={
            "properties": {
                "description": {},
                "image_size": {},
                "seed": {},
                "no_background": {},
                "text_guidance_scale": {},
                "outline": {},
                "view": {},
                "style_description": {},
                "forced_palette": {},
            },
            "required": ["description", "image_size"],
        },
    )

    arguments = pixellab_client._prototype_arguments(
        tool,
        "a mossy rock; single centered isolated object",
        32,
        32,
        7,
        "dark fantasy pixel art",
        {"outline": "single color black outline", "view": "side"},
        ["#112233", "#445566"],
        12.0,
    )

    assert arguments["description"] == "a mossy rock; single centered isolated object"
    assert arguments["image_size"] == {"width": 32, "height": 32}
    assert arguments["text_guidance_scale"] == 12.0
    assert arguments["style_description"] == "dark fantasy pixel art"
    assert arguments["outline"] == "single color black outline"
    assert arguments["forced_palette"] == ["#112233", "#445566"]


def test_official_mcp_prototype_arguments_preserve_style_when_not_structured():
    tool = SimpleNamespace(
        name="create_image_pixflux",
        input_schema={"properties": {"description": {}}, "required": ["description"]},
    )

    arguments = pixellab_client._prototype_arguments(
        tool,
        "a brass lantern",
        32,
        32,
        7,
        "industrial fantasy pixel art",
        {},
        [],
        8.0,
    )

    assert arguments["description"] == (
        "a brass lantern; art style: industrial fantasy pixel art"
    )


def test_official_mcp_prefers_pixflux_for_single_image_prototypes():
    tools = [
        SimpleNamespace(name="create_character", input_schema={"properties": {}}),
        SimpleNamespace(name="create_image_pixen", input_schema={"properties": {}}),
        SimpleNamespace(name="create_image_pixflux", input_schema={"properties": {}}),
    ]

    assert pixellab_client._prototype_tool(tools, "character").name == "create_image_pixflux"


def test_official_mcp_result_helpers_read_async_job_and_image():
    job_id = "123e4567-e89b-12d3-a456-426614174000"
    queued = SimpleNamespace(
        structured_content=None,
        content=[SimpleNamespace(type="text", text=f'{{"job_id": "{job_id}"}}')],
    )
    completed = SimpleNamespace(
        structured_content=None,
        content=[SimpleNamespace(type="image", data=base64.b64encode(_png_bytes()).decode())],
    )

    payloads = pixellab_client._result_payloads(queued)
    assert pixellab_client._identifier(payloads, ("job_id",)) == job_id
    encoded, url = pixellab_client._result_image(completed)
    assert encoded is not None
    assert url is None


def test_generate_with_style_uses_reference_and_returns_all_candidates(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    encoded = base64.b64encode(_png_bytes(size=(64, 64))).decode()
    captured = {}

    class _FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResponse({"background_job_id": "job-1", "usage": {}} , 202)

        def get(self, url, headers):
            return _FakeResponse(
                {
                    "status": "completed",
                    "last_response": {
                        "images": [{"base64": encoded}, {"image": {"base64": encoded}}]
                    },
                    "usage": {"type": "generations", "generations": 2.0},
                }
            )

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    images, usage, job_id = pixellab_client.generate_with_style(
        prompt="a red treasure chest",
        style_images=[
            Image.new("RGBA", (64, 64), (1, 2, 3, 255)),
            Image.new("RGBA", (128, 256), (4, 5, 6, 255)),
        ],
        style_description="dark fantasy pixel art",
        seed=9,
        poll_seconds=0,
    )

    assert captured["url"] == "https://api.pixellab.ai/v2/generate-with-style-v2"
    assert captured["payload"]["description"] == "a red treasure chest"
    assert captured["payload"]["style_images"][0]["width"] == 64
    assert captured["payload"]["style_images"][1]["width"] == 128
    assert captured["payload"]["style_images"][1]["height"] == 256
    assert len(captured["payload"]["style_images"]) == 2
    assert "image_size" not in captured["payload"]
    assert len(images) == 2
    assert usage["generations"] == 2.0
    assert job_id == "job-1"


def test_generate_with_style_accepts_a_non_square_reference(monkeypatch):
    """``image_size`` is REMOVED from this endpoint's schema.

    The output size is deduced from the style images, so a 1:2 character
    reference is an ordinary request and the returned size is whatever came
    back — not something to check against a size that was never sent.
    """

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    encoded = base64.b64encode(_png_bytes(size=(64, 128))).decode()
    captured = {}

    class _FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            captured["payload"] = json
            return _FakeResponse({"background_job_id": "job-2"}, 202)

        def get(self, url, headers):
            return _FakeResponse(
                {"status": "completed", "last_response": {"images": [{"base64": encoded}]}}
            )

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    images, _usage, _job = pixellab_client.generate_with_style(
        prompt="a tall hero",
        style_images=[Image.new("RGBA", (64, 128), (1, 2, 3, 255))],
        style_description="pixel art",
        seed=9,
        poll_seconds=0,
    )

    assert "image_size" not in captured["payload"]
    assert images[0].size == (64, 128)


# --------------------------------------------------------------------------
# create_animation (Issue #27)
# --------------------------------------------------------------------------


def _animation_client(monkeypatch, captured, statuses, encoded):
    """Fake httpx client whose job status walks through ``statuses``."""

    class _FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResponse({"background_job_id": "job-anim"}, 200)

        def get(self, url, headers):
            captured.setdefault("polls", 0)
            captured["polls"] += 1
            status = statuses[min(captured["polls"] - 1, len(statuses) - 1)]
            if status != "completed":
                return _FakeResponse({"status": status})
            return _FakeResponse(
                {
                    "status": "completed",
                    "last_response": {"images": [{"base64": encoded}, {"base64": encoded}]},
                    "usage": {"type": "generations", "generations": 2.0},
                }
            )

    monkeypatch.setattr(httpx, "Client", _FakeClient)


def test_create_animation_posts_the_first_frame_and_returns_ordered_frames(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    encoded = base64.b64encode(_png_bytes(size=(64, 64))).decode()
    captured: dict = {}
    _animation_client(monkeypatch, captured, ["processing", "completed"], encoded)

    frames, usage, job_id = pixellab_client.create_animation(
        first_frame=Image.new("RGBA", (64, 64), (1, 2, 3, 255)),
        action="walk cycle",
        frame_count=4,
        poll_seconds=0,
    )

    assert captured["url"] == "https://api.pixellab.ai/v2/animate-with-text-v3"
    assert captured["payload"]["action"] == "walk cycle"
    assert captured["payload"]["frame_count"] == 4
    assert captured["payload"]["first_frame"]["type"] == "base64"
    # Without this the provider paints every frame onto an opaque plate.
    assert captured["payload"]["no_background"] is True
    assert "description" not in captured["payload"]
    assert len(frames) == 2
    assert usage["generations"] == 2.0
    assert job_id == "job-anim"


def test_create_animation_reports_a_failed_job(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    encoded = base64.b64encode(_png_bytes()).decode()
    captured: dict = {}
    _animation_client(monkeypatch, captured, ["failed"], encoded)

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="animation job failed"):
        pixellab_client.create_animation(
            first_frame=Image.new("RGBA", (32, 32), (1, 2, 3, 255)),
            action="walk cycle",
            frame_count=4,
            poll_seconds=0,
        )


def test_create_animation_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    encoded = base64.b64encode(_png_bytes()).decode()
    captured: dict = {}
    _animation_client(monkeypatch, captured, ["processing"], encoded)

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="not ready after"):
        pixellab_client.create_animation(
            first_frame=Image.new("RGBA", (32, 32), (1, 2, 3, 255)),
            action="walk cycle",
            frame_count=4,
            poll_seconds=0,
            max_polls=3,
        )


@pytest.mark.parametrize("frame_count", [1, 5, 18])
def test_create_animation_rejects_a_frame_count_the_provider_refuses(monkeypatch, frame_count):
    """PixelLab answers 422 for odd counts; spend nothing to learn that."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="frame_count"):
        pixellab_client.create_animation(
            first_frame=Image.new("RGBA", (32, 32), (1, 2, 3, 255)),
            action="walk cycle",
            frame_count=frame_count,
        )


# --------------------------------------------------------------------------
# TaskGroup failures must name the error, not the group (#58)
# --------------------------------------------------------------------------


def test_flatten_exception_spells_out_nested_group_leaves():
    inner = ExceptionGroup("inner", [ValueError("image size must be >= 32")])
    outer = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

    flattened = pixellab_client._flatten_exception(outer)

    assert flattened == "ValueError: image size must be >= 32"
    assert "TaskGroup" not in flattened


def test_flatten_exception_keeps_a_plain_exception_readable():
    assert pixellab_client._flatten_exception(RuntimeError("boom")) == "RuntimeError: boom"


def test_pixellab_unavailable_defaults_to_not_billable():
    assert pixellab_client.PixelLabUnavailable("nope").job_started is False
    assert pixellab_client.PixelLabUnavailable("nope", job_started=True).job_started is True


# --------------------------------------------------------------------------
# Structured style values are checked against the tool's own enum (#59)
# --------------------------------------------------------------------------


def _styled_tool(enum_values):
    return SimpleNamespace(
        name="create_image_pixflux",
        input_schema={
            "properties": {"description": {}, "detail": {"enum": list(enum_values)}},
            "required": ["description"],
        },
    )


def test_prototype_arguments_reject_a_detail_the_tool_does_not_offer():
    with pytest.raises(pixellab_client.PixelLabUnavailable) as excinfo:
        pixellab_client._prototype_arguments(
            _styled_tool(["low detail", "medium detail", "highly detailed"]),
            "a mossy rock",
            32,
            32,
            7,
            "pixel art",
            {"detail": "minimal detail"},
            [],
            8.0,
        )

    message = str(excinfo.value)
    assert "'minimal detail'" in message
    assert "medium detail" in message


def test_prototype_arguments_pass_a_detail_the_tool_offers():
    arguments = pixellab_client._prototype_arguments(
        _styled_tool(["low detail", "medium detail"]),
        "a mossy rock",
        32,
        32,
        7,
        "pixel art",
        {"detail": "low detail"},
        [],
        8.0,
    )

    assert arguments["detail"] == "low detail"


def test_prototype_arguments_still_pass_style_fields_without_a_declared_enum():
    tool = SimpleNamespace(
        name="create_image_pixflux",
        input_schema={"properties": {"description": {}, "detail": {}}},
    )

    arguments = pixellab_client._prototype_arguments(
        tool, "a mossy rock", 32, 32, 7, "pixel art", {"detail": "low detail"}, [], 8.0
    )

    assert arguments["detail"] == "low detail"


# --------------------------------------------------------------------------
# Endpoint style contracts (issue #63)
#
# These lock the values recorded from https://api.pixellab.ai/v2/openapi.json
# on 2026-08-22. They are offline copies on purpose: a test that fetched the
# live document would turn a provider outage into a red build, and would let a
# silent provider change pass as green instead of failing here.
# --------------------------------------------------------------------------

SPEC_OUTLINE = (
    "single color black outline",
    "single color outline",
    "selective outline",
    "lineless",
)
SPEC_SHADING = (
    "flat shading",
    "basic shading",
    "medium shading",
    "detailed shading",
    "highly detailed shading",
)
SPEC_DETAIL = ("low detail", "medium detail", "highly detailed")

SPEC_STYLE_ENUMS = {
    "create-image-pixflux": {
        "outline": SPEC_OUTLINE,
        "shading": SPEC_SHADING,
        "detail": SPEC_DETAIL,
        "view": ("side", "low top-down", "high top-down"),
        "direction": (
            "north",
            "north-east",
            "east",
            "south-east",
            "south",
            "south-west",
            "west",
            "north-west",
        ),
    },
    "tilesets": {
        "outline": SPEC_OUTLINE,
        "shading": SPEC_SHADING,
        "detail": SPEC_DETAIL,
        "view": ("low top-down", "high top-down"),
    },
    "map-objects": {
        "outline": ("single color outline", "selective outline", "lineless"),
        "shading": ("flat shading", "basic shading", "medium shading", "detailed shading"),
        "detail": ("low detail", "medium detail", "high detail"),
        "view": ("low top-down", "high top-down", "side"),
    },
}


def test_style_enums_match_the_recorded_schema():
    assert pixellab_client.STYLE_ENUMS == SPEC_STYLE_ENUMS


def _tileset_client(monkeypatch, captured):
    """POST /tilesets answers 202 with a tileset_id; GET returns the tiles."""

    encoded = base64.b64encode(_png_bytes()).decode()

    class _FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResponse({"tileset_id": "ts-1"}, 202)

        def get(self, url, headers):
            return _FakeResponse(
                {
                    "tileset": {
                        "tiles": [
                            {
                                "name": "wang_0",
                                "image": {"base64": encoded},
                                "corners": {"NW": "lower"},
                            }
                        ]
                    }
                }
            )

    monkeypatch.setattr(httpx, "Client", _FakeClient)


def test_create_tileset_accepts_the_values_the_schema_declares(monkeypatch):
    """The previous table refused every one of these."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _tileset_client(monkeypatch, captured)

    pixellab_client.create_tileset(
        lower_description="grass",
        upper_description="stone",
        tile_size=32,
        view="high top-down",
        outline="single color black outline",
        shading="flat shading",
        detail="low detail",
        poll_seconds=0,
    )

    payload = captured["payload"]
    assert payload["outline"] == "single color black outline"
    assert payload["shading"] == "flat shading"
    assert payload["detail"] == "low detail"


@pytest.mark.parametrize(
    "field,value",
    [
        ("outline", "thick outline"),
        ("outline", "none"),
        ("shading", "no shading"),
        ("shading", "heavy shading"),
        ("detail", "minimal detail"),
        ("view", "side"),
    ],
)
def test_create_tileset_rejects_values_the_schema_does_not_declare(
    monkeypatch, field, value
):
    """The previous table offered "thick outline", "no shading" and
    "minimal detail" as valid; none of the three exists in the schema."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable) as excinfo:
        pixellab_client.create_tileset(
            lower_description="grass",
            upper_description="stone",
            **{field: value},
        )

    message = str(excinfo.value)
    assert field in message
    assert "/tilesets" in message


@pytest.mark.parametrize("tile_size", [24, 48, 64])
def test_create_tileset_rejects_a_tile_size_outside_the_enum(monkeypatch, tile_size):
    """``TileSize`` is an enum: 24 and 48 sit inside 16-64 and still 422,
    and 64 additionally needs a "pro" mode this client never sends."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="tile_size"):
        pixellab_client.create_tileset(
            lower_description="grass", upper_description="stone", tile_size=tile_size
        )


def test_create_tileset_sends_a_palette_as_color_image(monkeypatch):
    """``CreateTilesetRequest`` has no ``forced_palette`` field."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _tileset_client(monkeypatch, captured)

    pixellab_client.create_tileset(
        lower_description="grass",
        upper_description="stone",
        color_palette=[(10, 20, 30), (40, 50, 60)],
        poll_seconds=0,
    )

    payload = captured["payload"]
    assert "forced_palette" not in payload
    assert payload["color_image"]["type"] == "base64"
    swatch = Image.open(io.BytesIO(base64.b64decode(payload["color_image"]["base64"])))
    assert swatch.size == (2, 1)


def _map_object_client(monkeypatch, captured):
    class _FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json, headers):
            captured["url"] = url
            captured["payload"] = json
            return _FakeResponse({"object_id": "mo-1"}, 202)

        def get(self, url, headers=None):
            if url.endswith("/map-objects/mo-1"):
                return _FakeResponse(
                    {"download_url": "https://example.invalid/mo-1.png", "usage": {}}
                )
            return SimpleNamespace(
                content=_png_bytes(), raise_for_status=lambda: None, status_code=200
            )

    monkeypatch.setattr(httpx, "Client", _FakeClient)


def test_create_map_object_sends_style_and_palette(monkeypatch):
    """Sending no style let the endpoint default ``view`` to "high top-down",
    so a side-view game had its decorations drawn from above. The palette also
    has to travel as ``color_image``: there is no ``color_palette`` field."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _map_object_client(monkeypatch, captured)

    pixellab_client.create_map_object(
        description="a mossy rock",
        width=64,
        height=64,
        color_palette=[(10, 20, 30)],
        view="side",
        outline="single color outline",
        shading="flat shading",
        detail="medium detail",
        poll_seconds=0,
    )

    payload = captured["payload"]
    assert "color_palette" not in payload
    assert payload["color_image"]["type"] == "base64"
    assert payload["view"] == "side"
    assert payload["outline"] == "single color outline"
    assert payload["text_guidance_scale"] == pixellab_client.DEFAULT_TEXT_GUIDANCE


@pytest.mark.parametrize(
    "field,value",
    [
        ("outline", "single color black outline"),
        ("shading", "highly detailed shading"),
        ("detail", "highly detailed"),
    ],
)
def test_create_map_object_rejects_pixflux_only_wordings(monkeypatch, field, value):
    """Valid for pixflux, invalid here. The difference is why the table is
    keyed by endpoint rather than shared."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable) as excinfo:
        pixellab_client.create_map_object(description="a mossy rock", **{field: value})

    assert "/map-objects" in str(excinfo.value)


def test_create_map_object_rejects_a_size_below_its_own_floor(monkeypatch):
    """``CreateMapObjectRequest.image_size`` starts at 32, not pixflux's 16."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="32-400"):
        pixellab_client.create_map_object(description="a mossy rock", width=16, height=16)


def test_generate_image_relays_text_guidance_scale(monkeypatch):
    """Both generation paths must follow the description equally literally.

    The MCP path hard-coded 16 while this one sent nothing and got PixelLab's
    own 8, so two assets in one game were generated at different strengths.
    """

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["payload"] = json
        return _FakeResponse(
            {"image": {"base64": base64.b64encode(_png_bytes()).decode()}, "usage": {}}
        )

    monkeypatch.setattr(httpx, "post", _fake_post)

    pixellab_client.generate_image(prompt="a rock", width=32, height=32)
    assert pixellab_client.DEFAULT_TEXT_GUIDANCE == 8.0
    assert captured["payload"]["text_guidance_scale"] == 8.0

    pixellab_client.generate_image(
        prompt="a rock", width=32, height=32, text_guidance_scale=12.0
    )
    assert captured["payload"]["text_guidance_scale"] == 12.0


@pytest.mark.parametrize("value", [0.5, 20.5])
def test_generate_image_rejects_text_guidance_outside_the_schema_range(
    monkeypatch, value
):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="1-20"):
        pixellab_client.generate_image(
            prompt="a rock", width=32, height=32, text_guidance_scale=value
        )


def test_generate_image_rejects_a_style_value_outside_the_schema(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable) as excinfo:
        pixellab_client.generate_image(
            prompt="a rock", width=32, height=32, detail="minimal detail"
        )

    assert "/create-image-pixflux" in str(excinfo.value)


# --------------------------------------------------------------------------
# create_image_bitforge (issue #64)
# --------------------------------------------------------------------------


def _bitforge_post(monkeypatch, captured, size=(32, 64)):
    def _fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResponse(
            {
                "image": {"base64": base64.b64encode(_png_bytes(size=size)).decode()},
                "usage": {"type": "generations", "generations": 1.0},
            }
        )

    monkeypatch.setattr(httpx, "post", _fake_post)


def test_bitforge_sends_the_style_reference_as_base64(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured)

    image, usage = pixellab_client.create_image_bitforge(
        prompt="a red treasure chest",
        width=32,
        height=64,
        style_image=Image.new("RGBA", (128, 256), (1, 2, 3, 255)),
        seed=9,
    )

    payload = captured["payload"]
    assert captured["url"].endswith("/create-image-bitforge")
    assert payload["style_image"]["type"] == "base64"
    # Resized to the requested canvas: the endpoint requires an exact match and
    # answers a mismatch with a 500, not a 422 (measured 2026-08-22:
    # "style_image must be size (64, 32), not torch.Size([256, 128])").
    reference = Image.open(io.BytesIO(base64.b64decode(payload["style_image"]["base64"])))
    assert reference.size == (32, 64)
    assert image.size == (32, 64)
    assert usage["generations"] == 1.0


def test_a_matching_aspect_reference_is_only_scaled(monkeypatch):
    """The ordinary case — a stored sprite is an upscaled copy of its own
    canvas — must stay an exact integer downscale with no padding."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured)

    source = Image.new("RGBA", (128, 256), (0, 0, 0, 0))
    source.paste(Image.new("RGBA", (128, 256), (1, 2, 3, 255)), (0, 0))

    pixellab_client.create_image_bitforge(
        prompt="a knight",
        width=32,
        height=64,
        init_image=source,
        seed=9,
    )

    reference = Image.open(
        io.BytesIO(base64.b64decode(captured["payload"]["init_image"]["base64"]))
    )
    assert reference.size == (32, 64)
    # Every pixel still carries the subject: nothing was padded away.
    assert reference.convert("RGBA").getchannel("A").getbbox() == (0, 0, 32, 64)


def test_a_taller_reference_is_padded_rather_than_squashed(monkeypatch):
    """Measured 2026-08-23: a 66x161 concept-art cutout stretched into 64x64
    generated two and then three overlapping figures, at init_image_strength
    900 and 600 alike. A squashed human reads as several humans."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured, size=(64, 64))

    pixellab_client.create_image_bitforge(
        prompt="a boy",
        width=64,
        height=64,
        init_image=Image.new("RGBA", (66, 161), (1, 2, 3, 255)),
        seed=9,
    )

    reference = Image.open(
        io.BytesIO(base64.b64decode(captured["payload"]["init_image"]["base64"]))
    )
    assert reference.size == (64, 64)
    left, top, right, bottom = reference.convert("RGBA").getchannel("A").getbbox()
    # Scaled by one factor, so the subject keeps its 66:161 proportions.
    assert (bottom - top) == 64
    assert (right - left) == round(66 * 64 / 161)
    # Bottom-centred: a side-view sprite stands on the bottom of its canvas.
    assert bottom == 64
    assert left == (64 - (right - left)) // 2


def test_a_wider_reference_is_padded_too(monkeypatch):
    """The rule is about disagreeing aspects, not about tall references."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured, size=(64, 64))

    pixellab_client.create_image_bitforge(
        prompt="a signboard",
        width=64,
        height=64,
        style_image=Image.new("RGBA", (128, 32), (1, 2, 3, 255)),
        seed=9,
    )

    reference = Image.open(
        io.BytesIO(base64.b64decode(captured["payload"]["style_image"]["base64"]))
    )
    assert reference.size == (64, 64)
    left, top, right, bottom = reference.convert("RGBA").getchannel("A").getbbox()
    assert (right - left) == 64
    assert (bottom - top) == 16
    assert bottom == 64


def test_bitforge_resizes_an_init_image_to_the_canvas_too(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured)

    pixellab_client.create_image_bitforge(
        prompt="a rock",
        width=32,
        height=32,
        init_image=Image.new("RGBA", (128, 128), (1, 2, 3, 255)),
    )

    sketch = Image.open(io.BytesIO(base64.b64decode(captured["payload"]["init_image"]["base64"])))
    assert sketch.size == (32, 32)


def test_bitforge_defaults_style_strength_to_the_documented_midpoint(monkeypatch):
    """The schema default is 0, which means "ignore the style image".

    Sending a reference and letting the provider default apply would silently
    do nothing, so a reference with no explicit strength gets the schema's own
    "50 = balanced" instead.
    """

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured)

    pixellab_client.create_image_bitforge(
        prompt="a red treasure chest",
        width=32,
        height=64,
        style_image=Image.new("RGBA", (32, 64), (1, 2, 3, 255)),
    )
    assert captured["payload"]["style_strength"] == 50
    assert pixellab_client.BITFORGE_BALANCED_STYLE_STRENGTH == 50

    pixellab_client.create_image_bitforge(
        prompt="a red treasure chest",
        width=32,
        height=64,
        style_image=Image.new("RGBA", (32, 64), (1, 2, 3, 255)),
        style_strength=70,
    )
    assert captured["payload"]["style_strength"] == 70


def test_bitforge_sends_negative_description_that_pixflux_drops(monkeypatch):
    """``negative_description`` is live here and ``(Deprecated)`` on pixflux."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured)

    pixellab_client.create_image_bitforge(
        prompt="a lone knight on a hill",
        width=32,
        height=64,
        negative_description="city, buildings",
    )
    assert captured["payload"]["negative_description"] == "city, buildings"

    pixellab_client.generate_image(prompt="a lone knight on a hill", width=32, height=64)
    assert "negative_description" not in captured["payload"]


def test_bitforge_sends_coverage_and_projection(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured)

    pixellab_client.create_image_bitforge(
        prompt="a wooden crate",
        width=32,
        height=32,
        coverage_percentage=85.0,
        oblique_projection=True,
        init_image=Image.new("RGBA", (32, 32), (9, 9, 9, 255)),
        init_image_strength=200,
    )

    payload = captured["payload"]
    assert payload["coverage_percentage"] == 85.0
    assert payload["oblique_projection"] is True
    assert payload["init_image"]["type"] == "base64"
    assert payload["init_image_strength"] == 200


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"width": 201, "height": 32}, "16-200px per side"),
        ({"width": 32, "height": 32, "style_strength": 101}, "style_strength"),
        ({"width": 32, "height": 32, "coverage_percentage": 120}, "coverage_percentage"),
        ({"width": 32, "height": 32, "init_image_strength": 1000}, "init_image_strength"),
    ],
)
def test_bitforge_rejects_values_outside_the_schema(monkeypatch, kwargs, message):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match=message):
        pixellab_client.create_image_bitforge(prompt="a rock", **kwargs)


def test_bitforge_side_range_is_half_of_pixflux(monkeypatch):
    """The trade for the extra controls: a large asset cannot use this path.

    The same 201px request is ordinary for pixflux, whose ceiling is 400.
    """

    assert pixellab_client.BITFORGE_SIDE_RANGE == (16, 200)

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["payload"] = json
        return _FakeResponse(
            {"image": {"base64": base64.b64encode(_png_bytes()).decode()}, "usage": {}}
        )

    monkeypatch.setattr(httpx, "post", _fake_post)

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="16-200px per side"):
        pixellab_client.create_image_bitforge(prompt="a rock", width=201, height=201)

    pixellab_client.generate_image(prompt="a rock", width=201, height=201)
    assert captured["payload"]["image_size"] == {"width": 201, "height": 201}


# --------------------------------------------------------------------------
# direction / isometric / tile terrains (issue #65)
# --------------------------------------------------------------------------


def test_generate_image_sends_direction_and_isometric(monkeypatch):
    """Both are real fields. Facing used to be expressible only as prose, and
    isometric was sent as the camera view "high top-down" — a different
    projection: top-down looks down a vertical axis, isometric a diagonal one.
    """

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["payload"] = json
        return _FakeResponse(
            {"image": {"base64": base64.b64encode(_png_bytes()).decode()}, "usage": {}}
        )

    monkeypatch.setattr(httpx, "post", _fake_post)

    pixellab_client.generate_image(
        prompt="a knight", width=32, height=64, direction="west", isometric=True
    )
    assert captured["payload"]["direction"] == "west"
    assert captured["payload"]["isometric"] is True

    pixellab_client.generate_image(prompt="a knight", width=32, height=64)
    assert "direction" not in captured["payload"]
    assert "isometric" not in captured["payload"]


def test_bitforge_sends_direction_too(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured)

    pixellab_client.create_image_bitforge(
        prompt="a knight", width=32, height=64, direction="south-east"
    )
    assert captured["payload"]["direction"] == "south-east"


@pytest.mark.parametrize("value", ["left", "East", "up", "sideways"])
def test_direction_outside_the_enum_is_rejected_before_the_call(monkeypatch, value):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable) as excinfo:
        pixellab_client.generate_image(
            prompt="a knight", width=32, height=64, direction=value
        )

    assert "direction" in str(excinfo.value)


def test_direction_is_not_a_field_on_every_endpoint():
    """``CreateTilesetRequest`` and ``CreateMapObjectRequest`` do not declare
    it, so passing one is a caller error rather than a wrong value."""

    assert "direction" not in pixellab_client.STYLE_ENUMS["tilesets"]
    assert "direction" not in pixellab_client.STYLE_ENUMS["map-objects"]

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="no 'direction' field"):
        pixellab_client._reject_style_enums("tilesets", direction="east")


def _tile_tool(*fields):
    return SimpleNamespace(
        name="create_isometric_tile",
        input_schema={
            "properties": {field: {} for field in fields},
            "required": [],
        },
    )


def test_tile_terrains_are_split_rather_than_duplicated():
    """A Wang tileset derives its boundary from the two terrains differing.
    Sending the same text as both left nothing to transition between."""

    arguments = pixellab_client._prototype_arguments(
        _tile_tool("lower", "upper"),
        "grass meadow | grey stone cliff",
        32,
        32,
        7,
        "pixel art",
        {},
        [],
        8.0,
    )

    assert arguments["lower"] == "grass meadow"
    assert arguments["upper"] == "grey stone cliff"


def test_a_tile_prompt_without_two_terrains_is_refused():
    with pytest.raises(pixellab_client.PixelLabUnavailable, match="two terrains"):
        pixellab_client._prototype_arguments(
            _tile_tool("lower", "upper"),
            "grass meadow",
            32,
            32,
            7,
            "pixel art",
            {},
            [],
            8.0,
        )


# --------------------------------------------------------------------------
# skeleton keypoints (issue #69)
# --------------------------------------------------------------------------


def _keypoints(*labels):
    return [
        {"x": float(i), "y": float(i * 2), "label": label, "z_index": 0.0}
        for i, label in enumerate(labels)
    ]


def test_estimate_skeleton_reads_joints_out_of_a_sprite(monkeypatch):
    """An approved sprite already stands the way the game wants, so its
    skeleton can be handed to the next asset instead of describing the pose."""

    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}

    def _fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResponse(
            {
                "keypoints": [
                    {"x": 8.0, "y": 4.0, "label": "NOSE", "z_index": 1.7},
                    {"x": 8.0, "y": 12.0, "label": "NECK", "z_index": 0.0},
                ],
                "usage": {"type": "generations", "generations": 1.0},
            }
        )

    monkeypatch.setattr(httpx, "post", _fake_post)

    keypoints, usage = pixellab_client.estimate_skeleton(
        Image.new("RGBA", (32, 64), (1, 2, 3, 255))
    )

    assert captured["url"].endswith("/estimate-skeleton")
    assert captured["payload"]["image"]["type"] == "base64"
    # The response says z_index is a float; the request's Point wants an int.
    assert keypoints[0] == {"x": 8.0, "y": 4.0, "label": "NOSE", "z_index": 1}
    assert usage["generations"] == 1.0


def test_bitforge_sends_keypoints_and_defaults_their_guidance(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")
    captured = {}
    _bitforge_post(monkeypatch, captured, size=(32, 32))

    pixellab_client.create_image_bitforge(
        prompt="a knight",
        width=32,
        height=32,
        skeleton_keypoints=_keypoints("NOSE", "NECK", "LEFT HIP"),
    )
    payload = captured["payload"]
    assert [point["label"] for point in payload["skeleton_keypoints"]] == [
        "NOSE",
        "NECK",
        "LEFT HIP",
    ]
    assert payload["skeleton_guidance_scale"] == pixellab_client.DEFAULT_SKELETON_GUIDANCE

    pixellab_client.create_image_bitforge(
        prompt="a knight",
        width=32,
        height=32,
        skeleton_keypoints=_keypoints("NOSE"),
        skeleton_guidance_scale=4.0,
    )
    assert captured["payload"]["skeleton_guidance_scale"] == 4.0


def test_a_keypoint_label_outside_the_enum_is_refused_before_the_call(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="SkeletonLabel"):
        pixellab_client.create_image_bitforge(
            prompt="a knight",
            width=32,
            height=32,
            skeleton_keypoints=[{"x": 1, "y": 2, "label": "TAIL"}],
        )


@pytest.mark.parametrize("value", [-0.5, 5.5])
def test_skeleton_guidance_outside_the_schema_range_is_refused(monkeypatch, value):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="skeleton_guidance_scale"):
        pixellab_client.create_image_bitforge(
            prompt="a knight", width=32, height=32, skeleton_guidance_scale=value
        )


def test_the_keypoint_canvas_warning_matches_the_providers_own_advice():
    """PixelLab's words on ``skeleton_keypoints``: "Warning! Sizes that are not
    16x16, 32x32 and 64x64 can cause the generations to be lower quality".
    Every one is square, and a character is a 1:2 kind."""

    assert pixellab_client.SKELETON_FRIENDLY_SIZES == (16, 32, 64)
    for side in pixellab_client.SKELETON_FRIENDLY_SIZES:
        assert pixellab_client.skeleton_size_warning(side, side) is None
    assert pixellab_client.skeleton_size_warning(32, 64) is not None
    assert pixellab_client.skeleton_size_warning(48, 48) is not None
