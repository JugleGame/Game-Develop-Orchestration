"""Builds the LangGraph StateGraph described in §3 of the design spec."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph.nodes.approval_gate import approval_gate_node
from app.graph.nodes.asset_review import asset_review_blocked, build_asset_review_node
from app.graph.nodes.assetgen import build_assetgen_node
from app.graph.nodes.build import build_build_prototype_node
from app.graph.nodes.codegen import build_codegen_node
from app.graph.nodes.compile_check import build_compile_check_node, compile_succeeded
from app.graph.nodes.concept_gate import build_concept_gate_node, build_concept_propose_node
from app.graph.nodes.deployment import build_deployment_node
from app.graph.nodes.error_correction import error_correction_node
from app.graph.nodes.functional_test import build_functional_test_node
from app.graph.nodes.human_escalation import human_escalation_node
from app.graph.nodes.intake import intake_node
from app.graph.nodes.planning import build_planning_node
from app.graph.nodes.runtime_check import build_runtime_check_node
from app.graph.nodes.structure_compare import build_structure_compare_node
from app.graph.state import GraphState, Stage
from app.mcp.clients import ToolClients


def _build_route_after_concept(max_concept_rounds: int):
    def _route(state: GraphState) -> str:
        if state.get("concept_approved"):
            return Stage.PLANNING
        # Bounded like every other loop (§3.3). Revise rounds are cheap — no
        # LLM call — but an unbounded gate would still never reach a human.
        if state.get("concept_iteration_count", 0) >= max_concept_rounds:
            return Stage.HUMAN_ESCALATION
        # Back to the proposing node, not the gate: a revise round must fetch
        # fresh evidence for the edited idea before asking again.
        return Stage.CONCEPT_PROPOSE

    return _route


def _build_route_after_approval(max_replans: int):
    def _route(state: GraphState) -> str:
        if state.get("approved"):
            return Stage.CODE_GEN
        # Bounded like the dev/QA loops (§3.3): endless replan rounds burn
        # LLM budget with no exit, so hand over to a human instead.
        if state.get("replan_count", 0) >= max_replans:
            return Stage.HUMAN_ESCALATION
        return Stage.PLANNING

    return _route


def _route_after_compile_check(state: GraphState) -> str:
    return Stage.RUNTIME_CHECK if compile_succeeded(state) else Stage.ERROR_CORRECTION


def _build_route_after_error_correction(max_iterations: int):
    def _route(state: GraphState) -> str:
        if state.get("dev_iteration_count", 0) >= max_iterations:
            return Stage.HUMAN_ESCALATION
        return Stage.CODE_GEN

    return _route


def _build_route_after_functional_test(max_iterations: int):
    def _route(state: GraphState) -> str:
        if state.get("qa_passed"):
            return Stage.ASSET_REVIEW
        if state.get("qa_iteration_count", 0) >= max_iterations:
            return Stage.HUMAN_ESCALATION
        return Stage.CODE_GEN

    return _route


def _route_after_asset_review(state: GraphState) -> str:
    # No retry loop here: a rejection needs a human's judgment call (regenerate,
    # override, or abandon the feature), not an automated retry (doc05 §4).
    return Stage.HUMAN_ESCALATION if asset_review_blocked(state) else Stage.DEPLOYMENT


def build_graph(
    clients: ToolClients,
    *,
    max_iterations: int,
    planning_max_iterations: int | None = None,
    concept_max_iterations: int | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Wire every node and conditional edge of the DeveloperAI workflow.

    ``planning_max_iterations`` caps the ApprovalGate->Planning rejection
    loop; it defaults to ``max_iterations`` so existing callers keep a
    single knob. ``concept_max_iterations`` does the same for the
    ConceptGate revise loop.
    """

    max_replans = planning_max_iterations if planning_max_iterations is not None else max_iterations
    max_concept_rounds = (
        concept_max_iterations if concept_max_iterations is not None else max_iterations
    )

    graph: StateGraph = StateGraph(GraphState)

    graph.add_node(Stage.INTAKE, intake_node)
    graph.add_node(Stage.CONCEPT_PROPOSE, build_concept_propose_node(clients.strategic))
    graph.add_node(Stage.CONCEPT_GATE, build_concept_gate_node(clients.strategic))
    graph.add_node(Stage.PLANNING, build_planning_node(clients.strategic))
    graph.add_node(Stage.APPROVAL_GATE, approval_gate_node)
    graph.add_node(Stage.CODE_GEN, build_codegen_node(clients.unity))
    graph.add_node(Stage.ASSET_GEN, build_assetgen_node(clients.asset, clients.unity))
    graph.add_node(Stage.BUILD_PROTOTYPE, build_build_prototype_node(clients.unity))
    graph.add_node(Stage.COMPILE_CHECK, build_compile_check_node(clients.unity))
    graph.add_node(Stage.ERROR_CORRECTION, error_correction_node)
    graph.add_node(Stage.RUNTIME_CHECK, build_runtime_check_node(clients.unity))
    graph.add_node(Stage.STRUCTURE_COMPARE, build_structure_compare_node(clients.qa))
    graph.add_node(Stage.FUNCTIONAL_TEST, build_functional_test_node(clients.qa))
    graph.add_node(Stage.ASSET_REVIEW, build_asset_review_node(clients.asset))
    graph.add_node(Stage.DEPLOYMENT, build_deployment_node(clients.git))
    graph.add_node(Stage.HUMAN_ESCALATION, human_escalation_node)

    graph.add_edge(START, Stage.INTAKE)
    # ConceptGate sits ahead of Planning on purpose: the idea is reviewed on
    # research evidence alone, before the pipeline's most expensive LLM call.
    graph.add_edge(Stage.INTAKE, Stage.CONCEPT_PROPOSE)
    graph.add_edge(Stage.CONCEPT_PROPOSE, Stage.CONCEPT_GATE)
    graph.add_conditional_edges(
        Stage.CONCEPT_GATE,
        _build_route_after_concept(max_concept_rounds),
        {
            Stage.PLANNING: Stage.PLANNING,
            Stage.CONCEPT_PROPOSE: Stage.CONCEPT_PROPOSE,
            Stage.HUMAN_ESCALATION: Stage.HUMAN_ESCALATION,
        },
    )
    graph.add_edge(Stage.PLANNING, Stage.APPROVAL_GATE)

    graph.add_conditional_edges(
        Stage.APPROVAL_GATE,
        _build_route_after_approval(max_replans),
        {
            Stage.CODE_GEN: Stage.CODE_GEN,
            Stage.PLANNING: Stage.PLANNING,
            Stage.HUMAN_ESCALATION: Stage.HUMAN_ESCALATION,
        },
    )

    graph.add_edge(Stage.CODE_GEN, Stage.ASSET_GEN)
    graph.add_edge(Stage.ASSET_GEN, Stage.BUILD_PROTOTYPE)
    graph.add_edge(Stage.BUILD_PROTOTYPE, Stage.COMPILE_CHECK)

    graph.add_conditional_edges(
        Stage.COMPILE_CHECK,
        _route_after_compile_check,
        {
            Stage.RUNTIME_CHECK: Stage.RUNTIME_CHECK,
            Stage.ERROR_CORRECTION: Stage.ERROR_CORRECTION,
        },
    )
    graph.add_conditional_edges(
        Stage.ERROR_CORRECTION,
        _build_route_after_error_correction(max_iterations),
        {Stage.CODE_GEN: Stage.CODE_GEN, Stage.HUMAN_ESCALATION: Stage.HUMAN_ESCALATION},
    )

    graph.add_edge(Stage.RUNTIME_CHECK, Stage.STRUCTURE_COMPARE)
    graph.add_edge(Stage.STRUCTURE_COMPARE, Stage.FUNCTIONAL_TEST)
    graph.add_conditional_edges(
        Stage.FUNCTIONAL_TEST,
        _build_route_after_functional_test(max_iterations),
        {
            Stage.ASSET_REVIEW: Stage.ASSET_REVIEW,
            Stage.HUMAN_ESCALATION: Stage.HUMAN_ESCALATION,
            Stage.CODE_GEN: Stage.CODE_GEN,
        },
    )
    graph.add_conditional_edges(
        Stage.ASSET_REVIEW,
        _route_after_asset_review,
        {Stage.DEPLOYMENT: Stage.DEPLOYMENT, Stage.HUMAN_ESCALATION: Stage.HUMAN_ESCALATION},
    )

    graph.add_edge(Stage.DEPLOYMENT, END)
    graph.add_edge(Stage.HUMAN_ESCALATION, END)

    return graph.compile(checkpointer=checkpointer)
