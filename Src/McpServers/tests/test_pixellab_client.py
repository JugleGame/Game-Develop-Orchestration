"""Tests for asset/pixellab_client.py — the PixelLab HTTP call in isolation.

No real network access: every test mocks ``httpx.post``. Schema fields match
what 12문서 §3 confirmed against PixelLab's own openapi.json (2026-08-02).
"""

import base64
import io

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
