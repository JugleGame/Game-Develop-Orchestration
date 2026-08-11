"""LangGraph state definition. Nodes may only read/write this shape (CLAUDE.md)."""

from typing import Any, TypedDict


class Stage:
    """Node/stage names, matching §3 of the state diagram."""

    INTAKE = "Intake"
    CONCEPT_PROPOSE = "ConceptPropose"
    CONCEPT_GATE = "ConceptGate"
    PLANNING = "Planning"
    APPROVAL_GATE = "ApprovalGate"
    CODE_GEN = "CodeGen"
    ASSET_GEN = "AssetGen"
    BUILD_PROTOTYPE = "BuildPrototype"
    COMPILE_CHECK = "CompileCheck"
    ERROR_CORRECTION = "ErrorCorrection"
    RUNTIME_CHECK = "RuntimeCheck"
    STRUCTURE_COMPARE = "StructureCompare"
    FUNCTIONAL_TEST = "FunctionalTest"
    ASSET_REVIEW = "AssetReview"
    DEPLOYMENT = "Deployment"
    HUMAN_ESCALATION = "HumanEscalation"


class GraphState(TypedDict, total=False):
    """Full orchestration state threaded through every LangGraph node."""

    game_id: str
    prompt: str
    status: str
    current_stage: str

    game_design: dict[str, Any] | None
    feature_prompts: list[dict[str, Any]]
    repo_name: str | None
    # ``unity.design_architecture`` result: the files, prefabs and scene layout
    # of the whole game, decided before the first line of C# is written. Held
    # here so a retry reuses the same decomposition instead of re-rolling it —
    # a plan re-decided each round gives a different structure each round, which
    # is the known cost of letting the developer side own the split
    # (Doc/설계/06 §3.1).
    architecture: dict[str, Any] | None
    created_files: list[str]
    # Positional parallel of ``created_files`` carrying each script's source and
    # type name. CodeGen needs the source to repair a file on retry instead of
    # rewriting it, and the type names to tell the next script what already
    # exists. Kept beside ``created_files`` rather than replacing it because
    # ErrorCorrection matches compiler paths against that list by index.
    created_scripts: list[dict[str, Any]]
    # Which round of the dev/QA retry loop CodeGen last ran. Emitted by the node
    # rather than derived by the reader, because a stream consumer only ever
    # sees one node's partial update and cannot sum the two counters itself.
    codegen_attempt: int
    # The exact blueprint summary CodeGen sent with this attempt. Derived from
    # ``game_design``, but recorded as sent: the renderer may change later, and
    # a trace that says what the model was told has to stay true regardless.
    codegen_project_context: str
    generated_assets: list[dict[str, Any]]

    build_result: dict[str, Any] | None
    compile_errors: list[dict[str, Any]]
    # RuntimeCheck's raw result ({passed, durationSeconds, errorCount, errors}).
    # Folded into the `build` reference QA reads (qa_common.build_reference) so
    # QA judges against an actual playmode pass instead of compile metadata alone.
    runtime_check: dict[str, Any] | None
    # ``unity.inspect_project_layout`` result ({counts, ok, findings}) — which
    # scripts are attached to a scene or prefab, and which are not. Folded into
    # the same `build` reference. A script that exists as a file but is attached
    # to nothing never runs, and neither the build result nor the playmode log
    # says so, which is how a prototype with five unattached scripts reached
    # QA PASS (Doc/설계/06 §2.1).
    project_layout: dict[str, Any] | None

    qa_policy: dict[str, Any] | None
    structure_check: dict[str, Any] | None
    qa_passed: bool | None
    last_error: dict[str, Any] | None
    # AssetReview's raw asset_review_summary result ({pending, approved,
    # rejected, readyForBuild, ...}). Only an explicit rejection blocks
    # Deployment; pending (never reviewed) assets pass through (doc05 §4).
    asset_review: dict[str, Any] | None

    dev_iteration_count: int
    qa_iteration_count: int

    approved: bool | None
    # Set by ApprovalGate on rejection; consumed (and cleared) by Planning so
    # a re-plan actually addresses the user's objection instead of re-rolling.
    planning_feedback: str | None
    replan_count: int

    # ConceptGate (§3.1). The idea is reviewed on evidence alone before any
    # blueprint is written, so a wrong direction costs a DB query rather than
    # the pipeline's most expensive LLM call.
    concept: dict[str, Any] | None
    concept_approved: bool | None
    # The approved (possibly user-edited) idea. Planning prompts with this
    # rather than the raw intake prompt whenever ConceptGate produced one.
    approved_idea: str | None
    concept_iteration_count: int

    artifact: dict[str, Any] | None
