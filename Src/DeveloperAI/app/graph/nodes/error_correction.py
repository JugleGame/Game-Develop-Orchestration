"""ErrorCorrection node: records the compile failure and bumps the retry counter.

The node's one non-obvious job is *attributing* the failure. ``CodeGen`` only
regenerates the features named by ``last_error["related_feature_id"]``, and it
reads an empty id as "every feature" — so leaving the id blank turns a single
bad file into a full regeneration of all N scripts, once per retry round. The
compiler already tells us which file failed and ``created_files`` records which
file each feature produced, so the attribution is a lookup, not a guess.
"""

from pathlib import PurePath

from app.graph.state import GraphState, Stage
from app.models.schemas import ErrorType, JobStatus
from app.utils.ordering import order_by_dependencies


def _basename(path: str) -> str:
    """Filename of ``path``, tolerating either separator.

    Unity reports absolute Windows paths while ``created_files`` holds the
    project-relative ``Assets/Scripts/Foo.cs`` the Unity server wrote, so the
    two are only comparable at the leaf.
    """

    return PurePath(str(path).replace("\\", "/")).name


def _related_feature_id(state: GraphState, error_file: str | None) -> str:
    """Map a compile error's file back to the feature that generated it.

    Returns ``""`` when the file cannot be attributed — which ``CodeGen`` reads
    as "every feature", the behaviour this node had unconditionally before.
    Failing to attribute therefore costs a wider regeneration, never a wrong
    one: the fallback is the old, safe path.

    ``created_files`` is positionally aligned with
    ``order_by_dependencies(feature_prompts)`` because ``CodeGen`` appends in
    exactly that order, and the ordering is stable for a given feature list.
    """

    if not error_file:
        return ""

    target = _basename(error_file)
    ordered = order_by_dependencies(state.get("feature_prompts", []))
    for feature, created_path in zip(ordered, state.get("created_files") or []):
        if created_path and _basename(created_path) == target:
            return feature["feature_id"]
    return ""


async def error_correction_node(state: GraphState) -> dict:
    compile_errors = state.get("compile_errors", [])
    first_error = compile_errors[0] if compile_errors else {}
    error_file = first_error.get("file")
    last_error = {
        "error_type": ErrorType.COMPILE.value,
        "message": first_error.get("message", "Unknown compile error"),
        "file": error_file,
        "line": first_error.get("line"),
        "suggested_fix": "Regenerate the affected script and rebuild.",
        "related_feature_id": _related_feature_id(state, error_file),
    }
    return {
        "current_stage": Stage.ERROR_CORRECTION,
        "status": JobStatus.DEVELOPING.value,
        "last_error": last_error,
        "dev_iteration_count": state.get("dev_iteration_count", 0) + 1,
    }
