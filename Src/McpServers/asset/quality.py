"""Deterministic technical checks for generated raster assets.

These checks catch measurable production defects. They do not claim that an
image matches a user's concept; semantic judgment belongs to the host agent
and final approval remains human metadata.
"""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageChops, ImageStat


_ISOLATED_KINDS = frozenset({"character", "monster", "prop", "icon"})
_MAX_TILE_SEAM_DELTA = 32

# ponytail: pixel thresholds calibrated against one measured sequence set
# (sanabi-style-prototype, 2026-08-15: 4-16px horizontal drift read as visible
# shake at 64 pixels per unit). Replace with per-project calibration when more
# reviewed sequences exist.
_MAX_ANCHOR_DRIFT_PIXELS = 6
_MAX_SIZE_DRIFT_PIXELS = 12


def _edge_delta(image: Image.Image, first_x: int, second_x: int) -> float:
    rgb = image.convert("RGB")
    first = rgb.crop((first_x, 0, first_x + 1, rgb.height))
    second = rgb.crop((second_x, 0, second_x + 1, rgb.height))
    return sum(ImageStat.Stat(ImageChops.difference(first, second)).mean) / 3


def inspect(image: Image.Image, kind: str, expected_size: tuple[int, int]) -> dict[str, Any]:
    """Return stable measurements, hard failures, and review warnings."""

    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    occupied = rgba.width * rgba.height - alpha.histogram()[0]
    coverage = occupied / (rgba.width * rgba.height)
    bbox = alpha.getbbox()
    failures: list[str] = []
    warnings: list[str] = []

    if rgba.size != expected_size:
        failures.append("canvas_size_mismatch")
    if kind in _ISOLATED_KINDS:
        if coverage >= 0.98:
            failures.append("transparent_background_missing")
        elif coverage < 0.03:
            failures.append("subject_too_small")
        if bbox:
            left, top, right, bottom = bbox
            if left == 0 or top == 0 or right == rgba.width or bottom == rgba.height:
                warnings.append("subject_may_be_clipped")
    elif kind == "tile" and coverage < 0.99:
        failures.append("tile_has_transparent_gaps")

    horizontal_seam_delta: float | None = None
    if kind == "tile":
        horizontal_seam_delta = _edge_delta(rgba, 0, rgba.width - 1)
        # ponytail: this conservative pixel-distance threshold is global;
        # replace it with per-project calibration when real review data exists.
        if horizontal_seam_delta > _MAX_TILE_SEAM_DELTA:
            failures.append("horizontal_tile_seam_mismatch")

    return {
        "technicalStatus": "fail" if failures else "pass",
        "failures": failures,
        "warnings": warnings,
        "metrics": {
            "width": rgba.width,
            "height": rgba.height,
            "expectedWidth": expected_size[0],
            "expectedHeight": expected_size[1],
            "alphaCoverage": round(coverage, 4),
            "boundingBox": list(bbox) if bbox else None,
            "horizontalSeamMeanRgbDelta": (
                round(horizontal_seam_delta, 2)
                if horizontal_seam_delta is not None
                else None
            ),
        },
        "requiresSemanticReview": True,
    }


def _subject_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    return image.convert("RGBA").getchannel("A").getbbox()


def foot_anchor(image: Image.Image) -> tuple[float, float]:
    """Normalised pivot at the bottom centre of the subject, not of the canvas.

    A generated frame places its subject a few pixels differently each time. Unity
    anchors a sprite by its canvas unless told otherwise, so that difference becomes
    on-screen shake. Anchoring every frame to its own feet cancels it without
    touching a pixel of the art.
    """

    rgba = image.convert("RGBA")
    box = _subject_box(rgba)
    if box is None:
        return 0.5, 0.0

    left, _, right, lower = box
    return round(((left + right) / 2) / rgba.width, 5), round(1 - lower / rgba.height, 5)


def inspect_sequence(frames: list[Image.Image], kind: str) -> dict[str, Any]:
    """Measure what a single-frame check cannot: does this play as one motion?

    Every frame of the prototype's player sequences passed ``inspect`` and the
    result still shook on screen. The measurements that would have caught it are
    how far the subject wanders between frames, how much of the canvas is redrawn,
    and how far the last frame sits from the first when the clip loops.
    """

    if len(frames) < 2:
        return {
            "frameCount": len(frames),
            "warnings": [],
            "metrics": {},
            "anchors": [foot_anchor(frame) for frame in frames],
        }

    rgba = [frame.convert("RGBA") for frame in frames]
    boxes = [_subject_box(frame) for frame in rgba]
    known = [box for box in boxes if box is not None]
    warnings: list[str] = []

    anchor_drift = 0
    size_drift = 0
    if known:
        centres = [(box[0] + box[2]) / 2 for box in known]
        bottoms = [box[3] for box in known]
        widths = [box[2] - box[0] for box in known]
        heights = [box[3] - box[1] for box in known]
        anchor_drift = round(max(max(centres) - min(centres), max(bottoms) - min(bottoms)), 2)
        size_drift = round(max(max(widths) - min(widths), max(heights) - min(heights)), 2)

    # alpha_only defaults to True on RGBA, which would ignore a frame that changed
    # colour without changing silhouette.
    changed = []
    for index in range(1, len(rgba)):
        box = ImageChops.difference(rgba[index - 1], rgba[index]).getbbox(alpha_only=False)
        changed.append(0 if box is None else (box[2] - box[0]) * (box[3] - box[1]))

    loop_box = ImageChops.difference(rgba[-1], rgba[0]).getbbox(alpha_only=False)
    loop_change = 0 if loop_box is None else (loop_box[2] - loop_box[0]) * (loop_box[3] - loop_box[1])
    canvas = rgba[0].width * rgba[0].height

    if kind in _ISOLATED_KINDS:
        if anchor_drift > _MAX_ANCHOR_DRIFT_PIXELS:
            warnings.append("subject_drifts_between_frames")
        if size_drift > _MAX_SIZE_DRIFT_PIXELS:
            warnings.append("subject_size_unstable")
    if changed and max(changed) >= canvas * 0.9:
        warnings.append("frames_redrawn_rather_than_animated")
    if loop_change >= canvas * 0.9:
        warnings.append("loop_seam_jumps")

    return {
        "frameCount": len(rgba),
        # Warnings, never failures: a sequence that legitimately crosses the canvas
        # looks the same to these measurements as one that shakes in place.
        "warnings": warnings,
        "metrics": {
            "anchorDriftPixels": anchor_drift,
            "sizeDriftPixels": size_drift,
            "changedAreaPerFrame": changed,
            "maxChangedFraction": round(max(changed) / canvas, 4) if changed else 0.0,
            "loopSeamChangedFraction": round(loop_change / canvas, 4),
        },
        "anchors": [foot_anchor(frame) for frame in rgba],
    }


__all__ = ["inspect", "inspect_sequence", "foot_anchor"]
