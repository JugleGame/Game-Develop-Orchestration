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
    )

    assert arguments["description"] == "a mossy rock; single centered isolated object"
    assert arguments["image_size"] == {"width": 32, "height": 32}
    assert arguments["text_guidance_scale"] == 16.0
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
        output_size=(64, 64),
        style_description="dark fantasy pixel art",
        seed=9,
        poll_seconds=0,
    )

    assert captured["url"] == "https://api.pixellab.ai/v2/generate-with-style-v2"
    assert captured["payload"]["description"] == "a red treasure chest"
    assert captured["payload"]["style_images"][0]["width"] == 64
    assert captured["payload"]["style_images"][1]["width"] == 32
    assert captured["payload"]["style_images"][1]["height"] == 64
    assert len(captured["payload"]["style_images"]) == 2
    assert "image_size" not in captured["payload"]
    assert len(images) == 2
    assert usage["generations"] == 2.0
    assert job_id == "job-1"


def test_generate_with_style_rejects_non_square_output(monkeypatch):
    monkeypatch.setenv("PIXELLAB_API_KEY", "sk-test")

    with pytest.raises(pixellab_client.PixelLabUnavailable, match="must be square"):
        pixellab_client.generate_with_style(
            prompt="a tall hero",
            style_images=[Image.new("RGBA", (64, 128), (1, 2, 3, 255))],
            output_size=(64, 128),
            style_description="pixel art",
            seed=9,
        )


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
