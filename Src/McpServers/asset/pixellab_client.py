"""PixelLab REST API client — a single HTTP call, nothing else.

One function that returns pixels, one exception type callers fall back on.
Schema verified against PixelLab's own
OpenAPI document, v2 (``https://api.pixellab.ai/v2/openapi.json``,
2026-08-02) — see `12_PixelLab_에셋생성_연동_구현계획.md` §3 for the confirmed
fields. ``create-image-pixflux``, ``generate-with-style-v2``, ``tilesets``,
``map-objects``, and ``animate-with-text-v3`` have callers; the rest of
PixelLab's v2 surface (bitforge, rotate, inpaint, ...) does not. The style
enums differ per endpoint — see ``STYLE_ENUMS``.

**v1 vs v2**: an earlier pass of this module (and §3) verified v1
(``/v1/generate-image-pixflux``). PixelLab's own marketing/pricing page
(``pixellab.ai/pixellab-api``, checked 2026-08-02) now exclusively advertises
v2 paths (``/v2/create-image-pixflux`` — the ``generate-`` verb became
``create-``, ``no_background``/``seed``/response shape unchanged, and
``image_size`` stayed 16-400px per side). Whether v1 still resolves was not
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

#: Frame counts ``/animate-with-text-v3`` accepts. Odd values are refused with
#: 422 by the provider, so they are refused here before a request is spent.
ANIMATION_FRAME_COUNTS = (4, 6, 8, 10, 12, 14, 16)

# PixelLab's shared style enums (``Outline``/``Shading``/``Detail``/
# ``CameraView`` in ``v2/openapi.json``, re-verified 2026-08-22).
_OUTLINE = (
    "single color black outline",
    "single color outline",
    "selective outline",
    "lineless",
)
_SHADING = (
    "flat shading",
    "basic shading",
    "medium shading",
    "detailed shading",
    "highly detailed shading",
)
_DETAIL = ("low detail", "medium detail", "highly detailed")
_CAMERA_VIEW = ("side", "low top-down", "high top-down")
#: ``Direction`` — which way the subject faces. Its own field, and the one the
#: server had no way to set: before this, facing could only be asked for in
#: prose, where it competes with the subject for the model's attention.
DIRECTIONS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)

#: Allowed style values per endpoint. These are *not* the same everywhere and
#: the differences are not guessable: ``/tilesets`` uses ``TilesetCameraView``,
#: which has no "side", and ``/map-objects`` declares its own inline enums that
#: drop "single color black outline" and "highly detailed shading" and spell
#: the top detail level "high detail" rather than "highly detailed". One table
#: so a caller learns the difference here instead of from a 422 body.
STYLE_ENUMS: dict[str, dict[str, tuple[str, ...]]] = {
    "create-image-pixflux": {
        "outline": _OUTLINE,
        "shading": _SHADING,
        "detail": _DETAIL,
        "view": _CAMERA_VIEW,
        "direction": DIRECTIONS,
    },
    "tilesets": {
        "outline": _OUTLINE,
        "shading": _SHADING,
        "detail": _DETAIL,
        "view": ("low top-down", "high top-down"),
    },
    # No "direction" row: CreateTilesetRequest and CreateMapObjectRequest do
    # not declare the field at all. Passing one is a caller error, not a value
    # error, so it fails on the missing keyword rather than a wrong value.
    "map-objects": {
        "outline": ("single color outline", "selective outline", "lineless"),
        "shading": ("flat shading", "basic shading", "medium shading", "detailed shading"),
        "detail": ("low detail", "medium detail", "high detail"),
        "view": ("low top-down", "high top-down", "side"),
    },
}

#: ``TileSize.width``/``height`` is an enum, not a range. 64 additionally
#: requires ``mode="pro"``, which no caller here sends.
TILE_SIZES = (16, 32)

#: ``text_guidance_scale`` bounds, shared by every endpoint that takes it.
#: The provider's own default is 8; anything outside 1-20 is a 422.
TEXT_GUIDANCE_RANGE = (1.0, 20.0)
DEFAULT_TEXT_GUIDANCE = 8.0

BASE_URL = "https://api.pixellab.ai/v2"
MCP_URL = "https://api.pixellab.ai/mcp"
_GENERATE_PATH = "/create-image-pixflux"
_TIMEOUT_SECONDS = 60.0


class PixelLabUnavailable(Exception):
    """API key missing, request failed, or the response was malformed.

    There is nothing to fall back to: PixelLab is the only generation path
    (``asset/server.py::_generate_prototype``), so callers turn this into an MCP
    code-3000 tool error rather than drawing something else.

    ``job_started`` says whether PixelLab had already accepted a remote job
    when this failed. The caller uses it to decide whether a retry may be
    billed twice (``asset/server.py::_generate_prototype``).
    """

    def __init__(self, message: str, *, job_started: bool = False) -> None:
        super().__init__(message)
        self.job_started = job_started


def _flatten_exception(exc: BaseException) -> str:
    """Spell out an ``ExceptionGroup``'s leaves instead of its own summary.

    ``async with`` on a TaskGroup raises a group whose ``str`` is only
    "unhandled errors in a TaskGroup (1 sub-exception)". The one thing the
    caller needs — what actually went wrong — lives in ``.exceptions``, and
    groups nest, so this walks all the way down to the leaves.
    """

    leaves: list[str] = []
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop(0)
        nested = getattr(current, "exceptions", None)
        if nested:
            pending.extend(nested)
            continue
        leaves.append(f"{type(current).__name__}: {current}")
    return "; ".join(leaves) or f"{type(exc).__name__}: {exc}"


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


def _fit_to_canvas(image: Image.Image, width: int, height: int) -> Image.Image:
    """Resize a reference to an exact canvas without changing its proportions.

    The endpoint requires the reference to be exactly the requested canvas, so
    something has to give when the two shapes disagree. Stretching is the wrong
    thing to give: measured 2026-08-23, a 66x161 concept-art cutout squashed
    into 64x64 came back as two and then three overlapping figures, at
    ``init_image_strength`` 900 and 600 alike — the strength was not what was
    wrong, the reference was. A squashed human reads as several humans.

    So the image is scaled by one factor and the leftover is transparent. The
    subject sits **bottom-centred**, because a side-view sprite stands on the
    bottom of its canvas and that is where the generator is being asked to put
    it. References that already match the canvas aspect — every stored sprite
    of the same kind, which is the ordinary case — take the plain resize and
    are unaffected.
    """

    if image.size == (width, height):
        return image
    source = image if image.mode == "RGBA" else image.convert("RGBA")
    if source.width * height == source.height * width:
        return source.resize((width, height), Image.NEAREST)
    scale = min(width / source.width, height / source.height)
    scaled = source.resize(
        (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
        Image.NEAREST,
    )
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.paste(scaled, ((width - scaled.width) // 2, height - scaled.height))
    return canvas


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
    text_guidance_scale: float,
) -> dict[str, Any]:
    """Map the common prototype request onto the selected official tool schema."""

    schema = tool.input_schema or {}
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    description = prompt
    if "style_description" not in properties and style_description:
        description = f"{prompt}; art style: {style_description}"
    if "description" in properties:
        arguments["description"] = description
    elif "prompt" in properties:
        arguments["prompt"] = description
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
        # Relayed, not fixed: this used to hard-code 16 while the REST path
        # sent nothing and got PixelLab's own 8, so two assets in one game were
        # generated at different literal-following strengths.
        arguments["text_guidance_scale"] = text_guidance_scale
    if "style_description" in properties:
        arguments["style_description"] = style_description
    for field, value in style_params.items():
        if field not in properties:
            continue
        allowed = (properties[field] or {}).get("enum")
        if allowed and value not in allowed:
            # The style is game-wide, so a wrong enum would otherwise burn one
            # request per asset before the 422 explains itself.
            raise PixelLabUnavailable(
                f"{field}={value!r} is not accepted by PixelLab tool {tool.name!r}; "
                f"use one of {tuple(allowed)}"
            )
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
    if "lower" in properties or "upper" in properties:
        # A Wang tileset is defined by the boundary between two terrains, and
        # it derives that boundary from the two descriptions differing. Sending
        # the same text as both produced a set with nothing to transition
        # between. The caller writes them as "lower | upper"; without a
        # separator the whole prompt is the lower terrain and the upper is left
        # for the provider to default, which at least is not a contradiction.
        lower, _, upper = prompt.partition("|")
        lower, upper = lower.strip(), upper.strip()
        if "upper" in properties and not upper:
            raise PixelLabUnavailable(
                f"PixelLab tool {tool.name!r} builds a tileset from two terrains; "
                'write the prompt as "<lower terrain> | <upper terrain>"'
            )
        if "lower" in properties:
            arguments["lower"] = lower or prompt
        if upper:
            arguments["upper"] = upper

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
    text_guidance_scale: float,
) -> tuple[Image.Image, dict[str, Any], str]:
    _reject_text_guidance(text_guidance_scale)
    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx2.Timeout(_TIMEOUT_SECONDS),
    )
    # Once PixelLab hands back a job identifier the image may already be
    # billed, so a failure past this point must not be retried blindly.
    job_started = False
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
                            text_guidance_scale,
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
                        job_started = True

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
                                    f"PixelLab MCP polling failed: {text[:400]}",
                                    job_started=True,
                                )
                            usage = _result_usage(result) or usage
                            encoded, image_url = _result_image(result)
                            if encoded or image_url:
                                break
                        else:
                            raise PixelLabUnavailable(
                                f"PixelLab MCP job {identifier} did not finish within 300 seconds",
                                job_started=True,
                            )
    except PixelLabUnavailable:
        raise
    except Exception as exc:
        raise PixelLabUnavailable(
            f"PixelLab MCP request failed: {_flatten_exception(exc)}",
            job_started=job_started,
        ) from exc

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
    text_guidance_scale: float = DEFAULT_TEXT_GUIDANCE,
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
            text_guidance_scale=text_guidance_scale,
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
    direction: str | None = None,
    isometric: bool = False,
    text_guidance_scale: float = DEFAULT_TEXT_GUIDANCE,
) -> tuple[Image.Image, dict[str, Any]]:
    """Call ``/create-image-pixflux`` and return ``(image, usage)``.

    ``width``/``height`` must be 16-400 (Pixflux v2's own limit);
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
    ``CameraView``: "side"/"low top-down"/"high top-down" — see
    ``STYLE_ENUMS["create-image-pixflux"]`` for the exact wordings.

    ``direction`` is one of ``DIRECTIONS`` and says which way the subject
    faces. ``isometric`` is a boolean and is **not** a camera view: top-down
    looks straight down a vertical axis, isometric looks along a diagonal one,
    so a game asking for isometric used to be sent "high top-down" and nothing
    ever carried the actual request.

    All of these are documented ``(weakly guiding)``. They bias the result;
    they do not override the description, which is why the description keeps
    its own wording rather than having it stripped out (see
    ``prompting.compose``).

    ``text_guidance_scale`` is how literally the description is followed
    (1-20). It is relayed rather than fixed here so both generation paths use
    one value: the MCP path used to hard-code 16 while this one sent nothing
    at all, which left two assets in the same game generated at different
    strengths. The default is PixelLab's own.
    """

    _reject_style_enums(
        "create-image-pixflux",
        outline=outline,
        shading=shading,
        detail=detail,
        view=view,
        direction=direction,
    )
    _reject_text_guidance(text_guidance_scale)

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload: dict[str, Any] = {
        "description": prompt,
        "image_size": {"width": width, "height": height},
        "no_background": no_background,
        "text_guidance_scale": text_guidance_scale,
    }
    if direction is not None:
        payload["direction"] = direction
    if isometric:
        payload["isometric"] = True
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


#: ``StyleImage.width``/``height`` cap. The model works at this size, and the
#: output follows the style images rather than a requested size.
STYLE_IMAGE_MAX_SIDE = 512

#: ``CreateImageBitforgeRequest.image_size`` stops at 200 per side, half of
#: pixflux's 400. A caller that needs a larger canvas cannot use this endpoint.
BITFORGE_SIDE_RANGE = (16, 200)

#: ``style_strength`` defaults to 0 in the schema, which means "ignore the
#: style image entirely". Sending a reference and leaving the strength at the
#: provider default would silently do nothing, so a caller that supplies a
#: ``style_image`` and no strength gets the schema's own documented midpoint
#: ("50 = balanced") instead.
BITFORGE_BALANCED_STYLE_STRENGTH = 50

_BITFORGE_PATH = "/create-image-bitforge"
_SKELETON_PATH = "/estimate-skeleton"

#: ``SkeletonLabel`` — the joints a keypoint may name. Anything else is a 422.
SKELETON_LABELS: tuple[str, ...] = (
    "NOSE",
    "NECK",
    "RIGHT SHOULDER",
    "RIGHT ELBOW",
    "RIGHT ARM",
    "LEFT SHOULDER",
    "LEFT ELBOW",
    "LEFT ARM",
    "RIGHT HIP",
    "RIGHT KNEE",
    "RIGHT LEG",
    "LEFT HIP",
    "LEFT KNEE",
    "LEFT LEG",
    "RIGHT EYE",
    "LEFT EYE",
    "RIGHT EAR",
    "LEFT EAR",
)

#: Canvases PixelLab says keypoints work well on. Its own words on
#: ``skeleton_keypoints``: "Warning! Sizes that are not 16x16, 32x32 and 64x64
#: can cause the generations to be lower quality". Every one of them is square,
#: and a character is a 1:2 kind — so posing a character is a request the
#: provider warns about rather than refuses. The caller is told, not blocked.
#:
#: "Lower quality" understates it (measured 2026-08-22, same reference and
#: seed): at 32x64 with guidance 4.0 the result was noise with no figure in it,
#: while the same request at 64x64 came back as a clean posed knight. The
#: difference is the canvas, not the keypoints.
SKELETON_FRIENDLY_SIZES: tuple[int, ...] = (16, 32, 64)

#: ``skeleton_guidance_scale`` bounds and the provider's own default.
SKELETON_GUIDANCE_RANGE = (0.0, 5.0)
DEFAULT_SKELETON_GUIDANCE = 1.0


def skeleton_size_warning(width: int, height: int) -> str | None:
    """Whether keypoints on this canvas are outside PixelLab's advice."""

    if width == height and width in SKELETON_FRIENDLY_SIZES:
        return None
    return (
        f"{width}x{height} is not one of PixelLab's keypoint-friendly canvases "
        f"({', '.join(f'{side}x{side}' for side in SKELETON_FRIENDLY_SIZES)}); "
        "measured, an off-canvas request came back as noise at high guidance "
        "while the same request on a friendly canvas came back clean"
    )


def _skeleton_points(keypoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate keypoints and reduce them to the request's ``Point`` shape.

    ``/estimate-skeleton`` answers with a ``z_index`` that is a float while the
    request's ``Point`` declares an integer, so the round trip needs a coercion
    the caller should not have to know about.
    """

    points: list[dict[str, Any]] = []
    for index, keypoint in enumerate(keypoints):
        label = str(keypoint.get("label", ""))
        if label not in SKELETON_LABELS:
            raise PixelLabUnavailable(
                f"skeleton_keypoints[{index}].label={label!r} is not a SkeletonLabel; "
                f"use one of {SKELETON_LABELS}"
            )
        try:
            point = {
                "x": float(keypoint["x"]),
                "y": float(keypoint["y"]),
                "label": label,
                "z_index": int(keypoint.get("z_index", 0)),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise PixelLabUnavailable(
                f"skeleton_keypoints[{index}] is not a usable point: {exc}"
            ) from exc
        points.append(point)
    return points


def estimate_skeleton(image: Image.Image) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read joint keypoints out of an existing sprite — ``(keypoints, usage)``.

    This is what makes a pose reusable: an approved sprite already stands the
    way the game wants, so its skeleton can be handed to the next asset instead
    of describing the pose in prose and hoping.

    **The coordinates are normalised to 0-1, not pixels.** The schema types
    ``x``/``y`` as bare numbers and says nothing about their range, so this is
    the kind of thing that has to be measured: a full-body 128x256 sprite came
    back with every joint between 0.4 and 0.9 (2026-08-22). They therefore
    transfer to a canvas of any size unchanged — scaling them by a size ratio
    collapses the pose into a corner and the generation comes back as noise.
    """

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload = {
        "image": {"type": "base64", "base64": _image_b64(image), "format": "png"}
    }
    try:
        response = httpx.post(
            f"{BASE_URL}{_SKELETON_PATH}",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise PixelLabUnavailable(f"PixelLab skeleton request failed: {exc}") from exc

    keypoints = data.get("keypoints")
    if not isinstance(keypoints, list) or not keypoints:
        raise PixelLabUnavailable("malformed PixelLab skeleton response: no keypoints")
    return _skeleton_points(keypoints), dict(data.get("usage") or {})


def create_image_bitforge(
    *,
    prompt: str,
    width: int,
    height: int,
    style_image: Image.Image | None = None,
    style_strength: int | None = None,
    negative_description: str = "",
    coverage_percentage: float | None = None,
    init_image: Image.Image | None = None,
    init_image_strength: int | None = None,
    oblique_projection: bool = False,
    isometric: bool = False,
    direction: str | None = None,
    skeleton_keypoints: list[dict[str, Any]] | None = None,
    skeleton_guidance_scale: float | None = None,
    seed: int | None = None,
    no_background: bool = True,
    forced_palette: list[tuple[int, int, int]] | None = None,
    outline: str | None = None,
    shading: str | None = None,
    detail: str | None = None,
    view: str | None = None,
    text_guidance_scale: float = DEFAULT_TEXT_GUIDANCE,
) -> tuple[Image.Image, dict[str, Any]]:
    """Call ``/create-image-bitforge`` and return ``(image, usage)``.

    Synchronous like ``generate_image``: 200 with ``{"image": ..., "usage":
    ...}``, no polling. It is the endpoint that carries the controls pixflux
    does not have, which is the whole reason for a second image path:

    * ``style_image`` + ``style_strength`` (0-100) — "draw it like this
      picture". Nothing in pixflux does this; the game's palette lock is the
      closest it gets, and a palette cannot carry line weight, proportion, or
      shading habits.
    * ``negative_description`` — here it is a live field
      (``"Text description of what to avoid in the generated image"``),
      whereas pixflux marks the same field ``(Deprecated)``. That difference
      is why exclusions are dropped on one path and sent on this one.
    * ``coverage_percentage`` — how much of the canvas the subject fills,
      which is the field that actually addresses "the figure came out cropped"
      rather than stretching the canvas ratio until it stops happening.
    * ``init_image`` + ``init_image_strength`` (1-999) — start from a drawing.
    * ``skeleton_keypoints`` + ``skeleton_guidance_scale`` (0-5) — say where the
      joints go. Unlike everything else here this is coordinates, not prose or
      a picture, so it is the one control that states a pose outright. See
      ``SKELETON_FRIENDLY_SIZES`` for the canvases the provider recommends.
    * ``oblique_projection`` / ``isometric`` — real booleans, not camera views.

    The cost is reach: ``image_size`` stops at 200 per side (``pixflux``
    allows 400), so a large asset still has to go through ``generate_image``.

    ``style_image`` and ``init_image`` are fitted to the requested canvas
    before they are sent. The endpoint requires an exact match and says so
    with a 500 rather than a 422, which is not something a caller can be
    expected to discover from the schema. A reference whose aspect differs from
    the canvas is padded, not stretched — see ``_fit_to_canvas``.
    """

    _reject_style_enums(
        "create-image-pixflux",
        outline=outline,
        shading=shading,
        detail=detail,
        view=view,
        direction=direction,
    )
    _reject_text_guidance(text_guidance_scale)
    low, high = BITFORGE_SIDE_RANGE
    if not (low <= width <= high and low <= height <= high):
        raise PixelLabUnavailable(
            f"bitforge size must be {low}-{high}px per side, got {width}x{height}; "
            "use generate_image for a larger canvas"
        )
    if style_strength is not None and not 0 <= style_strength <= 100:
        raise PixelLabUnavailable(
            f"style_strength must be 0-100, got {style_strength!r}"
        )
    if coverage_percentage is not None and not 0 <= coverage_percentage <= 100:
        raise PixelLabUnavailable(
            f"coverage_percentage must be 0-100, got {coverage_percentage!r}"
        )
    if init_image_strength is not None and not 1 <= init_image_strength <= 999:
        raise PixelLabUnavailable(
            f"init_image_strength must be 1-999, got {init_image_strength!r}"
        )
    if skeleton_guidance_scale is not None:
        low, high = SKELETON_GUIDANCE_RANGE
        if not low <= skeleton_guidance_scale <= high:
            raise PixelLabUnavailable(
                f"skeleton_guidance_scale must be {low:g}-{high:g}, "
                f"got {skeleton_guidance_scale!r}"
            )

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload: dict[str, Any] = {
        "description": prompt,
        "image_size": {"width": width, "height": height},
        "no_background": no_background,
        "text_guidance_scale": text_guidance_scale,
    }
    if style_image is not None:
        # Undocumented and answered with a 500, not a 422: the style image must
        # be exactly the requested canvas. Measured 2026-08-22 — a 128x256
        # reference against a 32x64 request returned
        # ``style_image must be size (64, 32), not torch.Size([256, 128])``.
        # Stored sprites are upscaled copies of their generated canvas, so this
        # is normally an exact integer downscale back to the pixels the
        # reference was drawn at. A reference of another shape — concept art a
        # human drew at whatever size suited them — is padded rather than
        # stretched; see ``_fit_to_canvas``.
        style_image = _fit_to_canvas(style_image, width, height)
        payload["style_image"] = {
            "type": "base64",
            "base64": _image_b64(style_image),
            "format": "png",
        }
        # Only defaulted when there is a reference to weigh: sending a strength
        # with no style image would claim an influence that does not exist.
        payload["style_strength"] = (
            BITFORGE_BALANCED_STYLE_STRENGTH if style_strength is None else style_strength
        )
    elif style_strength is not None:
        payload["style_strength"] = style_strength
    if init_image is not None:
        init_image = _fit_to_canvas(init_image, width, height)
        payload["init_image"] = {
            "type": "base64",
            "base64": _image_b64(init_image),
            "format": "png",
        }
        if init_image_strength is not None:
            payload["init_image_strength"] = init_image_strength
    if negative_description.strip():
        payload["negative_description"] = negative_description.strip()
    if coverage_percentage is not None:
        payload["coverage_percentage"] = coverage_percentage
    if oblique_projection:
        payload["oblique_projection"] = True
    if isometric:
        payload["isometric"] = True
    if direction is not None:
        payload["direction"] = direction
    if skeleton_keypoints:
        payload["skeleton_keypoints"] = _skeleton_points(skeleton_keypoints)
        payload["skeleton_guidance_scale"] = (
            DEFAULT_SKELETON_GUIDANCE
            if skeleton_guidance_scale is None
            else skeleton_guidance_scale
        )
    elif skeleton_guidance_scale is not None:
        payload["skeleton_guidance_scale"] = skeleton_guidance_scale
    if seed is not None:
        payload["seed"] = seed
    if forced_palette:
        payload["color_image"] = {
            "type": "base64",
            "base64": _palette_swatch_b64(forced_palette),
            "format": "png",
        }
    for field, value in (
        ("outline", outline),
        ("shading", shading),
        ("detail", detail),
        ("view", view),
    ):
        if value is not None:
            payload[field] = value

    try:
        response = httpx.post(
            f"{BASE_URL}{_BITFORGE_PATH}",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise PixelLabUnavailable(f"PixelLab bitforge request failed: {exc}") from exc

    try:
        image = _decode(data["image"]["base64"])
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise PixelLabUnavailable(f"malformed PixelLab bitforge response: {exc}") from exc

    return image, dict(data.get("usage") or {})


def generate_with_style(
    *,
    prompt: str,
    style_images: list[Image.Image],
    style_description: str,
    seed: int,
    poll_seconds: float = 5.0,
    max_polls: int = 60,
) -> tuple[list[Image.Image], dict[str, Any], str]:
    """Generate a variation set through the style-reference REST endpoint.

    There is no output-size argument. ``GenerateWithStyleV2Request.image_size``
    is marked ``deprecated`` with the description "REMOVED. Output size is
    deduced from the style images." — so this neither sends a size nor checks
    the returned one against a size it never asked for. The caller records the
    size that actually came back.
    """

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")
    if not 1 <= len(style_images) <= 4:
        raise PixelLabUnavailable("style_images must contain between 1 and 4 images")

    # Scaled by one factor per image, so this path never squashes a reference
    # and needs no padding: the endpoint deduces the output size from what it
    # is given rather than demanding a canvas. Only bitforge has to match an
    # exact canvas, which is why ``_fit_to_canvas`` lives on that path alone.
    normalized_style_images = []
    for image in style_images:
        scale = min(1.0, STYLE_IMAGE_MAX_SIDE / max(image.size))
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        normalized_style_images.append(
            image if size == image.size else image.resize(size, Image.Resampling.NEAREST)
        )

    payload = {
        "style_images": [
            {
                "image": {"type": "base64", "base64": _image_b64(image)},
                "width": image.width,
                "height": image.height,
            }
            for image in normalized_style_images
        ],
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
    return images, dict(usage), job_id


def _reject_style_enums(endpoint: str, **values: str | None) -> None:
    """Fail before the request when a style value is wrong for this endpoint.

    PixelLab answers 422 with the offending field buried in a JSON body; a
    caller that passes a value from elsewhere (e.g. an ArtStyle whose
    ``camera_view`` is "side") otherwise learns that only after a round trip.
    The allowed set is per endpoint — see ``STYLE_ENUMS``.
    """

    allowed_by_field = STYLE_ENUMS[endpoint]
    for field, value in values.items():
        allowed = allowed_by_field.get(field)
        if allowed is None:
            # Not a wrong value — a field this endpoint does not declare at all
            # (``direction`` exists on the image endpoints and nowhere else).
            raise PixelLabUnavailable(f"/{endpoint} has no {field!r} field")
        if value is not None and value not in allowed:
            raise PixelLabUnavailable(
                f"{field}={value!r} is not accepted by /{endpoint}; use one of {allowed}"
            )


def _reject_text_guidance(value: float) -> None:
    """``text_guidance_scale`` is 1-20 on every endpoint that accepts it."""

    low, high = TEXT_GUIDANCE_RANGE
    if not low <= value <= high:
        raise PixelLabUnavailable(
            f"text_guidance_scale must be {low:g}-{high:g}, got {value!r}"
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
    color_palette: list[tuple[int, int, int]] | None = None,
    text_guidance_scale: float = DEFAULT_TEXT_GUIDANCE,
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

    ``tile_size`` is one of ``TILE_SIZES``, squared — ``TileSize`` is an enum,
    not a range, so 24 or 48 is a 422 even though both sit inside 16-64.
    ``color_palette`` is sent as ``color_image``: like pixflux, this endpoint
    has no array-of-colours field. ``usage`` is relayed verbatim as with
    ``generate_image``; measured it comes back ``null`` on the retrieval call,
    so callers must not assume a dict.
    """

    # Argument checks come before the key check: a wrong enum is wrong whether
    # or not the environment is configured, and reporting the env first hides it.
    # ``view`` is the one that differs from pixflux here — ``TilesetCameraView``
    # has no "side" — while outline/shading/detail share pixflux's wordings.
    _reject_style_enums(
        "tilesets", view=view, outline=outline, shading=shading, detail=detail
    )
    _reject_text_guidance(text_guidance_scale)
    if tile_size not in TILE_SIZES:
        raise PixelLabUnavailable(
            f"tile_size must be one of {TILE_SIZES}, got {tile_size}"
        )

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload: dict[str, Any] = {
        "lower_description": lower_description,
        "upper_description": upper_description,
        "tile_size": {"width": tile_size, "height": tile_size},
        "text_guidance_scale": text_guidance_scale,
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
    if color_palette:
        payload["color_image"] = {
            "type": "base64",
            "base64": _palette_swatch_b64(color_palette),
            "format": "png",
        }

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


def create_animation(
    *,
    first_frame: Image.Image,
    action: str,
    frame_count: int,
    description: str | None = None,
    poll_seconds: float = 5.0,
    max_polls: int = 60,
) -> tuple[list[Image.Image], dict[str, Any], str]:
    """Animate an approved first frame — returns ``(frames, usage, job_id)``.

    ``/animate-with-text-v3`` takes the frame the human already approved and
    continues the motion from it, so the approval gate that guards static
    sprites also guards every frame that follows.

    Asynchronous like the other v2/v3 endpoints: the POST answers with a
    ``background_job_id`` and the frames arrive under ``last_response.images``.
    PixelLab documents 30-180 seconds for a typical sequence, so the default
    poll budget is wider than a single image needs.

    ``frame_count`` must be one of :data:`ANIMATION_FRAME_COUNTS`. The endpoint
    answers 422 for an odd number ("frame_count must be an even number"), and it
    returns one image *more* than requested — the first frame is echoed back at
    the head of the sequence (measured 2026-08-15: 4 -> 5 images, 6 -> 7).

    ``no_background`` defaults to **false** here, unlike the static image call.
    Left alone it returns every frame on an opaque grey plate, which cannot be
    used as a sprite at all (measured 2026-08-15: 100% opaque, corner pixel
    ``(128, 128, 128, 255)``), so this always asks for the cut-out.
    """

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")
    if frame_count not in ANIMATION_FRAME_COUNTS:
        raise PixelLabUnavailable(
            f"frame_count must be one of {ANIMATION_FRAME_COUNTS}"
        )
    if not action.strip():
        raise PixelLabUnavailable("action must not be empty")
    if max(first_frame.size) > 512:
        raise PixelLabUnavailable("first frame dimensions must not exceed 512 pixels")

    payload: dict[str, Any] = {
        "first_frame": {"type": "base64", "base64": _image_b64(first_frame)},
        "action": action.strip(),
        "frame_count": frame_count,
        # Defaults to false, which returns frames painted onto a flat grey plate.
        # A sprite needs the alpha the static endpoints already ask for.
        "no_background": True,
    }
    if description and description.strip():
        payload["description"] = description.strip()

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            created = client.post(
                f"{BASE_URL}/animate-with-text-v3", json=payload, headers=headers
            )
            if created.status_code >= 400:
                raise PixelLabUnavailable(
                    f"PixelLab rejected the animation request ({created.status_code}): "
                    f"{created.text[:400]}"
                )
            created_data = created.json()
            job_id = created_data.get("background_job_id")
            if not job_id:
                raise PixelLabUnavailable("PixelLab returned no background_job_id")

            data: dict[str, Any] | None = None
            for _ in range(max_polls):
                candidate = _poll_json(
                    client,
                    f"{BASE_URL}/background-jobs/{job_id}",
                    headers,
                    poll_seconds=poll_seconds,
                    max_polls=1,
                )
                status = candidate.get("status")
                if status == "failed":
                    raise PixelLabUnavailable(
                        f"PixelLab animation job failed: {candidate.get('last_response')!r}"
                    )
                if status == "completed":
                    data = candidate
                    break
                time.sleep(poll_seconds)
            if data is None:
                raise PixelLabUnavailable(
                    f"animation job {job_id} not ready after {max_polls * poll_seconds:.0f}s"
                )
    except httpx.HTTPError as exc:
        raise PixelLabUnavailable(f"PixelLab animation request failed: {exc}") from exc

    response = data.get("last_response") or {}
    response_dict = response if isinstance(response, dict) else {}
    raw_images = response_dict.get("images")
    encoded_frames: list[str] = []
    if isinstance(raw_images, list):
        for item in raw_images:
            encoded = _encoded_image(item)
            if encoded:
                encoded_frames.append(encoded)
    if not encoded_frames:
        raise PixelLabUnavailable("malformed PixelLab animation response: no frames")

    frames = [_decode(encoded) for encoded in encoded_frames]
    usage = data.get("usage") or created_data.get("usage") or response_dict.get("usage") or {}
    return frames, dict(usage), str(job_id)


#: ``CreateMapObjectRequest.image_size`` starts at 32, not at pixflux's 16.
MAP_OBJECT_SIDE_RANGE = (32, 400)


def create_map_object(
    *,
    description: str,
    width: int = 32,
    height: int = 32,
    color_palette: list[tuple[int, int, int]] | None = None,
    view: str | None = None,
    outline: str | None = None,
    shading: str | None = None,
    detail: str | None = None,
    text_guidance_scale: float = DEFAULT_TEXT_GUIDANCE,
    poll_seconds: float = 5.0,
    max_polls: int = 24,
) -> tuple[Image.Image, dict[str, Any]]:
    """Generate one transparent map decoration — returns ``(image, usage)``.

    Scattered on a layer above the floor, these are what break up a tiled
    ground without needing a variant for every cell.

    The style arguments are what keep a decoration in the same game as the
    rest: without them this endpoint applies its own defaults, and ``view``
    defaults to "high top-down" — so a side-view game used to get its props
    drawn from above. Their allowed values are this endpoint's own and are
    narrower than pixflux's (see ``STYLE_ENUMS``), which is why the caller
    must map a locked ``ArtStyle`` onto them rather than pass it through.

    ``color_palette`` is sent as ``color_image``; there is no
    ``color_palette`` field in ``CreateMapObjectRequest``, so the string this
    used to send was silently dropped or rejected.

    The finished object comes back as a ``download_url``, not base64, and
    PixelLab deletes it 8 hours after creation — so this fetches the bytes
    immediately rather than handing the URL to the caller.
    """

    _reject_style_enums(
        "map-objects", view=view, outline=outline, shading=shading, detail=detail
    )
    _reject_text_guidance(text_guidance_scale)
    low, high = MAP_OBJECT_SIDE_RANGE
    if not (low <= width <= high and low <= height <= high):
        raise PixelLabUnavailable(
            f"map object size must be {low}-{high}px per side, got {width}x{height}"
        )

    api_key = os.getenv("PIXELLAB_API_KEY")
    if not api_key:
        raise PixelLabUnavailable("PIXELLAB_API_KEY not set")

    payload: dict[str, Any] = {
        "description": description,
        "image_size": {"width": width, "height": height},
        "text_guidance_scale": text_guidance_scale,
    }
    for field, value in (
        ("view", view),
        ("outline", outline),
        ("shading", shading),
        ("detail", detail),
    ):
        if value is not None:
            payload[field] = value
    if color_palette:
        payload["color_image"] = {
            "type": "base64",
            "base64": _palette_swatch_b64(color_palette),
            "format": "png",
        }

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
