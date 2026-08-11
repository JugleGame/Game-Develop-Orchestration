"""ErrorCorrection must name the feature whose script failed to compile.

Without attribution, ``CodeGen`` reads the blank ``related_feature_id`` as
"every feature" and regenerates all N scripts for one bad file, on every retry
round. These tests pin the mapping and — just as importantly — pin the
fallback, so a path Unity reports in a shape we did not anticipate widens the
retry instead of silently regenerating the wrong script.
"""

import pytest

from app.graph.nodes.error_correction import error_correction_node

FEATURES = [
    {"feature_id": "spec-001", "description": "player movement", "dependencies": []},
    {"feature_id": "spec-002", "description": "inventory", "dependencies": []},
    {"feature_id": "spec-003", "description": "day/night cycle", "dependencies": []},
]

CREATED = [
    "Assets/Scripts/Spec001.cs",
    "Assets/Scripts/Spec002.cs",
    "Assets/Scripts/Spec003.cs",
]


def _state(**overrides):
    base = {
        "game_id": "g1",
        "feature_prompts": FEATURES,
        "created_files": CREATED,
        "dev_iteration_count": 0,
    }
    return {**base, **overrides}


@pytest.mark.asyncio
async def test_attributes_error_to_the_feature_that_produced_the_file() -> None:
    state = _state(
        compile_errors=[
            {"file": "Assets/Scripts/Spec002.cs", "line": 42, "message": "CS1002: ; expected"}
        ]
    )

    result = await error_correction_node(state)

    assert result["last_error"]["related_feature_id"] == "spec-002"


@pytest.mark.asyncio
async def test_matches_absolute_windows_path_against_relative_created_file() -> None:
    """Unity reports an absolute path; ``created_files`` holds a relative one."""

    state = _state(
        compile_errors=[
            {
                "file": r"C:\Users\dev\Games\g1\Assets\Scripts\Spec003.cs",
                "line": 7,
                "message": "CS0103: name not found",
            }
        ]
    )

    result = await error_correction_node(state)

    assert result["last_error"]["related_feature_id"] == "spec-003"


@pytest.mark.asyncio
async def test_unattributable_file_falls_back_to_every_feature() -> None:
    """An unknown file must widen the retry, never pick an arbitrary feature."""

    state = _state(
        compile_errors=[{"file": "Assets/Scripts/Unknown.cs", "line": 1, "message": "CS0246"}]
    )

    result = await error_correction_node(state)

    assert result["last_error"]["related_feature_id"] == ""


@pytest.mark.asyncio
async def test_missing_file_field_falls_back_to_every_feature() -> None:
    state = _state(compile_errors=[{"message": "CS0016: could not write output"}])

    result = await error_correction_node(state)

    assert result["last_error"]["related_feature_id"] == ""
    assert result["last_error"]["file"] is None


@pytest.mark.asyncio
async def test_first_pass_has_no_created_files_yet() -> None:
    """A compile failure before any file is recorded must not raise."""

    state = _state(
        created_files=[],
        compile_errors=[{"file": "Assets/Scripts/Spec001.cs", "message": "CS1002"}],
    )

    result = await error_correction_node(state)

    assert result["last_error"]["related_feature_id"] == ""
    assert result["dev_iteration_count"] == 1
