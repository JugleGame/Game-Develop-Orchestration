"""AssetGenMcpServer — PixelLab 2D asset boundary for Unity games.

Design decisions worth knowing before editing:

* **PixelLab only, no fallback.** Generation requires ``PIXELLAB_API_KEY``.
  A missing key or a failed call is an MCP error (code 3000), not a
  silent degrade to placeholder art (see ``_generate_prototype``). ``render.py``
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
# Human-supplied reference drawings, one directory per game. Deliberately not
# under ASSET_ROOT: nothing here is generated, reviewed, or served as an asset.
CONCEPT_ART_ROOT = REPO_ROOT / "var" / "concept-art"
CONCEPT_REFERENCE_PREFIX = "concept:"
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
    # Square, and the 64 rows that made it work live in _KIND_MIN_GRID instead.
    # The ratio used to be 1:2 because at 32x48 a full-body humanoid came out
    # cropped below the thigh (measured, prompt-eval round 11, two of two
    # samples) and 32x64 fit head to feet. That measurement said 48 rows are
    # too few, not that the canvas has to be tall: 64x64 keeps every one of
    # those 64 rows and only widens. A 1:2 character could reach a square
    # canvas solely by being grown for bitforge (_posable_canvas), which
    # stopped at 64 and so left gridSize 64 generating an unusable 64x128.
    "character": (1.0, 1.0),
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


# Smallest grid a kind may be derived at from the game's locked grid. A
# character needs 64 rows — 48 cropped the figure below the thigh (prompt-eval
# round 11) — and now that the ratio is square those rows can only come from
# the grid. Games lock a 32 grid for their tiles, so without this floor a
# character would shrink to 32x32 and bring the crop back. An explicit
# ``gridSize`` is not floored: it is the caller sizing one asset deliberately,
# and ``canvas`` is there for sizes that leave the ratio entirely.
_KIND_MIN_GRID: dict[str, int] = {"character": 64}


# PixelLab (Pixflux) accepts 16-400px per side — ``CreateImagePixfluxRequest.
# image_size`` declares ``minimum: 16``, ``maximum: 400`` (v2/openapi.json,
# re-verified 2026-08-22). The floor used to sit at 32 on the strength of one
# opaque failure (a 16x32 request through the *MCP* path answered with a
# TaskGroup exception that named no cause, two of two samples). That failure
# was never explained and the REST schema contradicts it, so the contract
# follows the schema; if 16px fails again it must be diagnosed, not re-guessed.
_PIXELLAB_MIN_SIDE = 16
_PIXELLAB_MAX_SIDE = 400


def _size_for(
    style: Any, kind: render.AssetKind, grid: int | None = None
) -> tuple[int, int]:
    """Native PixelLab generation size for this kind, derived from a grid.

    ``grid`` overrides ``style.pixel_grid`` for one call. Without it the game's
    locked grid decides every asset's canvas, so two things of different
    in-world size (a boy and the giant chasing him) can only be generated at
    the same pixel density by editing the game's style file between calls —
    which mutates shared state and races any concurrent generation.
    """

    width_ratio, height_ratio = _KIND_SIZE_RATIO.get(kind, (1.0, 1.0))
    if grid is None:
        resolved = max(style.pixel_grid, _KIND_MIN_GRID.get(kind, 0))
    else:
        resolved = grid
    return int(resolved * width_ratio), int(resolved * height_ratio)


def _resolve_size(
    style: Any,
    kind: render.AssetKind,
    grid: int | None,
    feature_id: str,
    canvas: list[int] | None = None,
) -> tuple[int, int]:
    """Size for this call, rejected here rather than by an opaque provider error.

    ``canvas`` is the caller stating the size outright. It skips
    ``_KIND_SIZE_RATIO`` entirely, which is the point: the ratio is a good
    default for a kind and a wrong answer for one asset, and until this existed
    there was no way to say so. The per-side range check below still applies —
    naming a size is allowed, naming one the provider cannot draw is not.
    """

    if canvas is not None:
        if grid is not None:
            raise tool_error(
                VALIDATION_ERROR,
                "canvas and gridSize both set the generated size; pass one",
                featureId=feature_id,
            )
        if len(canvas) != 2 or any(
            not isinstance(side, int) or isinstance(side, bool) or side <= 0
            for side in canvas
        ):
            raise tool_error(
                VALIDATION_ERROR,
                "canvas must be [width, height], both positive integers",
                featureId=feature_id,
            )
        width, height = canvas
        if not (
            _PIXELLAB_MIN_SIDE <= width <= _PIXELLAB_MAX_SIDE
            and _PIXELLAB_MIN_SIDE <= height <= _PIXELLAB_MAX_SIDE
        ):
            raise tool_error(
                VALIDATION_ERROR,
                f"canvas {width}x{height} is outside PixelLab's "
                f"{_PIXELLAB_MIN_SIDE}-{_PIXELLAB_MAX_SIDE}px per-side range",
                featureId=feature_id,
            )
        return width, height
    if grid is not None and grid <= 0:
        raise tool_error(VALIDATION_ERROR, "gridSize must be a positive integer")
    width, height = _size_for(style, kind, grid)
    if not (
        _PIXELLAB_MIN_SIDE <= width <= _PIXELLAB_MAX_SIDE
        and _PIXELLAB_MIN_SIDE <= height <= _PIXELLAB_MAX_SIDE
    ):
        raise tool_error(
            VALIDATION_ERROR,
            f"{kind} at grid {style.pixel_grid if grid is None else grid} generates "
            f"{width}x{height}, outside PixelLab's "
            f"{_PIXELLAB_MIN_SIDE}-{_PIXELLAB_MAX_SIDE}px per-side range",
            featureId=feature_id,
        )
    return width, height


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


def _brief_path(brief_id: str) -> Path:
    return _root_path("briefs", f"{brief_id}.json")


def _save_brief(brief: dict[str, Any]) -> str:
    """Persist one intake brief and return its id.

    Keyed by content, so re-asking the same question set returns the same id
    and answering one more question produces a new one. That makes the id
    evidence of *which* answers were on the table, which is the whole reason
    ``generate_2d_sprite`` asks for it.
    """

    payload = json.dumps(brief, sort_keys=True, ensure_ascii=False)
    brief_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    path = _brief_path(brief_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return brief_id


def _answered_brief(brief_id: str, feature_id: str) -> dict[str, Any]:
    """The brief this generation is allowed to run from, or a refusal.

    Refusing here rather than defaulting is the point of the gate: every
    default this tool used to carry was a value the user was never asked
    about, and the bill for the guess arrived as a generated image.
    """

    path = _brief_path(brief_id)
    if not path.is_file():
        raise tool_error(
            VALIDATION_ERROR,
            f"unknown briefId: {brief_id}; call prepare_asset_prompt first",
            featureId=feature_id,
        )
    brief = json.loads(path.read_text(encoding="utf-8"))
    unanswered = [
        str(question["field"])
        for question in brief.get("questions") or []
        if question.get("required")
    ]
    if unanswered:
        raise tool_error(
            VALIDATION_ERROR,
            f"briefId {brief_id} still has unanswered questions: {', '.join(unanswered)}. "
            "Ask the user, then call prepare_asset_prompt again with the answers.",
            featureId=feature_id,
        )
    return brief


def _brief_parameter(
    brief: dict[str, Any], field: str, passed: Any, feature_id: str
) -> Any:
    """The answered value, refusing a call that contradicts it."""

    answered = (brief.get("parameters") or {}).get(field)
    if passed is not None and passed != answered:
        raise tool_error(
            VALIDATION_ERROR,
            f"{field}={passed!r} contradicts the brief, which answered {answered!r}. "
            "Re-run prepare_asset_prompt to change an answer.",
            featureId=feature_id,
        )
    return answered


def _brief_prompt(brief: dict[str, Any], passed: str, feature_id: str) -> str:
    """The brief's own prompt, refusing a call that rewrote it.

    The ``briefId`` gate pinned every generation *parameter* to an answer the
    user gave and left the one field the picture is actually made of free. A
    caller could answer five questions, take the id, then generate from an
    unrelated paragraph — so the preflight proved nothing about the prompt it
    was gating. The brief is the prompt now; restating it is allowed, changing
    it is not.
    """

    canonical = str(brief.get("prompt") or "").strip()
    if passed.strip() and passed.strip() != canonical:
        raise tool_error(
            VALIDATION_ERROR,
            f"prompt does not match the brief, which composed {canonical!r}. "
            "Re-run prepare_asset_prompt with new answers to change the picture "
            "rather than rewording the prompt here.",
            featureId=feature_id,
        )
    return canonical


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


def _prototype_claim_path(asset_id: str) -> Path:
    return _root_path("submissions", f"{asset_id}.json")


def _claim_paid_prototype(asset_id: str) -> dict[str, Any] | None:
    """Atomically reserve an idempotent 2D prototype request before payment."""

    path = _prototype_claim_path(asset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "submission_state_unreadable"}
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(_fresh_prototype_claim(asset_id), stream)
        stream.flush()
        os.fsync(stream.fileno())
    return None


def _fresh_prototype_claim(asset_id: str) -> dict[str, Any]:
    return {"assetId": asset_id, "status": "SUBMITTING", "createdAt": _now()}


def _claim_is_retryable(claim: dict[str, Any]) -> bool:
    """Whether a non-completed claim may be retaken by the same prompt.

    Only a failure that never reached PixelLab's meter qualifies. Anything
    else — a billable failure, or a claim left at ``SUBMITTING`` because the
    process died mid-call — stays blocked so the same image is not paid for
    twice; the blocked response names the file to delete.
    """

    # ponytail: no age cutoff on SUBMITTING. Add one only if crashed calls
    # turn out to be common enough that manual deletion is a burden.
    return claim.get("status") == "FAILED" and not claim.get("billable", True)


def _save_prototype_claim(asset_id: str, value: dict[str, Any]) -> None:
    path = _prototype_claim_path(asset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
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


def _direction(value: str | None, feature_id: str) -> str | None:
    """Validate a per-asset facing before it costs a request.

    ``None`` means "use the game's locked direction"; a wrong value would
    otherwise come back as a 422 that has already been paid for.
    """

    if value is None or not value.strip():
        return None
    direction = value.strip().lower()
    if direction not in pixellab_client.DIRECTIONS:
        raise tool_error(
            VALIDATION_ERROR,
            f"direction must be one of {pixellab_client.DIRECTIONS}",
            featureId=feature_id,
        )
    return direction


ASSET_KINDS: tuple[str, ...] = get_args(render.AssetKind)


def _asset_kind(value: str | None) -> render.AssetKind | None:
    """Validate an optional explicit kind before prompt classification."""

    if value is None:
        return None
    normalized = _require(value, "assetKind")
    if normalized not in ASSET_KINDS:
        raise tool_error(
            VALIDATION_ERROR,
            f"unsupported assetKind: {normalized}; use one of {ASSET_KINDS}",
        )
    return normalized  # type: ignore[return-value]


def _is_pixellab_asset(record: dict[str, Any]) -> bool:
    """Whether this asset was drawn by PixelLab, by any of its paths.

    Style-anchor eligibility used to demand ``method == "pixellab-mcp"``
    exactly. The REST path records ``"pixellab"`` and the variation path
    records ``"pixellab-api"``, so an approved asset from either could never
    become an anchor — including an approved variation, which is the obvious
    thing to build the next batch on. What actually matters is that a human
    approved a PixelLab image of this game, not which of its endpoints drew it.
    """

    method = (record.get("provenance") or {}).get("method") or ""
    return method.startswith("pixellab")


def _posable_canvas(width: int, height: int) -> tuple[int, int] | None:
    """The canvas this request has to use for keypoints to work, or ``None``.

    Stated as a rule about canvases rather than a rule about characters, so it
    holds for any kind — including ones this repository has not defined yet.
    PixelLab's keypoint-friendly sizes are all square
    (``pixellab_client.SKELETON_FRIENDLY_SIZES``), so a posed request is grown
    to the smallest square that still contains the canvas it asked for.

    Growing rather than shrinking is what keeps the original measurement
    intact: a character has 64 rows because 48 cropped the figure below the
    thigh, and a square that contains the request keeps every one of those
    rows. Only the width changes. A canvas already square and friendly is
    returned unchanged, so this is a no-op for ``character``, ``monster``,
    ``prop``, ``icon``, and ``tile`` at the usual grids — it earns its keep on
    ``ui_button`` and ``ui_panel``, and on any kind added later that is not
    square.
    """

    if width == height:
        # Already square: there is nothing for the growth to fix, whatever the
        # size. The friendly list is about keypoints, and a keypoint request on
        # a square canvas outside it is reported by
        # ``pixellab_client.skeleton_size_warning`` instead. Without this, a
        # 128x128 request fell past the list and was reported as unreliable
        # "on a non-square canvas" — about a canvas that is square.
        return (width, height)
    needed = max(width, height)
    for side in sorted(pixellab_client.SKELETON_FRIENDLY_SIZES):
        if side >= needed:
            return (side, side)
    return None


def _concept_reference(name: str, game_id: str, field: str, feature_id: str) -> Image.Image:
    """Open a concept-art image of this game to reuse as a reference.

    The approval gates below exist to stop an *unreviewed generation* from
    being laundered into approved work. Concept art is the opposite case: a
    human put the file in ``var/concept-art/<gameId>/`` deliberately, and it is
    the drawing the generated asset is meant to match. Without this path the
    settled design can only be described in prose, and every regeneration
    drifts on proportion, clothing, and prop placement.

    The directory is the whole gate, so the name is resolved against it and a
    result outside it is refused rather than clamped.
    """

    root = (CONCEPT_ART_ROOT / game_id).resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise tool_error(
            VALIDATION_ERROR,
            f"{field} concept reference must stay within {root}",
            featureId=feature_id,
        ) from exc
    if not path.is_file():
        raise tool_error(
            VALIDATION_ERROR,
            f"unknown {field} concept reference: {path}",
            featureId=feature_id,
        )
    with Image.open(path) as opened:
        return opened.convert("RGBA").copy()


def _approved_reference(
    asset_id: str, game_id: str, field: str, feature_id: str
) -> Image.Image:
    """Open an approved sprite of this game to reuse as a reference.

    The same three gates as a style anchor: it belongs to this game, a human
    approved it, and PixelLab drew it. A reference is a second asset's pose or
    starting pixels, so an unreviewed one would launder an unapproved image
    into approved work.

    ``concept:<filename>`` takes the concept-art path instead — see
    ``_concept_reference`` for why that one is not an approval hole.
    """

    if asset_id.startswith(CONCEPT_REFERENCE_PREFIX):
        return _concept_reference(
            asset_id[len(CONCEPT_REFERENCE_PREFIX) :], game_id, field, feature_id
        )
    if asset_id.split("__", 1)[0] != game_id:
        raise tool_error(
            VALIDATION_ERROR, f"{field} must belong to gameId", featureId=feature_id
        )
    record = (_load_manifest(game_id)["assets"] or {}).get(asset_id)
    if record is None:
        raise tool_error(
            VALIDATION_ERROR, f"unknown {field}: {asset_id}", featureId=feature_id
        )
    if record["status"] != APPROVED:
        raise tool_error(
            VALIDATION_ERROR, f"{field} must be approved", featureId=feature_id
        )
    if not _is_pixellab_asset(record):
        raise tool_error(
            VALIDATION_ERROR, f"{field} must come from PixelLab", featureId=feature_id
        )
    path = Path(record["asset_path"])
    if not path.is_file():
        raise tool_error(
            VALIDATION_ERROR, f"{field} file is missing: {path}", featureId=feature_id
        )
    with Image.open(path) as opened:
        return opened.convert("RGBA").copy()


def _pixellab_palette(
    style: Any,
    kind: render.AssetKind,
    prompt: str,
    palette_lock: bool = True,
) -> list[tuple[int, int, int]] | None:
    """The locked ``ArtStyle``'s ramp for this kind, as PixelLab's ``color_image``.

    PixelLab has no notion of "this game's look" between calls — each request
    is stateless. Reusing the game's own ramps (rather than inventing a
    second palette scheme) is what makes a PixelLab tile and a PixelLab icon
    from the same game share a hue family instead of each call picking its
    own colours.

    ``character``/``monster`` used to get no palette at all. The recorded
    reason was that a living thing needs more colour range than a five-swatch
    ramp can hold — the locked ramp read as "too green, no character" (5/10)
    while dropping it scored 7/10, tied with keeping it. A tie is thin ground
    for giving up consistency, and the alternative that was assumed to cover
    it does not exist: measured 2026-08-22 (see ``docs/contracts.md``),
    ``style_image`` carries the reference's *subject*, not its look, so it
    cannot make two different subjects share a game's palette. That left
    characters and monsters — the assets whose style is most visible — with no
    colour lock of any kind.

    They now get ``ArtStyle.character_palette()``: the same identity ramp
    first, then skin, metal, and leather, so the range objection is answered
    without giving up the lock. ``palette_lock=False`` turns it off for one
    asset.
    """

    if not palette_lock:
        return None
    if kind in ("character", "monster"):
        return style.character_palette()
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


def _pixellab_style_params(
    style: Any, kind: render.AssetKind, direction: str | None = None
) -> dict[str, Any]:
    """PixelLab's structured style controls.

    Confirmed enums, not free text. ``view`` comes from the locked
    ``ArtStyle`` (not per-kind), the same way the palette is locked: a game
    mixing camera angles per asset call reads as broken.

    ``shading`` is the one axis that had to differ by kind — the game's own
    setting reads fine on a character or creature body, but the same setting
    made a boxy prop (a treasure chest) look like a 3D render instead of flat
    pixel art (measured, scored 3/5). Flattened for anything that isn't a
    character or monster, whatever the game asks for.

    These fields are all documented ``(weakly guiding)``. They bias a result,
    they do not override the description — which is why ``prompting.compose``
    keeps the description's own style wording instead of deleting it as a
    duplicate. Sending both is the point.

    ``direction`` is the one control that is genuinely per-asset: a game locks
    which way its sprites face, but a single asset may need another (a door on
    the west wall, an NPC turned to the player). ``None`` means the game's
    locked value. ``isometric`` is a boolean here, not a camera view.
    """

    return {
        "outline": "single color black outline",
        "shading": style.shading if kind in ("character", "monster") else "flat shading",
        "detail": style.detail,
        "view": style.camera_view,
        "direction": direction or style.direction,
        "isometric": style.isometric,
    }


# ``/map-objects`` declares its own style enums and they are narrower than
# pixflux's, so the game's locked values cannot be passed straight through.
_MAP_OBJECT_OUTLINE = {"single color black outline": "single color outline"}
_MAP_OBJECT_DETAIL = {"highly detailed": "high detail"}


def _map_object_style_params(style: Any) -> dict[str, str]:
    """The locked ``ArtStyle`` expressed in ``/map-objects``' own vocabulary.

    Sending nothing let the endpoint apply its defaults, and its ``view``
    default is "high top-down" — which drew a side-view game's props as if
    seen from above. Decorations are inanimate, so ``shading`` is flattened
    for the same reason ``_pixellab_style_params`` flattens it.
    """

    outline = _pixellab_style_params(style, "prop")["outline"]
    # No "direction"/"isometric": CreateMapObjectRequest declares neither.
    return {
        "view": style.camera_view,
        "outline": _MAP_OBJECT_OUTLINE.get(outline, outline),
        "shading": "flat shading",
        "detail": _MAP_OBJECT_DETAIL.get(style.detail, style.detail),
    }


def _generate_prototype(
    feature_id: str,
    prompt: str,
    game_id: str | None,
    forced_kind: render.AssetKind | None = None,
    art_style: str | None = None,
    grid_size: int | None = None,
    direction: str | None = None,
    kind_source: str = "inferred",
    palette_lock: bool = True,
    pose_from_asset_id: str | None = None,
    init_asset_id: str | None = None,
    skeleton_guidance: float | None = None,
    init_image_strength: int | None = None,
    canvas: list[int] | None = None,
    exclusions: list[str] | None = None,
) -> dict[str, Any]:
    """Generate the reviewable style prototype.

    Normally through PixelLab's official MCP. A request that carries a pose or
    a starting image goes through ``create-image-bitforge`` instead, because
    those fields exist only there.
    """

    feature_id = _require_identifier(feature_id, "featureId")
    direction = _direction(direction, feature_id)
    prompt = _require(prompt, "prompt")
    resolved_game = _resolve_game_id(game_id)
    style = load_or_create(
        ROOT, resolved_game, art_style or os.getenv("ASSET_ART_STYLE", DEFAULT_ART_STYLE)
    )
    kind = forced_kind or render.classify(prompt)
    # The structured payload is built here rather than at the request so the
    # composer can see it: wording that contradicts a field it is sent
    # alongside is reported instead of quietly competing with it.
    style_params = _pixellab_style_params(style, kind, direction)
    # A starting image at a strength where it leads states the composition in
    # pixels, so the kind framing would be prose arguing with an image that has
    # already won. A pose reference is not the same thing: keypoints carry the
    # pose and nothing else, so framing still has something to say there.
    # An unanswered strength means the provider's own default applies and we do
    # not know which band it lands in, so the framing stays.
    reference_leads = bool(init_asset_id) and (init_image_strength or 0) >= (
        prompting.REFERENCE_LEAD_STRENGTH
    )
    prompt_plan = prompting.compose(
        prompt, kind, style_params, reference_leads=reference_leads
    )
    width, height = _resolve_size(style, kind, grid_size, feature_id, canvas)
    # The *caller's* prompt seeds the generation and names the asset, not the
    # composed one. Composition is a presentation step that this change just
    # rewrote; feeding its output to the digest would rename every asset ever
    # generated and unblock every duplicate claim guarding a paid prompt.
    seed = render.rng_for(style, feature_id, prompt).getrandbits(32)
    # The grid joins the digest only when overridden, so digests written before
    # this parameter existed still resolve to the same asset id and file. The
    # canvas joins it the same way and for the same reason as the grid: the
    # same prompt at another size is another asset, not a duplicate of this one.
    digest_source = prompt if grid_size is None else f"{prompt}|grid{grid_size}"
    if canvas is not None:
        digest_source = f"{digest_source}|canvas{canvas[0]}x{canvas[1]}"
    # The palette joins it too, for the same reason as the grid and the canvas:
    # the same prompt with the game ramp forced on is a different image from the
    # same prompt without it, so asking for both has to produce two assets to
    # compare rather than a duplicate block on the second. Only the non-default
    # value is appended, so digests written before this still resolve to the
    # same asset id and file.
    if not palette_lock:
        digest_source = f"{digest_source}|palette-unlocked"
    prompt_digest = hashlib.sha256(digest_source.encode()).hexdigest()[:8]
    asset_id = f"{resolved_game}__{feature_id}__{kind}__{prompt_digest}"
    if not pixellab_client.is_configured():
        raise tool_error(MCP_ERROR, "PIXELLAB_API_KEY is not set", featureId=feature_id)
    existing_claim = _claim_paid_prototype(asset_id)
    if existing_claim is not None:
        if existing_claim.get("status") == "COMPLETED":
            return {
                "assetPath": existing_claim.get("assetPath"),
                "assetId": asset_id,
                "kind": kind,
                "kindSource": kind_source,
                "gameId": resolved_game,
                "status": PENDING,
                "duplicateBlocked": True,
                "workflowStage": "prototype",
                "styleSeed": style.seed,
            }
        if _claim_is_retryable(existing_claim):
            # Nothing was generated and nothing was billed, so the prompt is
            # free again. Retaking the claim keeps the prompt — and therefore
            # the seed (render.rng_for) — identical, which is the whole point:
            # editing the prompt just to dodge the claim regenerates the asset
            # with a different seed.
            _save_prototype_claim(asset_id, _fresh_prototype_claim(asset_id))
        else:
            return {
                "assetId": asset_id,
                "gameId": resolved_game,
                "status": "duplicate_blocked",
                "recoveryRequired": True,
                "claimPath": str(_prototype_claim_path(asset_id)),
                "reason": existing_claim.get("error")
                or f"an earlier request for this prompt is recorded as {existing_claim.get('status')}",
                "recovery": (
                    "An earlier request for this exact prompt may already have been billed. "
                    "Review it, then delete the file at claimPath and call this tool again with "
                    "the same prompt to force one more generation. Do not reword the prompt: "
                    "that changes the seed and the asset."
                ),
            }
    palette_rgb = _pixellab_palette(style, kind, prompt, palette_lock)
    palette = [f"#{red:02x}{green:02x}{blue:02x}" for red, green, blue in palette_rgb or []]

    posed = pose_from_asset_id or init_asset_id
    skeleton: list[dict[str, Any]] = []
    skeleton_usage: dict[str, Any] = {}
    init_image = None
    warnings: list[str] = []
    requested_size = (width, height)
    if posed:
        ceiling = pixellab_client.BITFORGE_SIDE_RANGE[1]
        if max(width, height) > ceiling:
            # Refused rather than dropped: silently generating without the pose
            # that was asked for is the worst of the three outcomes.
            raise tool_error(
                VALIDATION_ERROR,
                f"poseFromAssetId and initAssetId need create-image-bitforge, which stops "
                f"at {ceiling}px per side; this request is {width}x{height}",
                featureId=feature_id,
            )
        # Grown for every bitforge request, not only the posed ones. Keypoints
        # were the reason to look, but the control says the canvas is the
        # problem by itself: measured 2026-08-22, a slim character asked for at
        # 32x64 through bitforge with *no* keypoints came back as a detached hat
        # floating above a body, while the same prompt at 64x64 came back as a
        # complete figure. A non-square canvas is where this endpoint fails.
        # An explicit canvas is not grown. Growing it would answer a question
        # the caller already answered, and the warning below says what the
        # provider's weakness is — which is the caller's to weigh, not this
        # server's to overrule.
        squared = None if canvas is not None else _posable_canvas(width, height)
        if canvas is not None and width != height:
            warnings.append(
                f"canvas {width}x{height} was used as given; create-image-bitforge is "
                "unreliable on a non-square canvas"
            )
        elif squared is None:
            warnings.append(
                f"{width}x{height} has no square canvas to grow to within "
                f"{pixellab_client.SKELETON_FRIENDLY_SIZES}; this endpoint is "
                "unreliable on a non-square canvas"
            )
        elif squared != requested_size:
            width, height = squared
            warnings.append(
                f"canvas grown from {requested_size[0]}x{requested_size[1]} to "
                f"{width}x{height}: create-image-bitforge is unreliable on a "
                "non-square canvas. Every row of the original is kept and only "
                "the width changes"
            )
        if init_asset_id:
            init_image = _approved_reference(
                init_asset_id, resolved_game, "initAssetId", feature_id
            )
        if pose_from_asset_id:
            source = _approved_reference(
                pose_from_asset_id, resolved_game, "poseFromAssetId", feature_id
            )
            try:
                # Passed through unscaled. The keypoints are normalised to
                # 0-1, not pixels, so the reference's own size is irrelevant
                # and rescaling them by the size ratio is actively wrong —
                # measured 2026-08-22: a 128x256 reference scaled onto a 32x64
                # canvas put every joint in the top-left corner and the
                # generation came back as noise.
                skeleton, skeleton_usage = pixellab_client.estimate_skeleton(source)
            except pixellab_client.PixelLabUnavailable as exc:
                raise tool_error(
                    MCP_ERROR,
                    f"PixelLab skeleton estimation failed: {exc}",
                    featureId=feature_id,
                ) from exc
            warning = pixellab_client.skeleton_size_warning(width, height)
            if warning:
                warnings.append(warning)

    # ``avoid`` answers were collected by the intake and then dropped on the
    # floor: nothing here ever read them, so the one question that asks what
    # must not appear had no effect on a prototype. They are live on bitforge
    # and ``(Deprecated)`` on pixflux, so they travel on one path only — and
    # the result says which, rather than leaving the caller to infer it.
    negatives = ", ".join(
        dict.fromkeys(
            part
            for part in (prompt_plan.negative_description, *(exclusions or []))
            if part and part.strip()
        )
    )
    try:
        if posed:
            image, usage = pixellab_client.create_image_bitforge(
                prompt=prompt_plan.prompt,
                negative_description=negatives,
                width=width,
                height=height,
                seed=seed,
                skeleton_keypoints=skeleton or None,
                skeleton_guidance_scale=skeleton_guidance,
                init_image=init_image,
                init_image_strength=init_image_strength,
                forced_palette=palette_rgb,
                **style_params,
            )
            tool_name = "create-image-bitforge"
        else:
            image, usage, tool_name = pixellab_client.generate_prototype(
                prompt=prompt_plan.prompt,
                width=width,
                height=height,
                kind=kind,
                seed=seed,
                style_description=style.art_style,
                style_params=style_params,
                palette=palette,
            )
    except pixellab_client.PixelLabUnavailable as exc:
        # No image came back, so record how the claim ended instead of leaving
        # it at SUBMITTING, which used to block this prompt forever.
        _save_prototype_claim(
            asset_id,
            {
                "assetId": asset_id,
                "status": "FAILED",
                "error": str(exc),
                "billable": bool(getattr(exc, "job_started", False)),
                "failedAt": _now(),
            },
        )
        raise tool_error(
            MCP_ERROR, f"PixelLab MCP prototype failed: {exc}", featureId=feature_id
        ) from exc

    target_size = (width * _PIXELLAB_UPSCALE, height * _PIXELLAB_UPSCALE)
    if image.size != target_size:
        image = image.resize(target_size, Image.NEAREST)

    out_path = _asset_path(resolved_game, feature_id, kind, prompt_digest)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)

    provenance = {
        "method": "pixellab-api" if posed else "pixellab-mcp",
        "generator": (
            "https://api.pixellab.ai/v2" if posed else "https://api.pixellab.ai/mcp"
        ),
        "tool": tool_name,
        "pose_from": pose_from_asset_id,
        "init_from": init_asset_id,
        "kind": kind,
        "kind_source": kind_source,
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
    _save_prototype_claim(
        asset_id,
        {
            "assetId": asset_id,
            "status": "COMPLETED",
            "assetPath": str(out_path),
            "completedAt": _now(),
        },
    )
    result = {
        "assetPath": str(out_path),
        "assetId": asset_id,
        "kind": kind,
        "kindSource": kind_source,
        "gameId": resolved_game,
        "status": PENDING,
        "workflowStage": "prototype",
        "styleSeed": style.seed,
        "generatedBy": provenance["method"],
        "promptMetrics": prompt_plan.metadata(),
        "exclusionsSent": bool(posed and negatives),
        "exclusions": negatives or None,
    }
    if negatives and not posed:
        warnings.append(
            "exclusions were not sent: negative_description is (Deprecated) on the "
            "pixflux path. State the replacement positively in mustHave, or generate "
            "with a reference so the request goes through create-image-bitforge"
        )
    if skeleton:
        result["skeletonKeypoints"] = len(skeleton)
    if (width, height) != requested_size:
        result["canvas"] = [width, height]
        result["requestedCanvas"] = list(requested_size)
    if warnings:
        result["warnings"] = warnings
    images = _images_generated(usage)
    # Skeleton estimation is its own billed call, so it is added rather than
    # folded into the generation's own report.
    result["imagesGenerated"] = (images or 1) + _images_generated(skeleton_usage)
    return result


# --------------------------------------------------------------------------
# Agent-first asset tools
# --------------------------------------------------------------------------


def _approved_source(
    asset_id: str, game_id: str | None, field: str, feature_id: str
) -> tuple[dict[str, Any], dict[str, Any], str, Image.Image]:
    """The approved sprite named by ``asset_id``, as ``(manifest, record, game, image)``.

    Every derivation tool needs the same four things and the same three
    refusals, and each one that grew its own copy grew a slightly different set
    of them.
    """

    asset_id = _require(asset_id, field)
    source_game = asset_id.split("__", 1)[0]
    resolved_game = _resolve_game_id(game_id) if game_id else source_game
    if resolved_game != source_game:
        raise tool_error(VALIDATION_ERROR, f"gameId must match the {field} asset")

    manifest = _load_manifest(resolved_game)
    record = manifest["assets"].get(asset_id)
    if record is None:
        raise tool_error(VALIDATION_ERROR, f"unknown {field}: {asset_id}")
    if record["status"] != APPROVED:
        raise tool_error(
            VALIDATION_ERROR, f"the {field} asset must be approved before deriving from it"
        )
    path = Path(record["asset_path"])
    if not path.is_file():
        raise tool_error(VALIDATION_ERROR, f"{field} file is missing: {path}")
    with Image.open(path) as opened:
        image = opened.convert("RGBA").copy()
    if not pixellab_client.is_configured():
        raise tool_error(MCP_ERROR, "PIXELLAB_API_KEY is not set", featureId=feature_id)
    return manifest, record, resolved_game, image


def _native(image: Image.Image) -> Image.Image:
    """A stored sprite back at the canvas it was generated on.

    Sprites are saved at ``_PIXELLAB_UPSCALE`` times their generated size, so
    asking a provider to work on the stored size would be a different request
    at a different canvas — and for rotation, one the endpoint refuses outright.
    """

    size = tuple(max(1, side // _PIXELLAB_UPSCALE) for side in image.size)
    return image if size == image.size else image.resize(size, Image.NEAREST)


@mcp.tool(
    description=(
        "Derive eight facings from one approved sprite through PixelLab's "
        "generate-8-rotations-v2. Eight separate generations produce eight different "
        "characters; this derives all eight from the same reference. The sprite must "
        "have been generated on a square 16, 32, 64, or 128 canvas."
    )
)
@expects_dict_return
def generate_2d_rotations(
    featureId: str,
    sourceAssetId: str,
    gameId: str | None = None,
) -> dict[str, Any]:
    """Eight facings from one approved sprite.

    Facing used to be askable only as prose or as the ``direction`` field, both
    of which re-roll the whole image: asking for the same character eight times
    returns eight characters. This endpoint turns one reference instead, so the
    eight frames are the same subject by construction.

    Each frame lands as its own ``pending`` asset named for its facing, because
    a rotation set is reviewed the way its source was — one bad facing is a bad
    facing, not a bad set.
    """

    feature_id = _require_identifier(featureId, "featureId")
    manifest, source, resolved_game, stored = _approved_source(
        sourceAssetId, gameId, "sourceAssetId", feature_id
    )
    if not _is_pixellab_asset(source):
        raise tool_error(VALIDATION_ERROR, "sourceAssetId must come from PixelLab")

    reference = _native(stored)
    style = load_or_create(ROOT, resolved_game, os.getenv("ASSET_ART_STYLE", DEFAULT_ART_STYLE))
    try:
        images, usage, job_id = pixellab_client.generate_rotations(
            reference=reference, view=style.camera_view
        )
    except pixellab_client.PixelLabUnavailable as exc:
        raise tool_error(
            MCP_ERROR,
            f"PixelLab rotation failed: {exc}",
            featureId=feature_id,
            sourceAssetId=sourceAssetId,
        ) from exc

    kind = source["kind"]
    digest = hashlib.sha256(f"{sourceAssetId}|rotations".encode()).hexdigest()[:8]
    set_id = f"{resolved_game}__{feature_id}__rotations__{digest}"
    out_dir = ROOT / "assets" / resolved_game / "rotations" / f"{feature_id}_{digest}"
    out_dir.mkdir(parents=True, exist_ok=True)

    target = tuple(side * _PIXELLAB_UPSCALE for side in reference.size)
    records: list[dict[str, Any]] = []
    failures: set[str] = set()
    for index, (facing, image) in enumerate(zip(pixellab_client.ROTATION_ORDER, images)):
        if image.size != target:
            image = image.resize(target, Image.NEAREST)
        asset_id = f"{set_id}__{facing}"
        path = out_dir / f"{index:02d}_{facing}.png"
        image.save(path)
        inspection = quality.inspect(image, kind, image.size)
        failures.update(inspection["failures"])
        manifest["assets"][asset_id] = {
            "asset_id": asset_id,
            "feature_id": feature_id,
            "kind": kind,
            "prompt": source.get("prompt", ""),
            "provider_prompt": source.get("provider_prompt", ""),
            "status": PENDING,
            "asset_path": str(path),
            "created_at": _now(),
            "reviewed_at": None,
            "review_note": None,
            "prototype_asset_id": sourceAssetId,
            "rotation_set_id": set_id,
            "direction": facing,
            "provenance": {
                "method": "pixellab-api",
                "endpoint": "generate-8-rotations-v2",
                "job_id": job_id,
                "direction": facing,
                "source_asset_id": sourceAssetId,
                "usage": usage,
                "commercial_use": "see PixelLab terms of service",
            },
        }
        records.append({"assetId": asset_id, "direction": facing, "assetPath": str(path)})
    _save_manifest(manifest)

    return {
        "rotationSetId": set_id,
        "gameId": resolved_game,
        "kind": kind,
        "sourceAssetId": sourceAssetId,
        "status": PENDING,
        "workflowStage": "rotations",
        "rotations": records,
        "technicalStatus": "fail" if failures else "pass",
        "technicalFailures": sorted(failures),
        "imagesGenerated": _images_generated(usage) or len(records),
    }


@mcp.tool(
    description=(
        "Redraw one region of an approved sprite through PixelLab's inpaint-v3 "
        "instead of regenerating the whole thing. maskAssetId names a mask drawn "
        "by a human under var/concept-art/<gameId>/ as concept:<filename>, white "
        "where the region should be redrawn and black where it must be preserved."
    )
)
@expects_dict_return
def inpaint_asset(
    featureId: str,
    sourceAssetId: str,
    description: str,
    maskAssetId: str | None = None,
    gameId: str | None = None,
) -> dict[str, Any]:
    """Repair one region rather than re-rolling the sprite.

    Regenerating to fix one wrong detail throws away every detail that was
    right, and the re-roll is a fresh sample so it rarely returns them. This is
    the repair step PixelLab's own tutorial loop is built around.

    The result is a new ``pending`` asset. The approved original is never
    overwritten: a repair is a proposal, and the human gate that approved the
    original is the one that decides whether the repair replaces it.
    """

    feature_id = _require_identifier(featureId, "featureId")
    instruction = _require(description, "description")
    manifest, source, resolved_game, stored = _approved_source(
        sourceAssetId, gameId, "sourceAssetId", feature_id
    )

    image = _native(stored)
    mask = None
    if maskAssetId:
        mask = _approved_reference(maskAssetId, resolved_game, "maskAssetId", feature_id)
        if mask.size != image.size:
            mask = mask.resize(image.size, Image.NEAREST)

    try:
        repaired, usage, job_id = pixellab_client.inpaint(
            image=image, description=instruction, mask=mask
        )
    except pixellab_client.PixelLabUnavailable as exc:
        raise tool_error(
            MCP_ERROR,
            f"PixelLab inpaint failed: {exc}",
            featureId=feature_id,
            sourceAssetId=sourceAssetId,
        ) from exc

    kind = source["kind"]
    digest = hashlib.sha256(f"{sourceAssetId}|{instruction}".encode()).hexdigest()[:8]
    asset_id = f"{resolved_game}__{feature_id}__{kind}__inpaint{digest}"
    target = tuple(side * _PIXELLAB_UPSCALE for side in image.size)
    if repaired.size != target:
        repaired = repaired.resize(target, Image.NEAREST)
    out_path = _asset_path(resolved_game, feature_id, kind, f"inpaint{digest}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    repaired.save(out_path)

    inspection = quality.inspect(repaired, kind, repaired.size)
    manifest["assets"][asset_id] = {
        "asset_id": asset_id,
        "feature_id": feature_id,
        "kind": kind,
        "prompt": instruction,
        "provider_prompt": instruction,
        "status": PENDING,
        "asset_path": str(out_path),
        "created_at": _now(),
        "reviewed_at": None,
        "review_note": None,
        "prototype_asset_id": sourceAssetId,
        "provenance": {
            "method": "pixellab-api",
            "endpoint": "inpaint-v3",
            "job_id": job_id,
            "source_asset_id": sourceAssetId,
            "mask_asset_id": maskAssetId,
            "masked": mask is not None,
            "usage": usage,
            "commercial_use": "see PixelLab terms of service",
        },
    }
    _save_manifest(manifest)

    return {
        "assetId": asset_id,
        "assetPath": str(out_path),
        "gameId": resolved_game,
        "kind": kind,
        "sourceAssetId": sourceAssetId,
        "masked": mask is not None,
        "status": PENDING,
        "workflowStage": "inpaint",
        "technicalStatus": "fail" if inspection["failures"] else "pass",
        "technicalFailures": sorted(inspection["failures"]),
        "imagesGenerated": _images_generated(usage) or 1,
    }


@mcp.tool(
    description=(
        "Collect a complete asset brief and return a deterministic, kind-aware prompt "
        "plus the briefId generate_2d_sprite requires. This preflight does not generate "
        "an image or call a model. Questions it returns are for the user to answer, not "
        "for the caller to fill in."
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
    gridSize: int | None = None,
    paletteLock: bool | None = None,
    initAssetId: str | None = None,
    initImageStrength: int | None = None,
    direction: str | None = None,
) -> dict[str, Any]:
    """The intake step. It asks about the generation parameters too, not only
    the picture.

    ``gridSize``, ``paletteLock``, ``initAssetId``, ``initImageStrength``, and
    ``direction`` used to be optional arguments on ``generate_2d_sprite`` with
    defaults, so an agent that never asked the user still got a sprite and the
    cost of the guess landed on generation credits and human review. Here they
    are questions like any other: ``None`` means unanswered, ``"none"``/``0``
    mean answered-as-nothing.

    The returned ``briefId`` is what ``generate_2d_sprite`` requires. A brief
    with unanswered required questions is stored all the same — a half-answered
    brief is a real state of the conversation — but generating from it is
    refused until the answers arrive.
    """

    kind = _asset_kind(assetKind)
    assert kind is not None
    brief = prompting.prepare(
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
        grid_size=gridSize,
        palette_lock=paletteLock,
        init_asset_id=initAssetId,
        init_image_strength=initImageStrength,
        direction=direction,
    )
    brief["briefId"] = _save_brief(brief)
    return brief


@mcp.tool(
    description=(
        "Generate the initial 2D style prototype through PixelLab's official MCP. "
        "assetKind is required: it decides the canvas ratio, palette, shading, and "
        "framing, so it is not guessed from prompt wording. Approve the result before "
        "requesting API variations. Pass gridSize to generate one asset at a different "
        "in-world size without changing the game's locked grid, canvas to state the "
        "generated [width, height] outright and skip the kind's fixed ratio, and "
        "direction to turn one asset without changing which way the game faces."
        "Requires a briefId from prepare_asset_prompt whose questions the user has "
        "answered: gridSize, paletteLock, initAssetId, initImageStrength, and direction "
        "come from that brief, not from the caller's judgement. assetKind is required: "
        "it decides the canvas ratio, palette, shading, and framing, so it is not guessed "
        "from prompt wording. Approve the result before requesting API variations."
    )
)
@expects_dict_return
def generate_2d_sprite(
    featureId: str,
    prompt: str,
    assetKind: render.AssetKind,
    briefId: str,
    gameId: str | None = None,
    artStyle: str | None = None,
    gridSize: int | None = None,
    direction: str | None = None,
    paletteLock: bool | None = None,
    poseFromAssetId: str | None = None,
    initAssetId: str | None = None,
    skeletonGuidance: float | None = None,
    initImageStrength: int | None = None,
    canvas: list[int] | None = None,
) -> dict[str, Any]:
    """``briefId`` comes from ``prepare_asset_prompt`` and is required.

    ``gridSize``, ``paletteLock``, ``initAssetId``, ``initImageStrength``, and
    ``direction`` are read from that brief. They may still be passed here, but
    only to restate what the brief already answered — a value that contradicts
    it is refused rather than preferred, so the answer the user gave cannot be
    overridden at the call site. A brief with unanswered required questions
    refuses too, and nothing is billed either way.

    They used to be optional arguments with defaults. Measured on ``daeume``
    (2026-08-22): an agent that never asked the user still generated, and it
    swept ``gridSize`` through 80, 64, 48, 40, and 32, ``paletteLock`` through
    both values, and ``initImageStrength`` through 400 and 700 — ten-plus
    billed generations to rediscover settings one question would have settled.

    ``assetKind`` is required — one of ``character``, ``monster``, ``tile``,
    ``prop``, ``icon``, ``ui_button``, ``ui_panel``.

    It used to be optional and inferred from prompt keywords. One wrong guess
    set the canvas ratio, the forced palette, the shading, and the framing
    together, and the cost landed on generation credits and human review rather
    than on the omitted argument. ``prepare_asset_prompt`` already requires the
    same value.

    ``poseFromAssetId`` names an approved sprite of this game whose joints are
    read with ``/estimate-skeleton`` and handed to this generation as
    coordinates. It is the one control that states a pose outright rather than
    describing it, and it costs one extra billed call. ``skeletonGuidance``
    (0-5) is how closely those joints are followed.

    ``initAssetId`` names an approved sprite to start the generation from, with
    ``initImageStrength`` (1-999) setting how much of it survives.

    Either field also takes ``concept:<filename>``, naming a human-supplied
    drawing in ``var/concept-art/<gameId>/`` instead of a generated asset. That
    is how a settled concept design reaches the generator as pixels rather than
    as prose; a name resolving outside that directory is refused.

    Both require ``create-image-bitforge``, which stops at 200px per side; a
    larger request is refused rather than generated without the pose it asked
    for. PixelLab also warns that keypoints work best on 16x16, 32x32, or
    64x64 — a character is a 1:2 kind, so a posed character comes back with a
    ``warnings`` entry saying so rather than being blocked.

    ``paletteLock`` sends the game's own colours as PixelLab's ``color_image``.
    It is on by default — that is what keeps two assets in one game from
    picking unrelated colours. Turn it off for a single asset whose colours are
    deliberately outside the palette (a boss with its own scheme, a
    colour-coded pickup).

    ``gridSize`` overrides the game's pixel grid for this one asset.

    Same density, different in-world size: a 32 grid character generates
    32x64, a 56 grid character 56x112, and both upscale by the same factor.
    The game's stored style is untouched either way.

    ``canvas`` is ``[width, height]`` used exactly as given. It is the way past
    the kind's fixed ratio: ``gridSize`` scales that ratio, it cannot leave it,
    so a square character was unaskable until this existed. A posed request
    with an explicit canvas is not grown to a square either — the provider's
    weakness on a non-square canvas is reported in ``warnings`` and left to the
    caller. ``canvas`` and ``gridSize`` set the same thing; passing both is
    refused.

    ``direction`` is which way the subject faces — one of ``north``,
    ``north-east``, ``east``, ``south-east``, ``south``, ``south-west``,
    ``west``, ``north-west``. It is PixelLab's own field; before it was wired
    up, facing could only be asked for in the prompt text. Omit it to use the
    game's locked direction.
    """

    # Kind first: it is a free check, and a caller who got the kind wrong should
    # be told that rather than be sent back to the intake step.
    kind = _asset_kind(assetKind)
    brief = _answered_brief(briefId, featureId)
    palette_lock = _brief_parameter(brief, "paletteLock", paletteLock, featureId)
    return _generate_prototype(
        featureId,
        _brief_prompt(brief, prompt, featureId),
        gameId,
        forced_kind=kind,
        art_style=artStyle,
        grid_size=_brief_parameter(brief, "gridSize", gridSize, featureId),
        direction=_brief_parameter(brief, "direction", direction, featureId),
        kind_source="explicit",
        palette_lock=bool(palette_lock),
        pose_from_asset_id=poseFromAssetId,
        init_asset_id=_brief_parameter(brief, "initAssetId", initAssetId, featureId),
        skeleton_guidance=skeletonGuidance,
        init_image_strength=initImageStrength,
        canvas=canvas,
        exclusions=brief.get("exclusions") or [],
    )


@mcp.tool(
    description=(
        "Generate an initial UI prototype through PixelLab's official MCP. Requires a "
        "briefId from prepare_asset_prompt, the same as generate_2d_sprite: it shares "
        "the same generation path, so without one the brief's prompt could be bypassed "
        "by calling this tool instead."
    )
)
@expects_dict_return
def generate_ui_asset(
    featureId: str,
    prompt: str,
    briefId: str,
    gameId: str | None = None,
    artStyle: str | None = None,
    assetKind: str | None = None,
) -> dict[str, Any]:
    """``briefId`` is required here for the reason it is required on
    ``generate_2d_sprite``: both tools share ``_generate_prototype``, so a gate
    on one of them is a gate on neither. This tool took no brief at all, which
    made it the way around the canonical prompt rather than a second path to
    the same check."""

    explicit = _asset_kind(assetKind)
    kind = explicit or render.classify(prompt)
    if not kind.startswith("ui_") and kind != "icon":
        if assetKind is not None:
            raise tool_error(VALIDATION_ERROR, "generate_ui_asset requires a UI assetKind")
        kind = "ui_panel"  # this tool always produces UI, whatever the wording
    # Inference is safe here in a way it was not for sprites: every outcome is
    # a UI kind, so a wrong guess picks the wrong UI shape rather than turning
    # a character into a tile.
    brief = _answered_brief(briefId, featureId)
    return _generate_prototype(
        featureId,
        _brief_prompt(brief, prompt, featureId),
        gameId,
        forced_kind=kind,
        art_style=artStyle,
        kind_source="explicit" if explicit else "inferred",
        exclusions=brief.get("exclusions") or [],
    )


#: Only these carry a style reference the provider can actually weigh, and
#: only the single-reference one exposes the strength/coverage/negative
#: controls. See ``_variation_endpoint``.
_BITFORGE = "create-image-bitforge"
_STYLE_V2 = "generate-with-style-v2"


def _variation_endpoint(reference_count: int, size: tuple[int, int]) -> str:
    """Which style-reference endpoint can serve this batch.

    ``create-image-bitforge`` is preferred: it is the only one with
    ``style_strength``, ``coverage_percentage``, and a live
    ``negative_description``. It takes exactly one reference image and stops
    at 200px per side, so a batch that needs several anchors or a bigger
    canvas falls back to ``generate-with-style-v2``, which takes one to four
    references and deduces the output size from them.

    A **non-square** canvas falls back too. Measured 2026-08-22: a slim
    character asked for at 32x64 through bitforge came back as a detached hat
    floating above a body, while the same prompt at 64x64 came back whole. The
    prototype path answers this by growing the canvas, which it can do because
    it owns the size; a variation batch cannot, because its output has to stay
    the size of the reference it varies. So it takes the endpoint that works at
    that size instead, and gives up the controls bitforge would have added —
    which the caller is told about rather than left to discover.
    """

    high = pixellab_client.BITFORGE_SIDE_RANGE[1]
    square = size[0] == size[1]
    if reference_count == 1 and square and max(size) <= high:
        return _BITFORGE
    return _STYLE_V2


@mcp.tool(
    description=(
        "Generate many same-kind variations through PixelLab's REST API, using an approved "
        "asset as the shared style reference. One reference uses create-image-bitforge and "
        "accepts styleStrength, coveragePercentage, and negativeDescription; several "
        "references fall back to generate-with-style-v2, which has none of those."
    )
)
@expects_dict_return
def generate_2d_variations(
    featureId: str,
    prototypeAssetId: str,
    prompts: list[str],
    gameId: str | None = None,
    styleAssetIds: list[str] | None = None,
    styleStrength: int | None = None,
    coveragePercentage: float | None = None,
    negativeDescription: str = "",
    direction: str | None = None,
    paletteLock: bool = True,
) -> dict[str, Any]:
    """Expand an approved prototype using one to four approved style anchors.

    A character is a 1:2 kind and every character prototype is therefore
    non-square. This used to be rejected outright, which left the only
    style-reference path in the server unusable for exactly the assets whose
    style matters most. Neither endpoint requires a square reference:
    ``generate-with-style-v2`` deduces the output size from the references and
    ``create-image-bitforge`` takes the size it is given.

    ``styleStrength`` (0-100, 50 = balanced), ``coveragePercentage`` (0-100),
    and ``negativeDescription`` exist only on the bitforge path. Passing one
    with several references is an error rather than a silent no-op.
    """

    feature_id = _require_identifier(featureId, "featureId")
    resolved_direction = _direction(direction, feature_id)
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
    if not _is_pixellab_asset(prototype):
        raise tool_error(VALIDATION_ERROR, "prototype asset must come from PixelLab")

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
        if not _is_pixellab_asset(style_record):
            raise tool_error(VALIDATION_ERROR, "every style asset must come from PixelLab")
        if style_record.get("kind") not in _KIND_SIZE_RATIO:
            # A tileset's asset_path is a JSON index, not a sprite; opening it
            # as an image fails with a message about the file, not the choice.
            raise tool_error(
                VALIDATION_ERROR,
                f"style asset {style_asset_id} is a {style_record.get('kind')!r}, "
                "which is not a single-sprite kind",
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
    reference_size = style_images[0].size
    # Stored sprites are ``_PIXELLAB_UPSCALE`` times their generated canvas, so
    # asking the provider for the stored size would generate a 128x256 sprite
    # where the prototype was a 32x64 one — a different asset, and one that
    # bitforge (200px per side) could not draw at all. Generate native, then
    # upscale to match, exactly as the sprite paths do.
    native_size = tuple(max(1, side // _PIXELLAB_UPSCALE) for side in reference_size)
    endpoint = _variation_endpoint(len(style_images), native_size)
    bitforge_only = {
        "styleStrength": styleStrength,
        "coveragePercentage": coveragePercentage,
        "negativeDescription": negativeDescription.strip() or None,
    }
    requested = [name for name, value in bitforge_only.items() if value is not None]
    if endpoint != _BITFORGE and requested:
        raise tool_error(
            VALIDATION_ERROR,
            f"{', '.join(requested)} require the single-reference bitforge path, but this "
            f"batch uses {endpoint} ({len(style_images)} reference(s), "
            f"{native_size[0]}x{native_size[1]} native). That path needs exactly one "
            f"reference on a square canvas no larger than "
            f"{pixellab_client.BITFORGE_SIDE_RANGE[1]}px per side; drop these arguments or "
            "vary a square-kind prototype instead.",
            featureId=feature_id,
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
            if endpoint == _BITFORGE:
                # The negations this prompt already carried are recovered here
                # rather than dropped: the field is live on this endpoint.
                negatives = ", ".join(
                    part
                    for part in (plan.negative_description, negativeDescription.strip())
                    if part
                )
                image, usage = pixellab_client.create_image_bitforge(
                    prompt=plan.prompt,
                    width=native_size[0],
                    height=native_size[1],
                    style_image=style_images[0],
                    style_strength=styleStrength,
                    negative_description=negatives,
                    coverage_percentage=coveragePercentage,
                    seed=seed,
                    forced_palette=_pixellab_palette(style, kind, prompt, paletteLock),
                    **_pixellab_style_params(style, kind, resolved_direction),
                )
                # No downsample: generated at the native canvas, so this is a
                # crisp nearest-neighbour scale to the size the rest of the
                # game's sprites are stored at.
                images, job_id = [image.resize(reference_size, Image.NEAREST)], None
            else:
                images, usage, job_id = pixellab_client.generate_with_style(
                    prompt=plan.prompt,
                    style_images=style_images,
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
        jobs.append(
            {
                "jobId": job_id,
                "endpoint": endpoint,
                "usage": usage,
                "promptMetrics": metrics,
            }
        )
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
                    "endpoint": endpoint,
                    "job_id": job_id,
                    "candidate": candidate_index,
                    "seed": seed,
                    "style_asset_ids": style_asset_ids,
                    "size": list(image.size),
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
        "endpoint": endpoint,
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
    if frameCount not in pixellab_client.ANIMATION_FRAME_COUNTS:
        raise tool_error(
            VALIDATION_ERROR,
            "frameCount must be one of "
            f"{list(pixellab_client.ANIMATION_FRAME_COUNTS)}",
        )

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

    sequence = quality.inspect_sequence(frames, kind)
    records: list[dict[str, Any]] = []
    failures: set[str] = set()
    for index, frame in enumerate(frames):
        asset_id = f"{sequence_id}__{index:02d}"
        # Zero-padded so the play order survives any directory listing.
        path = out_dir / f"{index:02d}_{motion_digest}.png"
        frame.save(path)
        # Frames are inspected here, not only on demand: a sequence that comes
        # back on an opaque plate is unusable as a sprite, and finding that out
        # after it is imported and bound costs a whole round trip.
        inspection = quality.inspect(frame, kind, frame.size)
        failures.update(inspection["failures"])
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
                "technicalStatus": inspection["technicalStatus"],
                "failures": inspection["failures"],
                # Unity anchors a sprite by its canvas unless told otherwise, and the
                # subject sits a few pixels differently in every generated frame.
                "footAnchor": list(sequence["anchors"][index]),
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
                "technicalStatus": "fail" if failures else "pass",
                "technicalFailures": sorted(failures),
                "sequenceWarnings": sequence["warnings"],
                "sequenceMetrics": sequence["metrics"],
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
        "technicalStatus": "fail" if failures else "pass",
        "technicalFailures": sorted(failures),
        "sequenceWarnings": sequence["warnings"],
        "sequenceMetrics": sequence["metrics"],
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

    if tileSize not in pixellab_client.TILE_SIZES:
        # An enum in the schema, not a range: 24 and 48 sit inside 16-64 and
        # are still 422s, and 64 needs a "pro" mode this server never sends.
        raise tool_error(
            VALIDATION_ERROR,
            f"tileSize must be one of {pixellab_client.TILE_SIZES}",
        )

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

    low, high = pixellab_client.MAP_OBJECT_SIDE_RANGE
    if not low <= size <= high:
        raise tool_error(VALIDATION_ERROR, f"size must be between {low} and {high}")

    style = load_or_create(ROOT, resolved_game, os.getenv("ASSET_ART_STYLE", DEFAULT_ART_STYLE))

    try:
        image, usage = pixellab_client.create_map_object(
            description=description,
            width=size,
            height=size,
            color_palette=_pixellab_palette(style, "prop", description),
            **_map_object_style_params(style),
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
        "provenance": {
            "method": "pixellab",
            "endpoint": "map-objects",
            "style_seed": style.seed,
            "style_params": _map_object_style_params(style),
            "usage": usage,
        },
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
def establish_art_style(
    gameId: str,
    artStyle: str = DEFAULT_ART_STYLE,
    detail: str = "",
    shading: str = "",
) -> dict[str, Any]:
    """Freeze the palette before any asset exists.

    Call this right after Planning, using the design document's ``art_style``,
    so every later asset inherits one deliberate look.

    ``detail`` and ``shading`` are PixelLab's structured controls and must be
    values from ``Detail``/``Shading`` — see
    ``pixellab_client.STYLE_ENUMS["create-image-pixflux"]``. They apply on
    first use only, like the rest of the frozen style; an existing game's
    values live in ``var/assets/styles/<gameId>.json``.
    """

    game_id = _require_identifier(gameId, "gameId")
    # Checked before freezing, not at generation time: these are written into
    # var/assets/styles/<gameId>.json and every later asset reads them, so a
    # value outside PixelLab's enum would 422 every asset in the game with no
    # way back short of editing the frozen file.
    allowed = pixellab_client.STYLE_ENUMS["create-image-pixflux"]
    for field, value in (("detail", detail.strip()), ("shading", shading.strip())):
        if value and value not in allowed[field]:
            raise tool_error(
                VALIDATION_ERROR,
                f"{field} must be one of {allowed[field]}",
                gameId=game_id,
            )
    style = load_or_create(
        ROOT,
        game_id,
        artStyle or DEFAULT_ART_STYLE,
        detail=detail.strip(),
        shading=shading.strip(),
    )
    return {
        "gameId": style.game_id,
        "artStyle": style.art_style,
        "palette": style.palette,
        "pixelGrid": style.pixel_grid,
        "seed": style.seed,
        "detail": style.detail,
        "shading": style.shading,
    }


@mcp.tool(description="List assets awaiting human review for a game.")
@expects_dict_return
def list_pending_assets(gameId: str) -> dict[str, Any]:
    game_id = _require_identifier(gameId, "gameId")
    manifest = _load_manifest(game_id)
    pending = [a for a in manifest["assets"].values() if a["status"] == PENDING]
    return {"gameId": game_id, "pending": pending, "count": len(pending)}


@mcp.tool(
    description=(
        "List resumable asset records with bounded pagination. Returns compact records by "
        "default; set detail=true only when full prompt and provenance data is required."
    )
)
@expects_dict_return
def list_assets(
    gameId: str,
    status: str | None = None,
    featureId: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    game_id = _require_identifier(gameId, "gameId")
    if status is not None and status not in (PENDING, APPROVED, REJECTED):
        raise tool_error(VALIDATION_ERROR, f"unsupported status: {status}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise tool_error(VALIDATION_ERROR, "limit must be between 1 and 100")
    if cursor is None:
        offset = 0
    elif not cursor.isdigit():
        raise tool_error(VALIDATION_ERROR, "cursor must be a non-negative integer string")
    else:
        offset = int(cursor)
    feature_id = featureId.strip() if featureId else None
    assets = [
        record
        for record in _load_manifest(game_id)["assets"].values()
        if (status is None or record["status"] == status)
        and (feature_id is None or record["feature_id"] == feature_id)
    ]
    assets.sort(key=lambda record: (record["created_at"], record["asset_id"]))
    page = assets[offset : offset + limit]
    next_offset = offset + len(page)
    if not detail:
        page = [
            {
                "assetId": record["asset_id"],
                "featureId": record["feature_id"],
                "kind": record["kind"],
                "status": record["status"],
                "assetPath": record["asset_path"],
                "createdAt": record["created_at"],
                "reviewFeedback": record.get("review_feedback"),
            }
            for record in page
        ]
    return {
        "gameId": game_id,
        "assets": page,
        "count": len(assets),
        "pageCount": len(page),
        "nextCursor": str(next_offset) if next_offset < len(assets) else None,
        "detail": detail,
    }


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
    # Two different questions, and conflating them sent an approved variation
    # back to "make more variations" instead of "import it".
    #   * can this asset anchor a batch?      -> any approved PixelLab image
    #   * is this asset a batch's *source*?   -> a prototype, not its output
    can_anchor = _is_pixellab_asset(record)
    is_prototype = can_anchor and not record.get("batch_id")
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
            technical_status == "pass" and human_review_status == APPROVED and can_anchor
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
