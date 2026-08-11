"""ConceptGate: human review of the idea *before* any blueprint is written.

Writing the blueprint is the most expensive LLM step in the pipeline, and a
blueprint written for the wrong idea is a total loss — the tokens buy nothing
and the user still has to explain what they actually wanted. This gate puts a
cheap check in front of it: gather supporting and counter-evidence from the
research DB (no model call at all), pause, and let the user approve, edit, or
reject.

**Two nodes, for the same reason Planning and ApprovalGate are two nodes.**
``interrupt()`` suspends the node it is called from, so that node never returns
its state update. A single node that both proposed and paused would leave
``concept`` unwritten, and the API layer — which reads the job row, not the
graph checkpoint — would have nothing to show the user to approve. So
``ConceptPropose`` fetches and commits the proposal, and ``ConceptGate``
pauses on the already-persisted state.

``ConceptGate`` is resumed by ``GameService.approve`` with
``Command(resume={...})``, using ApprovalGate's ``{approved, feedback}`` shape
plus ``edited_idea``:

* ``approved=True``  -> ``decide_concept(approve)``, on to Planning.
* ``approved=False`` -> ``decide_concept(revise, editedIdea=..., note=...)``,
  back to ``ConceptPropose`` — bounded by ``CONCEPT_MAX_ITERATIONS``.

A rejection is expressed as a revise round rather than
``decide_concept(reject)`` because the graph always needs somewhere to go next:
exhausting the rounds routes to ``HumanEscalation``, which is where an outright
reject would land anyway.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from langgraph.types import interrupt

from app.graph.state import GraphState, Stage
from app.mcp.strategic_client import StrategicClient
from app.models.schemas import JobStatus

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def current_idea(state: GraphState) -> str:
    """The idea under review: the user's edit if there is one, else intake."""

    return state.get("approved_idea") or state["prompt"]


def build_concept_propose_node(client: StrategicClient) -> NodeFn:
    async def concept_propose_node(state: GraphState) -> dict:
        proposal = await client.propose_concept(
            game_id=state["game_id"], idea=current_idea(state)
        )
        return {
            "current_stage": Stage.CONCEPT_PROPOSE,
            "status": JobStatus.PLANNING.value,
            # Committed here so the API can render it while the next node is
            # suspended waiting for the user to read exactly this.
            "concept": proposal,
        }

    return concept_propose_node


def build_concept_gate_node(client: StrategicClient) -> NodeFn:
    async def concept_gate_node(state: GraphState) -> dict:
        game_id = state["game_id"]
        idea = current_idea(state)

        decision = interrupt(
            {
                "stage": Stage.CONCEPT_GATE,
                "concept": state.get("concept"),
            }
        )

        approved = bool(decision.get("approved", False))
        feedback = decision.get("feedback") or ""
        edited_idea = decision.get("edited_idea") or ""

        if approved:
            result = await client.decide_concept(
                game_id=game_id, decision="approve", note=feedback
            )
            return {
                "current_stage": Stage.CONCEPT_GATE,
                "status": JobStatus.PLANNING.value,
                "concept": result,
                "concept_approved": True,
                # What Planning will actually plan from.
                "approved_idea": result.get("idea") or idea,
            }

        result = await client.decide_concept(
            game_id=game_id,
            decision="revise",
            note=feedback,
            edited_idea=edited_idea,
        )
        return {
            "current_stage": Stage.CONCEPT_GATE,
            "status": JobStatus.PLANNING.value,
            "concept": result,
            "concept_approved": False,
            # Carry the revised wording so the next round proposes on it.
            "approved_idea": result.get("idea") or edited_idea or idea,
            "concept_iteration_count": state.get("concept_iteration_count", 0) + 1,
        }

    return concept_gate_node
