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
            left, top, right, _bottom = bbox
            if left == 0 or top == 0 or right == rgba.width:
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
        "status": "fail" if failures else "pass",
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


__all__ = ["inspect"]
