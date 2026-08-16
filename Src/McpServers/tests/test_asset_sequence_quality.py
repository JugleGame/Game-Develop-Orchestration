"""Sequence-level checks for generated animation frames (Issue #35).

``quality.inspect`` looks at one image. These measurements look at the frames as a
motion: the prototype's player sequences passed every single-frame check and still
shook on screen.
"""

from __future__ import annotations

import pytest
from PIL import Image

from asset import quality


def _frame(size=(64, 64), left=20, bottom=60, width=24, height=40, colour=(10, 20, 30, 255)):
    """One subject rectangle on transparent ground, placed where asked."""

    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste(colour, (left, bottom - height, left + width, bottom))
    return image


def test_foot_anchor_sits_at_the_bottom_centre_of_the_subject():
    image = _frame(left=20, bottom=60, width=24, height=40)

    pivot_x, pivot_y = quality.foot_anchor(image)

    assert pivot_x == pytest.approx(32 / 64, abs=0.01)
    assert pivot_y == pytest.approx(1 - 60 / 64, abs=0.01)


def test_foot_anchor_of_an_empty_frame_falls_back_to_the_canvas():
    assert quality.foot_anchor(Image.new("RGBA", (32, 32), (0, 0, 0, 0))) == (0.5, 0.0)


def test_steady_sequence_reports_no_warnings():
    frames = [_frame(left=20 + (index % 2), bottom=60) for index in range(6)]

    report = quality.inspect_sequence(frames, "character")

    assert report["warnings"] == []
    assert report["metrics"]["anchorDriftPixels"] <= 2
    assert len(report["anchors"]) == 6


def test_drifting_subject_is_reported():
    """The measured defect: the subject wanders across the canvas frame to frame."""

    frames = [_frame(left=8 + index * 4, bottom=60) for index in range(6)]

    report = quality.inspect_sequence(frames, "character")

    assert "subject_drifts_between_frames" in report["warnings"]
    assert report["metrics"]["anchorDriftPixels"] >= 6


def test_unstable_subject_size_is_reported():
    frames = [_frame(left=20, bottom=60, width=24 + index * 4) for index in range(6)]

    report = quality.inspect_sequence(frames, "character")

    assert "subject_size_unstable" in report["warnings"]


def test_full_redraw_and_loop_seam_are_reported():
    frames = [
        Image.new("RGBA", (32, 32), (255, 0, 0, 255)),
        Image.new("RGBA", (32, 32), (0, 255, 0, 255)),
    ]

    report = quality.inspect_sequence(frames, "character")

    assert "frames_redrawn_rather_than_animated" in report["warnings"]
    assert "loop_seam_jumps" in report["warnings"]


def test_a_single_frame_sequence_is_not_judged():
    report = quality.inspect_sequence([_frame()], "character")

    assert report["warnings"] == []
    assert report["frameCount"] == 1


def test_tiles_are_not_measured_for_subject_drift():
    """A tile has no subject to anchor; drift rules would fire on every set."""

    frames = [_frame(left=8 + index * 8, bottom=60) for index in range(4)]

    report = quality.inspect_sequence(frames, "tile")

    assert "subject_drifts_between_frames" not in report["warnings"]
