"""AssetGenMcpServer — PixelLab 2D asset boundary for Unity games.

Design decisions worth knowing before editing:

* **PixelLab only, no fallback.** Generation requires ``PIXELLAB_API_KEY``.
  A missing key or a failed call is an MCP error (code 3000), not a
  silent degrade to placeholder art (see ``_generate_image``). ``render.py``
  keeps only the prompt classifier and the deterministic seed derivation
  PixelLab's call depends on — it no longer draws pixels itself.
* **Generation enters review as pending.** ``generate_2d_sprite`` records its
  output as ``pending``. Human review is metadata; whether approval gates a
  build remains the host's policy decision — see ``docs/contracts.md``.
* **An asset's path never changes.** Review status lives in the manifest, not
  in the directory name. Files used to be moved into ``pending/``,
  ``approved/`` or ``rejected/`` as they were reviewed — but
  the host can hand ``assetPath`` to UnityMcpServer's ``import_asset`` before
  review metadata changes, so moving the file would invalidate that path.
* **Return type is ``dict[str, Any]``.** A bare ``dict`` leaves
  ``structuredContent`` empty; see ``common/server.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

from PIL import Image

from common.env import REPO_ROOT
from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error
from common.server import build, expects_dict_return, serve

from . import pixellab_client, prompting, quality, render
from .style import load_or_create

logger = logging.getLogger(__name__)

mcp = build("AssetGenMcpServer")

DEFAULT_ASSET_ROOT = REPO_ROOT / "var" / "assets"


def _configured_asset_root(value: str | None = None) -> Path:
    """Resolve relative asset roots from the repository, never the MCP cwd."""

    configured = Path(value or os.getenv("ASSET_ROOT", str(DEFAULT_ASSET_ROOT)))
    if not configured.is_absolute():
        configured = REPO_ROOT / configured
    return configured.resolve()


ROOT = _configured_asset_root()
DEFAULT_ART_STYLE = "pixel art"
_IDENTIFIER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?")

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

# Upscale factor applied to PixelLab's native output, so every sprite ends at
# generation_size * 4 for crisp Point-filtered display.
#
# Measured 2026-08-02 (prompt-eval, slime-rancher-roguelite, round 5): asking
# PixelLab to generate at 128px then LANCZOS-downsampling to style.pixel_grid
# (the earlier approach) reads as blurred/anti-aliased once upscaled — LANCZOS
# is an interpolating filter, so the downsample step itself smears pixel
# edges. Requesting the native grid size directly (no downsample) scored the
# same or better on subject quality and fixed the blur. Do not reintroduce a
# generate-big-then-downsample step for PixelLab.
_PIXELLAB_UPSCALE = 4

# Width/height ratio (relative to style.pixel_grid) PixelLab is asked to
# generate at, per kind — a fixed square for every asset ignored how
# differently sized things actually read in-game (measured: a humanoid
# character cropped shoulder-to-knee at a 1:1 ratio). ``tile`` stays 1:1
# deliberately — a non-square tile cannot lay edge to edge seamlessly.
_KIND_SIZE_RATIO: dict[str, tuple[float, float]] = {
    # 2.0, not 1.5: at 32x48 a full-body humanoid came out cropped below the
    # thigh (measured, prompt-eval round 11, two of two samples). 32x64 fits
    # head to feet and scored 8/10 against 7/10 for 32x56, which still lost a
    # wrist (round 11b). Width stays at the grid — see _PIXELLAB_UPSCALE.
    "character": (1.0, 2.0),
    # Deliberately square, not the character ratio — a round creature (a
    # slime) distorted when stretched into the humanoid's tall canvas
    # (measured, prompt-eval round 6, scored 4/10 vs. 8/10 square).
    "monster": (1.0, 1.0),
    "tile": (1.0, 1.0),
    "prop": (1.0, 1.0),
    "icon": (1.0, 1.0),
    "ui_button": (2.0, 1.0),
    "ui_panel": (3.0, 2.0),
}


def _size_for(style: Any, kind: render.AssetKind) -> tuple[int, int]:
    """Native PixelLab generation size for this kind, derived from the game's grid."""

    width_ratio, height_ratio = _KIND_SIZE_RATIO.get(kind, (1.0, 1.0))
    return int(style.pixel_grid * width_ratio), int(style.pixel_grid * height_ratio)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manifest_path(game_id: str) -> Path:
    return _root_path("manifests", f"{game_id}.json")


def _asset_path(game_id: str, feature_id: str, kind: str, prompt_digest: str) -> Path:
    """Where an asset lives — for its whole life, regardless of review status.

    Deliberately not segmented by status: Unity imports this exact path, so a
    review decision must not move the file out from under it. ``prompt_digest``
    keys the filename because one feature can name several same-kind assets
    (e.g. unityHints.assetsNeeded = ["무기 아이콘", "방어구 아이콘"]) — without it,
    the second sprite silently overwrote the first on disk and in the manifest.
    """

    return _root_path("assets", game_id, f"{feature_id}_{kind}_{prompt_digest}.png")


def _root_path(*parts: str) -> Path:
    """Build a path that cannot escape the configured asset root."""

    path = ROOT.joinpath(*parts).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise tool_error(VALIDATION_ERROR, "asset path must stay within ASSET_ROOT") from exc
    return path


def _load_manifest(game_id: str) -> dict[str, Any]:
    path = _manifest_path(game_id)
    if not path.exists():
        return {"game_id": game_id, "assets": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(manifest: dict[str, Any]) -> None:
    path = _manifest_path(manifest["game_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _require(value: str, field: str) -> str:
    """Validation errors carry code 1000, not the generic 3000."""

    if not value or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value.strip()


def _require_identifier(value: str, field: str) -> str:
    """Accept one safe path segment for identifiers used in asset filenames."""

    normalized = _require(value, field)
    if not _IDENTIFIER.fullmatch(normalized) or "__" in normalized:
        raise tool_error(
            VALIDATION_ERROR,
            f"{field} must use letters, digits, hyphens, or single underscores",
        )
    return normalized


def _resolve_game_id(explicit: str | None) -> str:
    """Resolve which game an asset belongs to.

    Legacy callers can omit ``gameId``, so
    an explicit ``gameId`` is accepted where callers can supply one, and
    otherwise the server falls back to a single shared project.

    That fallback is *measurably* lossy and callers should always pass
    ``gameId``: with it omitted, two different games both resolve to
    ``"default"``, share one seed, one palette and one manifest, and — because
    Strategic AI reuses feature ids like ``f-1`` — the second game's assets
    silently overwrite the first game's, on disk and in the manifest. Verified
    by reproducing the orchestrator's current call shape.
    """

    if explicit and explicit.strip():
        return _require_identifier(explicit, "gameId")
    return _require_identifier(os.getenv("ASSET_DEFAULT_GAME_ID", "default"), "gameId")


def _asset_kind(value: str | None) -> render.AssetKind | None:
    """Validate an optional explicit kind before prompt classification."""

    if value is None:
        return None
    normalized = _require(value, "assetKind")
    if normalized not in get_args(render.AssetKind):
        raise tool_error(VALIDATION_ERROR, f"unsupported assetKind: {normalized}")
    return normalized  # type: ignore[return-value]


def _pixellab_provenance(
    *, kind: render.AssetKind, seed: int, feature_id: str, prompt: str, usage: dict[str, Any]
) -> dict[str, Any]:
    """Audit record for a PixelLab-generated asset.

    ``usage`` is PixelLab's own report, stored verbatim: v2 bills in credits
    (``{"type": "generations", "generations": 1.0}``, measured 2026-08-03) and
    never returns a dollar figure, so recording a ``cost_usd`` here would be
    inventing one. ``commercial_use`` is left to PixelLab's own terms of
    service rather than claimed here — this repository has no way to verify it.
    """

    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    return {
        "method": "pixellab",
        "generator": "AssetGenMcpServer/pixellab_client.py",
        "kind": kind,
        "endpoint": "generate-image-pixflux",
        "derived_from": f"seed={seed} feature={feature_id} prompt_sha256={prompt_digest}",
        "usage": usage,
        "commercial_use": "see PixelLab terms of service",
    }


def _pixellab_palette(
    style: Any, kind: render.AssetKind, prompt: str
) -> list[tuple[int, int, int]] | None:
    """The locked ``ArtStyle``'s ramp for this kind, as PixelLab's ``forced_palette``.

    PixelLab has no notion of "this game's look" between calls — each request
    is stateless. Reusing the game's own ramps (rather than inventing a
    second palette scheme) is what makes a PixelLab tile and a PixelLab icon
    from the same game share a hue family instead of each call picking its
    own colours.

    ``character``/``monster`` get no forced palette (``None``) — living
    things need enough colour range to read distinct materials (skin, cloth,
    fur) that a 5-7 swatch game ramp cannot cover. Measured (prompt-eval
    2026-08-02, rounds 7-9): the locked ramp read as "too green, no
    character" (5/10); dropping it entirely scored no worse (7/10, tied with
    keeping it) while giving the subject actual colour variety. Tile/prop/UI
    keep the lock — nothing there complained about the palette, and it's what
    stops two props in the same game from reading as unrelated (12문서 §10-7).
    """

    if kind in ("character", "monster"):
        return None
    if kind in ("tile", "prop"):
        material = render.material_for(prompt, kind)
        # Metal terms commonly name a visible colour (brass, copper, gold),
        # while the shared metal ramp is deliberately blue-grey. Do not let
        # that generic ramp override the host-authored material intent.
        if material in (None, "metal"):
            return None
        ramp = style.material_ramp(material)
        return [
            ramp["shadow"],
            ramp["base"],
            ramp["light"],
            ramp["highlight"],
            style.outline_for(material),
        ]
    return [
        style.rgb("ui_surface"),
        style.rgb("ui_text"),
        style.rgb("accent"),
        style.rgb("outline"),
    ]


def _images_generated(usage: dict[str, Any]) -> int:
    """How many images this call consumed from the account's monthly quota.

    PixelLab bills in ``generations`` — one per image (measured 2026-08-03) —
    and never returns dollars. Under a subscription the dollar figure would be
    an allocation (monthly fee / monthly quota) rather than what the call
    cost: the marginal cost of one more image is zero until the quota runs
    out. The image count is the number that actually depletes.
    """

    try:
        return int(float(usage.get("generations") or 0))
    except (TypeError, ValueError):
        return 0


def _pixellab_style_params(style: Any, kind: render.AssetKind) -> dict[str, str]:
    """PixelLab's structured style controls (12문서 §10-7 prompting eval).

    Confirmed enums, not free text — stuffing style words into the
    description competes with the subject for the model's attention; these
    fields don't. ``view`` comes from the locked ``ArtStyle`` (not per-kind),
    the same way the palette is locked: a game mixing camera angles per asset
    call reads as broken.

    ``shading`` is the one axis that had to differ by kind — "medium shading"
    reads fine on a character or creature body, but the same setting made a
    boxy prop (a treasure chest) look like a 3D render instead of flat pixel
    art (measured, scored 3/5). Flattened for anything that isn't a
    character or monster.
    """

    return {
        "outline": "single color black outline",
        "shading": "medium shading" if kind in ("character", "monster") else "flat shading",
        "detail": "medium detail",
        "view": style.camera_view,
    }


def _generate_image(
    style: Any, kind: render.AssetKind, rng: Any, prompt: str, feature_id: str
) -> tuple[Image.Image, dict[str, Any]]:
    """PixelLab only. A missing key or a failed call is a tool error.

    No placeholder-art fallback: generation either comes from PixelLab or it
    fails loudly, so a broken key never silently ships mismatched art.
    """

    if not pixellab_client.is_configured():
        raise tool_error(
            MCP_ERROR,
            "PIXELLAB_API_KEY is not set; asset generation requires PixelLab",
            featureId=feature_id,
        )

    width, height = _size_for(style, kind)

    try:
        seed = rng.getrandbits(32)
        image, usage = pixellab_client.generate_image(
            prompt=prompt,
            width=width,
            height=height,
            seed=seed,
            forced_palette=_pixellab_palette(style, kind, prompt),
            **_pixellab_style_params(style, kind),
        )
    except pixellab_client.PixelLabUnavailable as exc:
        raise tool_error(
            MCP_ERROR, f"PixelLab generation failed: {exc}", featureId=feature_id
        ) from exc

    # No downsample: PixelLab already generated at the native target size, so
    # this is a pure crisp upscale, not a smoothing round-trip.
    image = image.resize(
        (width * _PIXELLAB_UPSCALE, height * _PIXELLAB_UPSCALE),
        Image.NEAREST,
    )
    return image, _pixellab_provenance(
        kind=kind,
        seed=seed,
        feature_id=feature_id,
        prompt=prompt,
        usage=usage,
    )


def _generate(
    feature_id: str,
    prompt: str,
    game_id: str | None,
    forced_kind: render.AssetKind | None = None,
    art_style: str | None = None,
) -> dict[str, Any]:
    feature_id = _require_identifier(feature_id, "featureId")
    prompt = _require(prompt, "prompt")
    resolved_game = _resolve_game_id(game_id)

    style = load_or_create(
        ROOT, resolved_game, art_style or os.getenv("ASSET_ART_STYLE", DEFAULT_ART_STYLE)
    )
    kind = forced_kind or render.classify(prompt)
    prompt_plan = prompting.compose(prompt, kind)
    rng = render.rng_for(style, feature_id, prompt)

    image, provenance = _generate_image(style, kind, rng, prompt_plan.prompt, feature_id)
    provenance["prompt"] = prompt_plan.metadata()

    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
    asset_id = f"{resolved_game}__{feature_id}__{kind}__{prompt_digest}"
    out_path = _asset_path(resolved_game, feature_id, kind, prompt_digest)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)

    manifest = _load_manifest(resolved_game)
    manifest["assets"][asset_id] = {
        "asset_id": asset_id,
        "feature_id": feature_id,
        "kind": kind,
        "prompt": prompt,
        "provider_prompt": prompt_plan.prompt,
        "status": PENDING,
        "asset_path": str(out_path),
        "created_at": _now(),
        "reviewed_at": None,
        "review_note": None,
        "provenance": provenance,
    }
    _save_manifest(manifest)

    result = {
        "assetPath": str(out_path),
        "assetId": asset_id,
        "kind": kind,
        "gameId": resolved_game,
        "status": PENDING,
        "styleSeed": style.seed,
        "generatedBy": provenance["method"],
        "promptMetrics": prompt_plan.metadata(),
    }
    # PixelLab charges per image against a monthly quota; token-usage
    # accounting (common/usage.py) is Anthropic-specific and does not apply
    # here (12문서 §7). Reporting the image count keeps that consumption
    # visible instead of landing in neither ledger — see _images_generated for
    # why the unit is images rather than dollars.
    images = _images_generated(provenance.get("usage") or {})
    if images:
        result["imagesGenerated"] = images
    return result


def _generate_prototype(
    feature_id: str,
    prompt: str,
    game_id: str | None,
    forced_kind: render.AssetKind | None = None,
    art_style: str | None = None,
) -> dict[str, Any]:
    """Generate the reviewable style prototype through PixelLab's official MCP."""

    feature_id = _require_identifier(feature_id, "featureId")
    prompt = _require(prompt, "prompt")
    resolved_game = _resolve_game_id(game_id)
    style = load_or_create(
        ROOT, resolved_game, art_style or os.getenv("ASSET_ART_STYLE", DEFAULT_ART_STYLE)
    )
    kind = forced_kind or render.classify(prompt)
    prompt_plan = prompting.compose(prompt, kind)
    width, height = _size_for(style, kind)
    seed = render.rng_for(style, feature_id, prompt).getrandbits(32)
    palette_rgb = _pixellab_palette(style, kind, prompt)
    palette = [f"#{red:02x}{green:02x}{blue:02x}" for red, green, blue in palette_rgb or []]

    try:
        image, usage, tool_name = pixellab_client.generate_prototype(
            prompt=prompt_plan.prompt,
            width=width,
            height=height,
            kind=kind,
            seed=seed,
            style_description=style.art_style,
            style_params=_pixellab_style_params(style, kind),
            palette=palette,
        )
    except pixellab_client.PixelLabUnavailable as exc:
        raise tool_error(
            MCP_ERROR, f"PixelLab MCP prototype failed: {exc}", featureId=feature_id
        ) from exc

    target_size = (width * _PIXELLAB_UPSCALE, height * _PIXELLAB_UPSCALE)
    if image.size != target_size:
        image = image.resize(target_size, Image.NEAREST)

    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
    asset_id = f"{resolved_game}__{feature_id}__{kind}__{prompt_digest}"
    out_path = _asset_path(resolved_game, feature_id, kind, prompt_digest)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)

    provenance = {
        "method": "pixellab-mcp",
        "generator": "https://api.pixellab.ai/mcp",
        "tool": tool_name,
        "kind": kind,
        "derived_from": f"seed={seed} feature={feature_id}",
        "usage": usage,
        "prompt": prompt_plan.metadata(),
        "expected_size": list(target_size),
        "commercial_use": "see PixelLab terms of service",
    }
    manifest = _load_manifest(resolved_game)
    manifest["assets"][asset_id] = {
        "asset_id": asset_id,
        "feature_id": feature_id,
        "kind": kind,
        "prompt": prompt,
        "provider_prompt": prompt_plan.prompt,
        "status": PENDING,
        "asset_path": str(out_path),
        "created_at": _now(),
        "reviewed_at": None,
        "review_note": None,
        "provenance": provenance,
    }
    _save_manifest(manifest)
    result = {
        "assetPath": str(out_path),
        "assetId": asset_id,
        "kind": kind,
        "gameId": resolved_game,
        "status": PENDING,
        "workflowStage": "prototype",
        "styleSeed": style.seed,
        "generatedBy": provenance["method"],
        "promptMetrics": prompt_plan.metadata(),
    }
    images = _images_generated(usage)
    result["imagesGenerated"] = images or 1
    return result


# --------------------------------------------------------------------------
# Agent-first asset tools
# --------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Collect a complete asset brief and return a deterministic, kind-aware prompt. "
        "This preflight does not generate an image or call a model."
    )
)
@expects_dict_return
def prepare_asset_prompt(
    assetKind: str,
    subject: str = "",
    purpose: str = "",
    composition: str = "",
    mustHave: list[str] | None = None,
    avoid: list[str] | None = None,
    artStyle: str = "",
    isRevision: bool = False,
    preserve: list[str] | None = None,
    change: list[str] | None = None,
) -> dict[str, Any]:
    kind = _asset_kind(assetKind)
    assert kind is not None
    return prompting.prepare(
        kind,
        subject=subject,
        purpose=purpose,
        composition=composition,
        must_have=mustHave,
        avoid=avoid,
        art_style=artStyle,
        is_revision=isRevision,
        preserve=preserve,
        change=change,
    )


@mcp.tool(
    description=(
        "Generate the initial 2D style prototype through PixelLab's official MCP. "
        "Approve it before requesting API variations."
    )
)
@expects_dict_return
def generate_2d_sprite(
    featureId: str,
    prompt: str,
    gameId: str | None = None,
    artStyle: str | None = None,
    assetKind: str | None = None,
) -> dict[str, Any]:
    return _generate_prototype(
        featureId,
        prompt,
        gameId,
        forced_kind=_asset_kind(assetKind),
        art_style=artStyle,
    )


@mcp.tool(description="Generate an initial UI prototype through PixelLab's official MCP.")
@expects_dict_return
def generate_ui_asset(
    featureId: str,
    prompt: str,
    gameId: str | None = None,
    artStyle: str | None = None,
    assetKind: str | None = None,
) -> dict[str, Any]:
    kind = _asset_kind(assetKind) or render.classify(prompt)
    if not kind.startswith("ui_") and kind != "icon":
        if assetKind is not None:
            raise tool_error(VALIDATION_ERROR, "generate_ui_asset requires a UI assetKind")
        kind = "ui_panel"  # this tool always produces UI, whatever the wording
    return _generate_prototype(featureId, prompt, gameId, forced_kind=kind, art_style=artStyle)


@mcp.tool(
    description=(
        "Generate many same-kind variations through PixelLab's REST API, using an approved "
        "MCP prototype as the shared style reference."
    )
)
@expects_dict_return
def generate_2d_variations(
    featureId: str,
    prototypeAssetId: str,
    prompts: list[str],
    gameId: str | None = None,
    styleAssetIds: list[str] | None = None,
) -> dict[str, Any]:
    """Expand an approved MCP prototype using up to four approved style anchors."""

    feature_id = _require_identifier(featureId, "featureId")
    prototype_id = _require(prototypeAssetId, "prototypeAssetId")
    if not 1 <= len(prompts) <= 25:
        raise tool_error(VALIDATION_ERROR, "prompts must contain between 1 and 25 items")

    prototype_game = prototype_id.split("__", 1)[0]
    resolved_game = _resolve_game_id(gameId) if gameId else prototype_game
    if resolved_game != prototype_game:
        raise tool_error(VALIDATION_ERROR, "gameId must match the prototype asset")

    manifest = _load_manifest(resolved_game)
    prototype = manifest["assets"].get(prototype_id)
    if prototype is None:
        raise tool_error(VALIDATION_ERROR, f"unknown prototypeAssetId: {prototype_id}")
    if prototype["status"] != APPROVED:
        raise tool_error(VALIDATION_ERROR, "prototype asset must be approved before batching")
    if (prototype.get("provenance") or {}).get("method") != "pixellab-mcp":
        raise tool_error(VALIDATION_ERROR, "prototype asset must come from PixelLab's official MCP")

    style_asset_ids = list(dict.fromkeys([prototype_id, *(styleAssetIds or [])]))
    if not 1 <= len(style_asset_ids) <= 4:
        raise tool_error(
            VALIDATION_ERROR,
            "prototypeAssetId plus styleAssetIds must contain between 1 and 4 unique assets",
        )
    style_records: list[dict[str, Any]] = []
    for style_asset_id in style_asset_ids:
        if style_asset_id.split("__", 1)[0] != resolved_game:
            raise tool_error(VALIDATION_ERROR, "every style asset must belong to gameId")
        style_record = manifest["assets"].get(style_asset_id)
        if style_record is None:
            raise tool_error(VALIDATION_ERROR, f"unknown style asset: {style_asset_id}")
        if style_record["status"] != APPROVED:
            raise tool_error(VALIDATION_ERROR, "every style asset must be approved")
        if (style_record.get("provenance") or {}).get("method") != "pixellab-mcp":
            raise tool_error(
                VALIDATION_ERROR, "every style asset must come from PixelLab's official MCP"
            )
        style_records.append(style_record)

    kind = prototype["kind"]
    if kind not in _KIND_SIZE_RATIO:
        raise tool_error(VALIDATION_ERROR, f"unsupported prototype kind: {kind}")
    cleaned_prompts = [
        _require(prompt, f"prompts[{index}]") for index, prompt in enumerate(prompts)
    ]
    style_images: list[Image.Image] = []
    for style_record in style_records:
        style_path = Path(style_record["asset_path"])
        if not style_path.is_file():
            raise tool_error(VALIDATION_ERROR, f"style asset file is missing: {style_path}")
        with Image.open(style_path) as opened:
            style_images.append(opened.convert("RGBA").copy())
    output_size = style_images[0].size
    if output_size[0] != output_size[1]:
        raise tool_error(
            VALIDATION_ERROR,
            "generate-with-style-v2 requires a square primary prototype; "
            "use a provider-specific character workflow for non-square assets",
        )

    style = load_or_create(ROOT, resolved_game, DEFAULT_ART_STYLE)
    batch_digest = hashlib.sha256(
        f"{'|'.join(style_asset_ids)}|{'|'.join(cleaned_prompts)}".encode()
    ).hexdigest()[:8]
    batch_id = f"{resolved_game}__{feature_id}__batch__{batch_digest}"
    out_dir = ROOT / "assets" / resolved_game / "variations" / f"{feature_id}_{batch_digest}"
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    prompt_metrics: list[dict[str, object]] = []
    style_description = f"{style.art_style}; {style.camera_view}; consistent with the reference"
    for prompt_index, prompt in enumerate(cleaned_prompts):
        plan = prompting.compose(prompt, kind)
        seed = render.rng_for(style, f"{feature_id}:{prompt_index}", prompt).getrandbits(32)
        try:
            images, usage, job_id = pixellab_client.generate_with_style(
                prompt=plan.prompt,
                style_images=style_images,
                output_size=output_size,
                style_description=style_description,
                seed=seed,
            )
        except pixellab_client.PixelLabUnavailable as exc:
            raise tool_error(
                MCP_ERROR,
                f"PixelLab API variation batch failed: {exc}",
                featureId=feature_id,
            ) from exc

        prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        metrics = {"promptIndex": prompt_index, **plan.metadata()}
        prompt_metrics.append(metrics)
        jobs.append({"jobId": job_id, "usage": usage, "promptMetrics": metrics})
        for candidate_index, image in enumerate(images):
            asset_id = (
                f"{resolved_game}__{feature_id}__{kind}__{prompt_digest}__"
                f"{prompt_index}__{candidate_index}"
            )
            path = out_dir / f"{prompt_index:02d}_{candidate_index:02d}_{prompt_digest}.png"
            image.save(path)
            record = {
                "asset_id": asset_id,
                "feature_id": feature_id,
                "kind": kind,
                "prompt": prompt,
                "provider_prompt": plan.prompt,
                "status": PENDING,
                "asset_path": str(path),
                "created_at": _now(),
                "reviewed_at": None,
                "review_note": None,
                "prototype_asset_id": prototype_id,
                "batch_id": batch_id,
                "provenance": {
                    "method": "pixellab-api",
                    "endpoint": "generate-with-style-v2",
                    "job_id": job_id,
                    "candidate": candidate_index,
                    "seed": seed,
                    "style_asset_ids": style_asset_ids,
                    "expected_size": list(output_size),
                    "usage": usage,
                    "prompt": plan.metadata(),
                    "commercial_use": "see PixelLab terms of service",
                },
            }
            manifest["assets"][asset_id] = record
            records.append(
                {
                    "assetId": asset_id,
                    "assetPath": str(path),
                    "promptIndex": prompt_index,
                    "candidate": candidate_index,
                    "status": PENDING,
                }
            )

    index_path = out_dir / "batch.json"
    index_path.write_text(
        json.dumps(
            {
                "batchId": batch_id,
                "prototypeAssetId": prototype_id,
                "styleAssetIds": style_asset_ids,
                "kind": kind,
                "assets": records,
                "jobs": jobs,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _save_manifest(manifest)
    return {
        "batchId": batch_id,
        "gameId": resolved_game,
        "prototypeAssetId": prototype_id,
        "styleAssetIds": style_asset_ids,
        "kind": kind,
        "status": PENDING,
        "workflowStage": "variations",
        "indexPath": str(index_path),
        "assets": records,
        "imagesGenerated": len(records),
        "promptMetrics": prompt_metrics,
    }


@mcp.tool(
    description=(
        "Animate an approved 2D prototype into an ordered frame sequence through PixelLab. "
        "The approved prototype is the first frame, so the human gate still holds."
    )
)
@expects_dict_return
def generate_2d_animation(
    featureId: str,
    firstFrameAssetId: str,
    action: str,
    gameId: str | None = None,
    frameCount: int = 8,
    description: str = "",
) -> dict[str, Any]:
    """Turn one approved sprite into an ordered motion sequence.

    The sequence is one review unit: half an approved walk cycle cannot be
    played, so the frames share a status the way a tileset does.
    """

    feature_id = _require_identifier(featureId, "featureId")
    first_frame_id = _require(firstFrameAssetId, "firstFrameAssetId")
    motion = _require(action, "action")
    if not 2 <= frameCount <= 16:
        raise tool_error(VALIDATION_ERROR, "frameCount must be between 2 and 16")

    source_game = first_frame_id.split("__", 1)[0]
    resolved_game = _resolve_game_id(gameId) if gameId else source_game
    if resolved_game != source_game:
        raise tool_error(VALIDATION_ERROR, "gameId must match the first frame asset")

    manifest = _load_manifest(resolved_game)
    source = manifest["assets"].get(first_frame_id)
    if source is None:
        raise tool_error(VALIDATION_ERROR, f"unknown firstFrameAssetId: {first_frame_id}")
    if source["status"] != APPROVED:
        raise tool_error(
            VALIDATION_ERROR, "the first frame asset must be approved before animating"
        )

    source_path = Path(source["asset_path"])
    if not source_path.is_file():
        raise tool_error(VALIDATION_ERROR, f"first frame file is missing: {source_path}")
    with Image.open(source_path) as opened:
        first_frame = opened.convert("RGBA").copy()

    if not pixellab_client.is_configured():
        raise tool_error(
            MCP_ERROR,
            "PIXELLAB_API_KEY is not set; animation requires PixelLab",
            featureId=feature_id,
        )

    try:
        frames, usage, job_id = pixellab_client.create_animation(
            first_frame=first_frame,
            action=motion,
            frame_count=frameCount,
            description=description or None,
        )
    except pixellab_client.PixelLabUnavailable as exc:
        raise tool_error(
            MCP_ERROR,
            f"PixelLab animation failed: {exc}",
            featureId=feature_id,
            firstFrameAssetId=first_frame_id,
        ) from exc

    kind = source["kind"]
    motion_digest = hashlib.sha256(f"{first_frame_id}|{motion}".encode()).hexdigest()[:8]
    sequence_id = f"{resolved_game}__{feature_id}__animation__{motion_digest}"
    out_dir = ROOT / "assets" / resolved_game / "animations" / f"{feature_id}_{motion_digest}"
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        asset_id = f"{sequence_id}__{index:02d}"
        # Zero-padded so the play order survives any directory listing.
        path = out_dir / f"{index:02d}_{motion_digest}.png"
        frame.save(path)
        manifest["assets"][asset_id] = {
            "asset_id": asset_id,
            "feature_id": feature_id,
            "kind": kind,
            "prompt": motion,
            "provider_prompt": motion,
            "status": PENDING,
            "asset_path": str(path),
            "created_at": _now(),
            "reviewed_at": None,
            "review_note": None,
            "prototype_asset_id": first_frame_id,
            "sequence_id": sequence_id,
            "frame_index": index,
            "provenance": {
                "method": "pixellab-api",
                "endpoint": "animate-with-text-v3",
                "job_id": job_id,
                "action": motion,
                "frame_index": index,
                "frame_count": len(frames),
                "first_frame_asset_id": first_frame_id,
                "usage": usage,
                "commercial_use": "see PixelLab terms of service",
            },
        }
        records.append(
            {
                "assetId": asset_id,
                "assetPath": str(path),
                "frameIndex": index,
                "status": PENDING,
            }
        )

    index_path = out_dir / "animation.json"
    index_path.write_text(
        json.dumps(
            {
                "sequenceId": sequence_id,
                "firstFrameAssetId": first_frame_id,
                "action": motion,
                "kind": kind,
                "frameCount": len(records),
                "jobId": job_id,
                "usage": usage,
                "frames": records,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _save_manifest(manifest)
    return {
        "sequenceId": sequence_id,
        "gameId": resolved_game,
        "firstFrameAssetId": first_frame_id,
        "action": motion,
        "kind": kind,
        "status": PENDING,
        "workflowStage": "animation",
        "indexPath": str(index_path),
        "frames": records,
        "frameCount": len(records),
        "jobId": job_id,
        "usage": usage,
    }


@mcp.tool(
    description=(
        "Generate a corner-Wang tileset (16 tiles: ground, wall, and every transition) "
        "for a biome's level art. Returns the tile files and their corner terrain map."
    )
)
@expects_dict_return
def generate_tileset(
    featureId: str,
    lowerDescription: str,
    upperDescription: str,
    gameId: str | None = None,
    tileSize: int = 32,
    transitionDescription: str = "",
) -> dict[str, Any]:
    """Generate one biome's ground/wall tileset through PixelLab.

    Unlike the sprite tools this produces a *set* whose members only mean
    something together: each tile declares which terrain sits in each of its
    four corners, and the level builder picks a tile by matching a cell's
    corners. Splitting them into 16 independent assets would lose that.

    The whole set is one review unit for the same reason — half an approved
    tileset cannot be painted.
    """

    feature_id = _require_identifier(featureId, "featureId")
    lower = _require(lowerDescription, "lowerDescription")
    upper = _require(upperDescription, "upperDescription")
    resolved_game = _resolve_game_id(gameId)

    if not 16 <= tileSize <= 64:
        raise tool_error(VALIDATION_ERROR, "tileSize must be between 16 and 64")

    style = load_or_create(ROOT, resolved_game, os.getenv("ASSET_ART_STYLE", DEFAULT_ART_STYLE))

    try:
        tiles, metadata, usage = pixellab_client.create_tileset(
            lower_description=lower,
            upper_description=upper,
            tile_size=tileSize,
            transition_description=transitionDescription.strip() or None,
            # Tilesets are ground plans: PixelLab only accepts the two top-down
            # values here (a "side" ArtStyle is rejected with 422). A side-view
            # game has no use for this tool, so falling back is safe.
            view=(
                style.camera_view
                if style.camera_view in ("low top-down", "high top-down")
                else "high top-down"
            ),
            outline="single color outline",
            shading="medium shading",
            detail="medium detail",
        )
    except pixellab_client.PixelLabUnavailable as exc:
        raise tool_error(
            MCP_ERROR, f"PixelLab tileset generation failed: {exc}", featureId=feature_id
        ) from exc

    digest = hashlib.sha256(f"{lower}|{upper}".encode()).hexdigest()[:8]
    tileset_id = f"{resolved_game}__{feature_id}__tileset__{digest}"
    out_dir = ROOT / "assets" / resolved_game / "tilesets" / f"{feature_id}_{digest}"
    out_dir.mkdir(parents=True, exist_ok=True)

    tile_records: list[dict[str, Any]] = []
    for tile in tiles:
        name = tile["name"] or f"tile_{len(tile_records)}"
        path = out_dir / f"{name}.png"
        tile["image"].save(path)
        tile_records.append({"name": name, "corners": tile["corners"], "path": str(path)})

    index_path = out_dir / "tileset.json"
    index_path.write_text(
        json.dumps(
            {"tilesetId": tileset_id, "tileSize": tileSize, "tiles": tile_records, "metadata": metadata},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest = _load_manifest(resolved_game)
    manifest["assets"][tileset_id] = {
        "asset_id": tileset_id,
        "feature_id": feature_id,
        "kind": "tileset",
        "prompt": f"lower={lower} / upper={upper}",
        "status": PENDING,
        "asset_path": str(index_path),
        "created_at": _now(),
        "reviewed_at": None,
        "review_note": None,
        "provenance": {
            "method": "pixellab",
            "endpoint": "tilesets",
            "tile_count": len(tile_records),
            "tile_size": tileSize,
            "style_seed": style.seed,
            "usage": usage,
        },
    }
    _save_manifest(manifest)

    return {
        "tilesetId": tileset_id,
        "gameId": resolved_game,
        "status": PENDING,
        "tileSize": tileSize,
        "indexPath": str(index_path),
        "tiles": tile_records,
        "imagesGenerated": len(tile_records),
    }


@mcp.tool(
    description=(
        "Generate one transparent map decoration (rock, plant, debris) to scatter over a "
        "tiled floor so the tiling stops reading as a grid."
    )
)
@expects_dict_return
def generate_map_object(
    featureId: str,
    prompt: str,
    gameId: str | None = None,
    size: int = 32,
) -> dict[str, Any]:
    """Generate a decoration for the layer above the ground tilemap.

    A Wang tileset repeats one picture across every interior cell, which the
    camera reads as a grid. Scattering a handful of these on top is what
    breaks that up — cheaper than generating a variant for every cell, and it
    keeps the floor itself seamless.

    Uses PixelLab's ``/map-objects``, which is asynchronous *and* hands back a
    URL that expires 8 hours later; ``pixellab_client`` downloads the bytes
    before returning, so nothing here depends on that URL surviving.
    """

    feature_id = _require_identifier(featureId, "featureId")
    description = _require(prompt, "prompt")
    resolved_game = _resolve_game_id(gameId)

    if not 16 <= size <= 128:
        raise tool_error(VALIDATION_ERROR, "size must be between 16 and 128")

    try:
        image, usage = pixellab_client.create_map_object(
            description=description, width=size, height=size
        )
    except pixellab_client.PixelLabUnavailable as exc:
        raise tool_error(
            MCP_ERROR, f"PixelLab map object generation failed: {exc}", featureId=feature_id
        ) from exc

    prompt_digest = hashlib.sha256(description.encode()).hexdigest()[:8]
    asset_id = f"{resolved_game}__{feature_id}__decor__{prompt_digest}"
    out_path = ROOT / "assets" / resolved_game / "decor" / f"{feature_id}_{prompt_digest}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)

    manifest = _load_manifest(resolved_game)
    manifest["assets"][asset_id] = {
        "asset_id": asset_id,
        "feature_id": feature_id,
        "kind": "decor",
        "prompt": description,
        "status": PENDING,
        "asset_path": str(out_path),
        "created_at": _now(),
        "reviewed_at": None,
        "review_note": None,
        "provenance": {"method": "pixellab", "endpoint": "map-objects", "usage": usage},
    }
    _save_manifest(manifest)

    return {
        "assetPath": str(out_path),
        "assetId": asset_id,
        "kind": "decor",
        "gameId": resolved_game,
        "status": PENDING,
        "imagesGenerated": 1,
    }


# --------------------------------------------------------------------------
# Style + human review metadata
# --------------------------------------------------------------------------


@mcp.tool(description="Lock a game's art style and return its palette. Idempotent.")
@expects_dict_return
def establish_art_style(gameId: str, artStyle: str = DEFAULT_ART_STYLE) -> dict[str, Any]:
    """Freeze the palette before any asset exists.

    Call this right after Planning, using the design document's ``art_style``,
    so every later asset inherits one deliberate look.
    """

    game_id = _require_identifier(gameId, "gameId")
    style = load_or_create(ROOT, game_id, artStyle or DEFAULT_ART_STYLE)
    return {
        "gameId": style.game_id,
        "artStyle": style.art_style,
        "palette": style.palette,
        "pixelGrid": style.pixel_grid,
        "seed": style.seed,
    }


@mcp.tool(description="List assets awaiting human review for a game.")
@expects_dict_return
def list_pending_assets(gameId: str) -> dict[str, Any]:
    game_id = _require_identifier(gameId, "gameId")
    manifest = _load_manifest(game_id)
    pending = [a for a in manifest["assets"].values() if a["status"] == PENDING]
    return {"gameId": game_id, "pending": pending, "count": len(pending)}


@mcp.tool(description="List resumable asset records, optionally filtered by status or feature.")
@expects_dict_return
def list_assets(
    gameId: str, status: str | None = None, featureId: str | None = None
) -> dict[str, Any]:
    game_id = _require_identifier(gameId, "gameId")
    if status is not None and status not in (PENDING, APPROVED, REJECTED):
        raise tool_error(VALIDATION_ERROR, f"unsupported status: {status}")
    feature_id = featureId.strip() if featureId else None
    assets = [
        record
        for record in _load_manifest(game_id)["assets"].values()
        if (status is None or record["status"] == status)
        and (feature_id is None or record["feature_id"] == feature_id)
    ]
    assets.sort(key=lambda record: (record["created_at"], record["asset_id"]))
    return {"gameId": game_id, "assets": assets, "count": len(assets)}


@mcp.tool(
    description=(
        "Inspect measurable asset defects and return the next workflow action. "
        "Semantic fit still requires host-agent and human review."
    )
)
@expects_dict_return
def inspect_asset(assetId: str) -> dict[str, Any]:
    asset_id = _require(assetId, "assetId")
    game_id = asset_id.split("__", 1)[0]
    manifest = _load_manifest(game_id)
    record = manifest["assets"].get(asset_id)
    if record is None:
        raise tool_error(VALIDATION_ERROR, f"unknown assetId: {asset_id}")

    path = Path(record["asset_path"])
    if not path.is_file():
        raise tool_error(VALIDATION_ERROR, f"asset file is missing: {path}")
    with Image.open(path) as opened:
        image = opened.convert("RGBA")

    provenance = record.get("provenance") or {}
    expected = provenance.get("expected_size")
    if not (
        isinstance(expected, list)
        and len(expected) == 2
        and all(isinstance(value, int) for value in expected)
    ):
        expected = list(image.size)
    inspection = quality.inspect(image, record["kind"], (expected[0], expected[1]))

    rejected_attempts = sum(
        candidate.get("feature_id") == record.get("feature_id")
        and candidate.get("kind") == record.get("kind")
        and candidate.get("status") == REJECTED
        and (candidate.get("provenance") or {}).get("method") == "pixellab-mcp"
        for candidate in manifest["assets"].values()
    )
    technical_status = inspection["technicalStatus"]
    human_review_status = record["status"]
    semantic_status = (
        "human_review_required" if human_review_status == PENDING else human_review_status
    )
    is_prototype = provenance.get("method") == "pixellab-mcp"
    if technical_status == "fail":
        next_action = "regenerate_after_technical_fix"
    elif record["status"] == REJECTED:
        next_action = "prepare_revision_from_feedback"
    elif record["status"] == APPROVED and is_prototype:
        next_action = "generate_style_locked_variations"
    elif record["status"] == APPROVED:
        next_action = "import_asset"
    else:
        next_action = "review_visual_intent"

    return {
        "assetId": asset_id,
        "assetPath": str(path),
        "kind": record["kind"],
        "status": record["status"],
        "technicalStatus": technical_status,
        "semanticStatus": semantic_status,
        "humanReviewStatus": human_review_status,
        "readyForVariations": (
            technical_status == "pass" and human_review_status == APPROVED and is_prototype
        ),
        "readyForImport": technical_status == "pass" and human_review_status == APPROVED,
        "prompt": record["prompt"],
        "feedback": record.get("review_feedback"),
        "inspection": inspection,
        "rejectedPrototypeAttempts": rejected_attempts,
        "escalationRequired": rejected_attempts >= 3,
        "nextAction": next_action,
    }


@mcp.tool(description="Record a human verification decision and structured feedback.")
@expects_dict_return
def review_asset(
    assetId: str,
    approved: bool,
    note: str = "",
    preserve: list[str] | None = None,
    change: list[str] | None = None,
    artStyleFeedback: str = "",
) -> dict[str, Any]:
    """Approve or reject an asset. The file is never moved or deleted.

    The decision is metadata, so it is recorded as metadata. Moving the file
    into an ``approved/`` or ``rejected/`` directory would break the path
    UnityMcpServer already imported (see the module docstring), and deleting a
    rejected asset would throw away the very evidence a reviewer is pointing at.
    """

    asset_id = _require(assetId, "assetId")
    game_id = asset_id.split("__", 1)[0]
    manifest = _load_manifest(game_id)
    record = manifest["assets"].get(asset_id)
    if record is None:
        raise tool_error(VALIDATION_ERROR, f"unknown assetId: {asset_id}")

    target_status = APPROVED if approved else REJECTED
    feedback = {
        "preserve": list(prompting.normalize_items(preserve)),
        "change": list(prompting.normalize_items(change)),
        "artStyle": artStyleFeedback.strip() or None,
    }
    record.update(
        status=target_status,
        reviewed_at=_now(),
        review_note=note or None,
        review_feedback=feedback,
    )
    _save_manifest(manifest)
    return {
        "assetId": asset_id,
        "status": target_status,
        "assetPath": record["asset_path"],
        "feedback": feedback,
    }


@mcp.tool(description="Report the review state of every asset in a game.")
@expects_dict_return
def asset_review_summary(gameId: str) -> dict[str, Any]:
    game_id = _require_identifier(gameId, "gameId")
    assets = _load_manifest(game_id)["assets"].values()
    counts = {status: 0 for status in (PENDING, APPROVED, REJECTED)}
    for asset in assets:
        counts[asset["status"]] = counts.get(asset["status"], 0) + 1
    return {
        "gameId": game_id,
        "total": len(list(assets)),
        "pending": counts[PENDING],
        "approved": counts[APPROVED],
        "rejected": counts[REJECTED],
        "readyForBuild": counts[PENDING] == 0 and counts[REJECTED] == 0,
    }


if __name__ == "__main__":
    serve(mcp)
