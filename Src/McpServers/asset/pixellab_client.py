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
import json
import os
import re
import time
from functools import partial
from typing import Any

import anyio
import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

BASE_URL = "https://api.pixellab.ai/v2"
MCP_URL = "https://api.pixellab.ai/mcp"
_GENERATE_PATH = "/create-image-pixflux"
_TIMEOUT_SECONDS = 60.0


class PixelLabUnavailable(Exception):
    """API key missing, request failed, or the response was malformed.

    There is nothing to fall back to: PixelLab is the only generation path
    (``asset/server.py::_generate_image``), so callers turn this into an MCP
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


def _image_b64(image: Image.Image) -> str:
    """Encode an image in PixelLab's Base64Image shape without changing pixels."""

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _prototype_tool(tools: list[Any], kind: str) -> Any:
    """Choose a compatible image-creation tool exposed by PixelLab's MCP server."""

    by_name = {tool.name: tool for tool in tools}
    preferred = {
        "character": ("create_image_pixflux", "create_image_pixen", "create_character"),
        "monster": ("create_image_pixflux", "create_image_pixen", "create_character"),
        "tile": ("create_image_pixflux", "create_image_pixen", "create_isometric_tile"),
        "prop": ("create_image_pixflux", "create_image_pixen", "create_map_object"),
        "icon": ("create_image_pixflux", "create_image_pixen", "create_ui_asset"),
        "ui_button": ("create_image_pixflux", "create_image_pixen", "create_ui_asset"),
        "ui_panel": ("create_image_pixflux", "create_image_pixen", "create_ui_asset"),
    }[kind]
    for name in preferred:
        if name in by_name:
            return by_name[name]

    for tool in tools:
        properties = (tool.input_schema or {}).get("properties") or {}
        name = tool.name.casefold()
        if ("description" in properties or "prompt" in properties) and any(
            word in name for word in ("image", "character", "object", "tile", "ui")
        ):
            return tool
    raise PixelLabUnavailable("PixelLab MCP exposes no compatible 2D prototype tool")


def _prototype_arguments(
    tool: Any,
    prompt: str,
    width: int,
    height: int,
    seed: int,
    style_description: str,
    style_params: dict[str, str],
    palette: list[str],
) -> dict[str, Any]:
    """Map the common prototype request onto the selected official tool schema."""

    schema = tool.input_schema or {}
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    if "description" in properties:
        arguments["description"] = prompt
    elif "prompt" in properties:
        arguments["prompt"] = prompt
    if "image_size" in properties:
        arguments["image_size"] = {"width": width, "height": height}
    if "width" in properties:
        arguments["width"] = width
    if "height" in properties:
        arguments["height"] = height
    if "size" in properties:
        arguments["size"] = max(width, height)
    if "no_background" in properties:
        arguments["no_background"] = True
    if "seed" in properties:
        arguments["seed"] = seed
    if "text_guidance_scale" in properties:
        # PixelLab defaults to 8, which favored atmosphere over required
        # object structure in review prototypes. A stronger literal setting
        # keeps named parts such as bottle necks and platform edges readable.
        arguments["text_guidance_scale"] = 16.0
    if "style_description" in properties:
        arguments["style_description"] = style_description
    for field, value in style_params.items():
        if field in properties:
            arguments[field] = value
    if "color_palette" in properties:
        arguments["color_palette"] = ", ".join(palette)
    if "forced_palette" in properties:
        arguments["forced_palette"] = palette
    if "color_image_base64" in properties and palette:
        colors = [
            (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
            for color in palette
        ]
        arguments["color_image_base64"] = _palette_swatch_b64(colors)
    if "n_directions" in properties:
        arguments["n_directions"] = 4
    if "lower" in properties:
        arguments["lower"] = prompt
    if "upper" in properties:
        arguments["upper"] = prompt

    missing = set(schema.get("required") or ()) - set(arguments)
    if missing:
        raise PixelLabUnavailable(
            f"PixelLab MCP tool {tool.name!r} has unsupported required fields: {sorted(missing)}"
        )
    return arguments


def _encoded_image(value: Any) -> str | None:
    """Find the first inline image in a tool result or nested structured payload."""

    if isinstance(value, dict):
        encoded = value.get("base64")
        if isinstance(encoded, str) and encoded:
            return encoded
        for nested in value.values():
            found = _encoded_image(nested)
            if found:
                return found
    elif isinstance(value, list | tuple):
        for nested in value:
            found = _encoded_image(nested)
            if found:
                return found
    return None


def _image_url(value: Any) -> str | None:
    """Find the first downloadable image URL in a structured MCP result."""

    if isinstance(value, dict):
        for key in ("download_url", "image_url", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return candidate
        for nested in value.values():
            found = _image_url(nested)
            if found:
                return found
    elif isinstance(value, list | tuple):
        for nested in value:
            found = _image_url(nested)
            if found:
                return found
    elif isinstance(value, str):
        match = re.search(r"https://[^\s)\]>'\"]+", value)
        if match:
            return match.group(0)
    return None


def _result_payloads(result: Any) -> list[Any]:
    """Collect structured and JSON text payloads from an MCP tool result."""

    payloads: list[Any] = []
    structured = getattr(result, "structured_content", None)
    if structured:
        payloads.append(structured)
    for block in result.content:
        if getattr(block, "type", "") != "text":
            continue
        text = getattr(block, "text", "")
        try:
            payloads.append(json.loads(text))
        except (TypeError, ValueError):
            payloads.append(text)
    return payloads


def _result_image(result: Any) -> tuple[str | None, str | None]:
    """Return the first inline image or download URL from an MCP result."""

    for block in result.content:
        if getattr(block, "type", "") == "image":
            encoded = getattr(block, "data", None)
            if encoded:
                return encoded, None
    for payload in _result_payloads(result):
        encoded = _encoded_image(payload)
        if encoded:
            return encoded, None
        url = _image_url(payload)
        if url:
            return None, url
    return None, None


def _identifier(value: Any, keys: tuple[str, ...]) -> str | None:
    """Find a job-like identifier in a nested MCP result."""

    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for nested in value.values():
            found = _identifier(nested, keys)
            if found:
                return found
    elif isinstance(value, list | tuple):
        for nested in value:
            found = _identifier(nested, keys)
            if found:
                return found
    elif isinstance(value, str):
        match = re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            value,
            re.IGNORECASE,
        )
        if match:
            return match.group(0)
    return None


def _result_usage(result: Any) -> dict[str, Any]:
    for payload in _result_payloads(result):
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            return dict(payload["usage"])
    return {}


async def _generate_prototype_async(
    *,
    prompt: str,
    width: int,
    height: int,
    kind: str,
    seed: int,
    style_description: str,
    style_params: dict[str, str],
    palette: list[str],
) -> tuple[Image.Image, dict[str, Any], str]:
    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx2.Timeout(_TIMEOUT_SECONDS),
    )
    try:
        async with client:
            async with streamable_http_client(MCP_URL, http_client=client) as (read, write):
                async with ClientSession(
                    read, write, read_timeout_seconds=_TIMEOUT_SECONDS
                ) as session:
                    await session.initialize()
                    tool = _prototype_tool((await session.list_tools()).tools, kind)
                    result = await session.call_tool(
                        tool.name,
                        _prototype_arguments(
                            tool,
                            prompt,
                            width,
                            height,
                            seed,
                            style_description,
                            style_params,
                            palette,
                        ),
                        read_timeout_seconds=_TIMEOUT_SECONDS,
                    )
                    if getattr(result, "is_error", False):
                        text = " ".join(
                            getattr(block, "text", "") for block in result.content
                        )
                        raise PixelLabUnavailable(f"PixelLab MCP tool failed: {text[:400]}")
                    usage = _result_usage(result)
                    encoded, image_url = _result_image(result)
                    if not encoded and not image_url:
                        payloads = _result_payloads(result)
                        if tool.name == "create_character":
                            identifier = next(
                                (
                                    found
                                    for payload in payloads
                                    if (found := _identifier(payload, ("character_id",)))
                                ),
                                None,
                            )
                            poll_tool = "get_character"
                            poll_arguments = {"character_id": identifier, "include_preview": True}
                        else:
                            identifier = next(
                                (
                                    found
                                    for payload in payloads
                                    if (
                                        found := _identifier(
                                            payload, ("job_id", "background_job_id")
                                        )
                                    )
                                ),
                                None,
                            )
                            poll_tool = "get_image"
                            poll_arguments = {"job_id": identifier}
                        if not identifier:
                            raise PixelLabUnavailable(
                                f"PixelLab MCP tool {tool.name!r} returned no job identifier"
                            )

                        for _ in range(60):
                            await anyio.sleep(5)
                            result = await session.call_tool(
                                poll_tool,
                                poll_arguments,
                                read_timeout_seconds=_TIMEOUT_SECONDS,
                            )
                            if getattr(result, "is_error", False):
                                text = " ".join(
                                    getattr(block, "text", "") for block in result.content
                                )
                                raise PixelLabUnavailable(
                                    f"PixelLab MCP polling failed: {text[:400]}"
                                )
                            usage = _result_usage(result) or usage
                            encoded, image_url = _result_image(result)
                            if encoded or image_url:
                                break
                        else:
                            raise PixelLabUnavailable(
                                f"PixelLab MCP job {identifier} did not finish within 300 seconds"
                            )
    except PixelLabUnavailable:
        raise
    except Exception as exc:
        raise PixelLabUnavailable(f"PixelLab MCP request failed: {exc}") from exc

    if getattr(result, "is_error", False):
        text = " ".join(getattr(block, "text", "") for block in result.content)
        raise PixelLabUnavailable(f"PixelLab MCP tool failed: {text[:400]}")

    if encoded:
        image = _decode(encoded)
    elif image_url:
        try:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {api_key}"}, timeout=_TIMEOUT_SECONDS
            ) as download_client:
                downloaded = await download_client.get(image_url)
                downloaded.raise_for_status()
            image = Image.open(io.BytesIO(downloaded.content)).convert("RGBA")
        except Exception as exc:
            raise PixelLabUnavailable(f"PixelLab MCP image download failed: {exc}") from exc
    else:
        raise PixelLabUnavailable(
            f"PixelLab MCP tool {tool.name!r} returned no downloadable image"
        )
    return image, usage, tool.name


def generate_prototype(
    *,
    prompt: str,
    width: int,
    height: int,
    kind: str,
    seed: int,
    style_description: str,
    style_params: dict[str, str],
    palette: list[str],
) -> tuple[Image.Image, dict[str, Any], str]:
    """Generate one style prototype through PixelLab's official remote MCP."""

    return anyio.run(
        partial(
            _generate_prototype_async,
            prompt=prompt,
            width=width,
            height=height,
            kind=kind,
            seed=seed,
            style_description=style_description,
            style_params=style_params,
            palette=palette,
        )
    )


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


def generate_with_style(
    *,
    prompt: str,
    style_images: list[Image.Image],
    output_size: tuple[int, int],
    style_description: str,
    seed: int,
    poll_seconds: float = 5.0,
    max_polls: int = 60,
) -> tuple[list[Image.Image], dict[str, Any], str]:
    """Generate a variation set through the style-reference REST endpoint."""

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")
    if not 1 <= len(style_images) <= 4:
        raise PixelLabUnavailable("style_images must contain between 1 and 4 images")
    if any(max(image.size) > 512 for image in style_images):
        raise PixelLabUnavailable("style image dimensions must not exceed 512 pixels")
    if output_size[0] != output_size[1] or not 16 <= output_size[0] <= 512:
        raise PixelLabUnavailable("generate-with-style-v2 output must be square and 16-512 pixels")

    payload = {
        "style_images": [
            {
                "image": {"type": "base64", "base64": _image_b64(image)},
                "width": image.width,
                "height": image.height,
            }
            for image in style_images
        ],
        "image_size": {"width": output_size[0], "height": output_size[1]},
        "description": prompt,
        "style_description": style_description[:500],
        "seed": seed,
        "no_background": True,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            created = client.post(
                f"{BASE_URL}/generate-with-style-v2", json=payload, headers=headers
            )
            if created.status_code >= 400:
                raise PixelLabUnavailable(
                    f"PixelLab rejected the variation request ({created.status_code}): "
                    f"{created.text[:400]}"
                )
            created_data = created.json()
            job_id = created_data.get("background_job_id")
            if not job_id:
                raise PixelLabUnavailable("PixelLab returned no background_job_id")

            data: dict[str, Any] | None = None
            for _ in range(max_polls):
                got = client.get(f"{BASE_URL}/background-jobs/{job_id}", headers=headers)
                if got.status_code in (404, 423):
                    time.sleep(poll_seconds)
                    continue
                if got.status_code >= 400:
                    raise PixelLabUnavailable(
                        f"PixelLab returned {got.status_code}: {got.text[:400]}"
                    )
                candidate = got.json()
                status = candidate.get("status")
                if status == "failed":
                    raise PixelLabUnavailable(
                        f"PixelLab variation job failed: {candidate.get('last_response')!r}"
                    )
                if status == "completed":
                    data = candidate
                    break
                time.sleep(poll_seconds)
            if data is None:
                raise PixelLabUnavailable(
                    f"variation job {job_id} not ready after "
                    f"{max_polls * poll_seconds:.0f}s"
                )
    except httpx.HTTPError as exc:
        raise PixelLabUnavailable(f"PixelLab variation request failed: {exc}") from exc

    response = data.get("last_response") or {}
    response_dict = response if isinstance(response, dict) else {}
    encoded_images: list[str] = []
    raw_images = response_dict.get("images")
    if isinstance(raw_images, list):
        for item in raw_images:
            encoded = _encoded_image(item)
            if encoded:
                encoded_images.append(encoded)
    else:
        encoded = _encoded_image(response)
        if encoded:
            encoded_images.append(encoded)
    if not encoded_images:
        raise PixelLabUnavailable("malformed PixelLab variation response: no images")

    usage = data.get("usage") or created_data.get("usage") or response_dict.get("usage") or {}
    images = [_decode(encoded) for encoded in encoded_images]
    if any(image.size != output_size for image in images):
        raise PixelLabUnavailable(
            f"PixelLab returned an inconsistent variation size; expected {output_size}"
        )
    return images, dict(usage), job_id


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
    "generate_prototype",
    "generate_with_style",
    "is_configured",
]
