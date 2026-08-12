"""AssetGenMcpServer — Unity 게임용 PixelLab 2D 에셋 경계.

Design decisions worth knowing before editing:

* **PixelLab only, no fallback.** Generation requires ``PIXELLAB_API_KEY``.
  A missing key or a failed call is an MCP error (code 3000), not a
  silent degrade to placeholder art (see ``_generate_image``). ``render.py``
  keeps only the prompt classifier and the deterministic seed derivation
  PixelLab's call depends on — it no longer draws pixels itself.
* **Generation never blocks the pipeline.** ``generate_2d_sprite`` returns an
  ``assetPath`` immediately and records the asset as ``pending``. Human review
  is tracked alongside, not in front of, the build. Gating the pipeline on
  approval is the host's policy decision — see ``docs/contracts.md``.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error
from common.server import build, expects_dict_return, serve

from . import pixellab_client, render
from .style import load_or_create

logger = logging.getLogger(__name__)

mcp = build("AssetGenMcpServer")

ROOT = Path(os.getenv("ASSET_ROOT", "./var/assets")).resolve()
DEFAULT_ART_STYLE = "pixel art"

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
    return ROOT / "manifests" / f"{game_id}.json"


def _asset_path(game_id: str, feature_id: str, kind: str, prompt_digest: str) -> Path:
    """Where an asset lives — for its whole life, regardless of review status.

    Deliberately not segmented by status: Unity imports this exact path, so a
    review decision must not move the file out from under it. ``prompt_digest``
    keys the filename because one feature can name several same-kind assets
    (e.g. unityHints.assetsNeeded = ["무기 아이콘", "방어구 아이콘"]) — without it,
    the second sprite silently overwrote the first on disk and in the manifest.
    """

    return ROOT / "assets" / game_id / f"{feature_id}_{kind}_{prompt_digest}.png"


def _load_manifest(game_id: str) -> dict[str, Any]:
    path = _manifest_path(game_id)
    if not path.exists():
        return {"game_id": game_id, "assets": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(manifest: dict[str, Any]) -> None:
    path = _manifest_path(manifest["game_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _require(value: str, field: str) -> str:
    """Validation errors carry code 1000, not the generic 3000."""

    if not value or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value.strip()


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
        return explicit.strip()
    return os.getenv("ASSET_DEFAULT_GAME_ID", "default")


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
    feature_id = _require(feature_id, "featureId")
    prompt = _require(prompt, "prompt")
    resolved_game = _resolve_game_id(game_id)

    style = load_or_create(
        ROOT, resolved_game, art_style or os.getenv("ASSET_ART_STYLE", DEFAULT_ART_STYLE)
    )
    kind = forced_kind or render.classify(prompt)
    rng = render.rng_for(style, feature_id, prompt)

    image, provenance = _generate_image(style, kind, rng, prompt, feature_id)

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


# --------------------------------------------------------------------------
# Agent-first asset tools
# --------------------------------------------------------------------------


@mcp.tool(description="Generate a 2D sprite for a feature, in the game's locked art style.")
@expects_dict_return
def generate_2d_sprite(
    featureId: str, prompt: str, gameId: str | None = None, artStyle: str | None = None
) -> dict[str, Any]:
    return _generate(featureId, prompt, gameId, art_style=artStyle)


@mcp.tool(description="Generate a UI asset (panel, button, or icon) for a feature.")
@expects_dict_return
def generate_ui_asset(
    featureId: str, prompt: str, gameId: str | None = None, artStyle: str | None = None
) -> dict[str, Any]:
    kind = render.classify(prompt)
    if not kind.startswith("ui_") and kind != "icon":
        kind = "ui_panel"  # this tool always produces UI, whatever the wording
    return _generate(featureId, prompt, gameId, forced_kind=kind, art_style=artStyle)


@mcp.tool(description="Generate a placeholder stand-in for a 3D asset (rendered as a 2D sprite).")
@expects_dict_return
def generate_3d_placeholder(
    featureId: str, prompt: str, gameId: str | None = None, artStyle: str | None = None
) -> dict[str, Any]:
    # The pipeline targets 2D games; a 3D request still needs *something*
    # importable, so it gets a prop silhouette rather than an error.
    return _generate(featureId, prompt, gameId, forced_kind="prop", art_style=artStyle)


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

    feature_id = _require(featureId, "featureId")
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

    feature_id = _require(featureId, "featureId")
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

    game_id = _require(gameId, "gameId")
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
    game_id = _require(gameId, "gameId")
    manifest = _load_manifest(game_id)
    pending = [a for a in manifest["assets"].values() if a["status"] == PENDING]
    return {"gameId": game_id, "pending": pending, "count": len(pending)}


@mcp.tool(description="Record a human verification decision for one asset.")
@expects_dict_return
def review_asset(assetId: str, approved: bool, note: str = "") -> dict[str, Any]:
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
    record.update(
        status=target_status,
        reviewed_at=_now(),
        review_note=note or None,
    )
    _save_manifest(manifest)
    return {
        "assetId": asset_id,
        "status": target_status,
        "assetPath": record["asset_path"],
    }


@mcp.tool(description="Report the review state of every asset in a game.")
@expects_dict_return
def asset_review_summary(gameId: str) -> dict[str, Any]:
    game_id = _require(gameId, "gameId")
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
