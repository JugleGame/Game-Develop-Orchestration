"""Shared helper for QA nodes: serializes the raw build result as a reference string."""

import json

from app.graph.state import GraphState


def build_reference(state: GraphState) -> str:
    """QaMcpServer's ``build`` field is an opaque string; forward the full
    build result JSON since no dedicated build-id field is specified.

    RuntimeCheck's playmode result is folded in under ``runtimeCheck`` when
    present, so QA judges against an actual playmode pass (console errors,
    pass/fail) rather than compile metadata alone.

    ``projectLayout`` carries which scripts are actually attached to a scene or
    prefab. Without it the structure judge is asked whether the prototype is
    wired together while being shown only build metadata — a question it cannot
    answer, so it fell back to judging mechanic coverage alone and passed a
    build whose scripts were attached to nothing. Facts the judge does not
    receive are facts it cannot weigh.
    """

    payload = dict(state.get("build_result") or {})
    runtime_check = state.get("runtime_check")
    if runtime_check is not None:
        payload["runtimeCheck"] = runtime_check
    project_layout = state.get("project_layout")
    if project_layout is not None:
        payload["projectLayout"] = project_layout
    return json.dumps(payload)
