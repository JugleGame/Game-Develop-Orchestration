"""PixelLab REST API client — a single HTTP call, nothing else.

One function that returns pixels, one exception type callers fall back on.
Schema verified against PixelLab's own
OpenAPI document, v2 (``https://api.pixellab.ai/v2/openapi.json``,
2026-08-02) — see `12_PixelLab_에셋생성_연동_구현계획.md` §3 for the confirmed
fields. Only ``create-image-pixflux`` is implemented: it is the endpoint the
asset server actually calls (text prompt in, pixel art out); the rest of
PixelLab's v2 surface (bitforge, animation, rotate, inpaint, tilesets, ...)
has no caller yet.

**v1 vs v2**: an earlier pass of this module (and §3) verified v1
(``/v1/generate-image-pixflux``). PixelLab's own marketing/pricing page
(``pixellab.ai/pixellab-api``, checked 2026-08-02) now exclusively advertises
v2 paths (``/v2/create-image-pixflux`` — the ``generate-`` verb became
``create-``, ``no_background``/``seed``/response shape unchanged, minimum
image size rose from 16px to 32px). Whether v1 still resolves was not
tested — this module targets v2 because it is the only version PixelLab
currently documents as current.

No prompt-enhancement or translation step here — PixelLab has no such
endpoint (§3-3, confirmed) and the Korean ``assetsNeeded`` strings are passed
through unchanged. A translation layer is a separate, undecided piece of work
(12문서 §5).
"""

from __future__ import annotations

import base64
import io
import os
import time
from typing import Any

import httpx
from PIL import Image

BASE_URL = "https://api.pixellab.ai/v2"
_GENERATE_PATH = "/create-image-pixflux"
_TIMEOUT_SECONDS = 60.0


class PixelLabUnavailable(Exception):
    """API key missing, request failed, or the response was malformed.

    There is nothing to fall back to: PixelLab is the only generation path
    (``asset/server.py::_generate_image``), so callers turn this into a §03
    code-3000 tool error rather than drawing something else.
    """


def is_configured() -> bool:
    """Whether ``PIXELLAB_API_KEY`` is set — lets a caller skip straight past."""

    return bool(os.getenv("PIXELLAB_API_KEY"))


def _palette_swatch_b64(colors: list[tuple[int, int, int]]) -> str:
    """Encode ``colors`` as a 1px-tall PNG, one pixel per colour, base64'd.

    This is what ``color_image`` actually wants — despite the field's name
    and the endpoint description ("Forced color palette"), the real
    ``CreateImagePixfluxRequest`` schema (confirmed against
    ``v2/openapi.json``, 2026-08-02) has no array-of-colours field; there is
    no ``forced_palette`` field at all. ``color_image`` takes a
    ``Base64Image`` — an actual image PixelLab samples colours from — so the
    palette has to be rendered as pixels first. Verified against the live API
    (200, non-fallback response) before wiring this into ``server.py``.
    """

    swatch = Image.new("RGB", (len(colors), 1))
    for i, colour in enumerate(colors):
        swatch.putpixel((i, 0), colour)
    buf = io.BytesIO()
    swatch.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def generate_image(
    *,
    prompt: str,
    width: int,
    height: int,
    seed: int | None = None,
    no_background: bool = True,
    forced_palette: list[tuple[int, int, int]] | None = None,
    outline: str | None = None,
    shading: str | None = None,
    detail: str | None = None,
    view: str | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    """Call ``/create-image-pixflux`` and return ``(image, usage)``.

    ``width``/``height`` must be 32-400 (Pixflux v2's own limit — verified);
    the asset server derives both from ``style.pixel_grid`` times a per-kind
    ratio (``server.py::_size_for``), which stays in range for every
    configured kind. ``usage`` is PixelLab's own consumption report, relayed
    verbatim so the caller records what the API actually said (12문서 §7).
    Measured 2026-08-03 against the live v2 endpoint: it bills in credits, not
    dollars — ``{"type": "generations", "generations": 1.0}``, with no ``usd``
    field. Do not coerce it into a dollar shape; the credit price depends on
    the account's plan, which this process cannot see.

    ``forced_palette`` locks generation to the game's own colours, sent as
    ``color_image`` (see ``_palette_swatch_b64``). Without it, PixelLab has no
    notion of "this game's look" and every call picks its own colours, which
    is what made two assets from the same locked ``ArtStyle`` come out
    visually unrelated (measured: a purple-cloaked knight next to a
    grey/gold sword with no shared hue).

    ``outline``/``shading``/``detail``/``view`` are PixelLab's own structured
    style controls (confirmed enums, ``v2/openapi.json``, 2026-08-02) —
    ``Outline``: "single color black outline"/"single color outline"/
    "selective outline"/"lineless"; ``Shading``: "flat shading" through
    "highly detailed shading"; ``Detail``: "low"/"medium"/"highly detailed";
    ``CameraView``: "side"/"low top-down"/"high top-down". Passing these
    structurally, instead of stuffing style words into ``description``, is
    what the API actually offers for controlling look — untested until a
    caller sets them (12문서 §10-7 prompting eval, in progress).
    """

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload: dict[str, Any] = {
        "description": prompt,
        "image_size": {"width": width, "height": height},
        "no_background": no_background,
    }
    if seed is not None:
        payload["seed"] = seed
    if forced_palette:
        payload["color_image"] = {
            "type": "base64",
            "base64": _palette_swatch_b64(forced_palette),
            "format": "png",
        }
    if outline is not None:
        payload["outline"] = outline
    if shading is not None:
        payload["shading"] = shading
    if detail is not None:
        payload["detail"] = detail
    if view is not None:
        payload["view"] = view

    try:
        response = httpx.post(
            f"{BASE_URL}{_GENERATE_PATH}",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise PixelLabUnavailable(f"PixelLab request failed: {exc}") from exc

    try:
        image_b64 = data["image"]["base64"]
        image = Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGBA")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise PixelLabUnavailable(f"malformed PixelLab response: {exc}") from exc

    return image, dict(data.get("usage") or {})


def _reject_unless_in(field: str, value: str | None, allowed: tuple[str, ...]) -> None:
    """Fail before the request when a style enum is wrong.

    PixelLab answers 422 with the offending field buried in a JSON body; a
    caller that passes a value from elsewhere (e.g. an ArtStyle whose
    ``camera_view`` is "side") otherwise learns that only after a round trip.
    """

    if value is not None and value not in allowed:
        raise PixelLabUnavailable(
            f"{field}={value!r} is not accepted by /tilesets; use one of {allowed}"
        )


def create_tileset(
    *,
    lower_description: str,
    upper_description: str,
    tile_size: int = 32,
    transition_description: str | None = None,
    view: str | None = None,
    outline: str | None = None,
    shading: str | None = None,
    detail: str | None = None,
    forced_palette: list[str] | None = None,
    poll_seconds: float = 5.0,
    max_polls: int = 60,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Generate a corner-Wang tileset and return ``(tiles, metadata, usage)``.

    Unlike ``generate_image`` this endpoint is asynchronous: ``POST /tilesets``
    answers 202 with **two** ids and only ``tileset_id`` resolves —
    ``background_job_id`` returns 404 forever (measured 2026-08-03). While the
    job runs ``GET /tilesets/{tileset_id}`` answers 423, so this polls until it
    turns 200.

    Each returned tile carries a ``corners`` map (``NW``/``NE``/``SW``/``SE``,
    each ``lower``/``upper``/``transition``) — the caller places a tile by
    matching the four corners of a cell, not by neighbour rules. There are 16
    tiles for the default ``transition_size``; PixelLab names them
    ``wang_0``..``wang_15``.

    ``tile_size`` is 16-64 (PixelLab's own limit), squared. ``usage`` is
    relayed verbatim as with ``generate_image``; measured it comes back
    ``null`` on the retrieval call, so callers must not assume a dict.
    """

    # Argument checks come before the key check: a wrong enum is wrong whether
    # or not the environment is configured, and reporting the env first hides it.
    # Reject bad enums here rather than paying a round trip to learn it. These
    # differ from create-image-pixflux's — ``view`` has no "side" and the
    # outline/shading/detail wordings are the tileset endpoint's own (verified
    # against v2/openapi.json and a live 422, 2026-08-03).
    _reject_unless_in("view", view, ("low top-down", "high top-down"))
    _reject_unless_in("outline", outline, ("none", "single color outline", "thick outline"))
    _reject_unless_in(
        "shading", shading, ("no shading", "light shading", "medium shading", "heavy shading")
    )
    _reject_unless_in("detail", detail, ("minimal detail", "medium detail", "highly detailed"))
    if not 16 <= tile_size <= 64:
        raise PixelLabUnavailable(f"tile_size must be 16-64, got {tile_size}")

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload: dict[str, Any] = {
        "lower_description": lower_description,
        "upper_description": upper_description,
        "tile_size": {"width": tile_size, "height": tile_size},
    }
    if transition_description is not None:
        payload["transition_description"] = transition_description
    if view is not None:
        payload["view"] = view
    if outline is not None:
        payload["outline"] = outline
    if shading is not None:
        payload["shading"] = shading
    if detail is not None:
        payload["detail"] = detail
    if forced_palette:
        payload["forced_palette"] = forced_palette

    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            created = client.post(f"{BASE_URL}/tilesets", json=payload, headers=headers)
            if created.status_code >= 400:
                # The status line alone doesn't say *which* field was rejected,
                # and this endpoint's enums differ from pixflux's (view accepts
                # only the two top-down values here).
                raise PixelLabUnavailable(
                    f"PixelLab rejected the tileset request ({created.status_code}): "
                    f"{created.text[:400]}"
                )
            tileset_id = created.json().get("tileset_id")
            if not tileset_id:
                raise PixelLabUnavailable("PixelLab returned no tileset_id")

            data: dict[str, Any] | None = None
            for _ in range(max_polls):
                got = client.get(f"{BASE_URL}/tilesets/{tileset_id}", headers=headers)
                if got.status_code in (404, 423):
                    time.sleep(poll_seconds)
                    continue
                got.raise_for_status()
                data = got.json()
                break
            if data is None:
                raise PixelLabUnavailable(
                    f"tileset {tileset_id} not ready after {max_polls * poll_seconds:.0f}s"
                )
    except httpx.HTTPError as exc:
        raise PixelLabUnavailable(f"PixelLab tileset request failed: {exc}") from exc

    tiles_raw = (data.get("tileset") or {}).get("tiles") or []
    if not tiles_raw:
        raise PixelLabUnavailable("malformed PixelLab response: no tiles")

    tiles: list[dict[str, Any]] = []
    for tile in tiles_raw:
        encoded = (tile.get("image") or {}).get("base64", "")
        if "," in encoded:
            encoded = encoded.split(",", 1)[1]
        try:
            image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGBA")
        except (TypeError, ValueError, OSError) as exc:
            raise PixelLabUnavailable(f"malformed tile image: {exc}") from exc
        tiles.append(
            {
                "name": tile.get("name", ""),
                "corners": tile.get("corners") or {},
                "image": image,
            }
        )

    return tiles, dict(data.get("metadata") or {}), dict(data.get("usage") or {})


def _poll_json(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    *,
    poll_seconds: float,
    max_polls: int,
) -> dict[str, Any]:
    """GET ``url`` until it stops answering "still working" (423/404)."""

    for _ in range(max_polls):
        got = client.get(url, headers=headers)
        if got.status_code in (404, 423):
            time.sleep(poll_seconds)
            continue
        if got.status_code >= 400:
            raise PixelLabUnavailable(f"PixelLab returned {got.status_code}: {got.text[:400]}")
        return got.json()
    raise PixelLabUnavailable(f"{url} not ready after {max_polls * poll_seconds:.0f}s")



def create_map_object(
    *,
    description: str,
    width: int = 32,
    height: int = 32,
    color_palette: str | None = None,
    poll_seconds: float = 5.0,
    max_polls: int = 24,
) -> tuple[Image.Image, dict[str, Any]]:
    """Generate one transparent map decoration — returns ``(image, usage)``.

    Scattered on a layer above the floor, these are what break up a tiled
    ground without needing a variant for every cell.

    The finished object comes back as a ``download_url``, not base64, and
    PixelLab deletes it 8 hours after creation — so this fetches the bytes
    immediately rather than handing the URL to the caller.
    """

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload: dict[str, Any] = {
        "description": description,
        "image_size": {"width": width, "height": height},
    }
    if color_palette:
        payload["color_palette"] = color_palette

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            created = client.post(f"{BASE_URL}/map-objects", json=payload, headers=headers)
            if created.status_code >= 400:
                raise PixelLabUnavailable(
                    f"PixelLab rejected the map-object request ({created.status_code}): "
                    f"{created.text[:400]}"
                )
            object_id = created.json().get("object_id")
            if not object_id:
                raise PixelLabUnavailable("PixelLab returned no object_id")

            data = _poll_json(
                client,
                f"{BASE_URL}/map-objects/{object_id}",
                headers,
                poll_seconds=poll_seconds,
                max_polls=max_polls,
            )

            url = data.get("download_url")
            if not url:
                raise PixelLabUnavailable(f"map object {object_id} has no download_url")
            downloaded = client.get(url)
            downloaded.raise_for_status()
    except httpx.HTTPError as exc:
        raise PixelLabUnavailable(f"PixelLab map-object request failed: {exc}") from exc

    try:
        image = Image.open(io.BytesIO(downloaded.content)).convert("RGBA")
    except OSError as exc:
        raise PixelLabUnavailable(f"malformed map object image: {exc}") from exc
    return image, dict(data.get("usage") or {})


def _decode(encoded: str) -> Image.Image:
    """Decode a PixelLab base64 image, with or without a data: prefix."""

    if "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGBA")
    except (TypeError, ValueError, OSError) as exc:
        raise PixelLabUnavailable(f"malformed image in PixelLab response: {exc}") from exc


__all__ = [
    "PixelLabUnavailable",
    "create_map_object",
    "create_tileset",
    "generate_image",
    "is_configured",
]
