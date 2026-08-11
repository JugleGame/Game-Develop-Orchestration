"""ConceptGate: the cheap gate that protects the expensive one.

The point of this node is ordering — the idea is reviewed *before* Planning
spends the pipeline's largest LLM call. These tests pin that ordering, the
bound on the revise loop, and the two things easy to get subtly wrong: that an
approved edit is what Planning actually plans from, and that a revise round
re-gathers evidence instead of re-showing the stale proposal.
"""

from typing import Any

import pytest

from app.graph.graph import _build_route_after_concept
from app.graph.nodes.concept_gate import (
    build_concept_gate_node,
    build_concept_propose_node,
)
from app.graph.state import Stage


class _StubStrategic:
    """Records calls; returns the shapes StrategicMcpServer returns."""

    def __init__(self) -> None:
        self.proposed: list[dict[str, Any]] = []
        self.decided: list[dict[str, Any]] = []

    async def propose_concept(self, *, game_id: str, idea: str) -> dict[str, Any]:
        self.proposed.append({"game_id": game_id, "idea": idea})
        return {
            "gameId": game_id,
            "version": len(self.proposed),
            "idea": idea,
            "status": "pending",
            "evidence": {"supporting": [], "counterexamples": []},
        }

    async def decide_concept(
        self, *, game_id: str, decision: str, note: str = "", edited_idea: str = ""
    ) -> dict[str, Any]:
        self.decided.append(
            {"game_id": game_id, "decision": decision, "note": note, "edited_idea": edited_idea}
        )
        return {
            "gameId": game_id,
            "idea": edited_idea or "original idea",
            "status": "pending" if decision == "revise" else "approved",
        }


def _resume(monkeypatch: pytest.MonkeyPatch, decision: dict[str, Any]) -> None:
    """Stand in for LangGraph delivering the human's decision."""

    monkeypatch.setattr(
        "app.graph.nodes.concept_gate.interrupt", lambda _payload: decision
    )


@pytest.mark.asyncio
async def test_propose_commits_the_proposal_before_the_gate_suspends() -> None:
    """``interrupt()`` never returns, so the proposal must be written first.

    If proposing and pausing shared one node, ``concept`` would never reach the
    job row and the API would have nothing to show the user to approve.
    """

    client = _StubStrategic()
    node = build_concept_propose_node(client)

    result = await node({"game_id": "g1", "prompt": "an open world about tides"})

    assert result["concept"]["status"] == "pending"
    assert result["current_stage"] == Stage.CONCEPT_PROPOSE
    assert client.proposed == [{"game_id": "g1", "idea": "an open world about tides"}]


@pytest.mark.asyncio
async def test_approval_records_the_decision_and_sets_the_idea_to_plan_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubStrategic()
    _resume(monkeypatch, {"approved": True, "feedback": "good"})

    result = await build_concept_gate_node(client)({"game_id": "g1", "prompt": "tides"})

    assert result["concept_approved"] is True
    assert result["approved_idea"] == "original idea"
    assert client.decided[0]["decision"] == "approve"


@pytest.mark.asyncio
async def test_edited_idea_is_carried_into_the_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revise round must propose on the user's wording, not the original."""

    client = _StubStrategic()
    _resume(
        monkeypatch,
        {"approved": False, "feedback": "too broad", "edited_idea": "a fishing village sim"},
    )

    gate = await build_concept_gate_node(client)({"game_id": "g1", "prompt": "tides"})

    assert gate["concept_approved"] is False
    assert gate["concept_iteration_count"] == 1
    assert client.decided[0]["decision"] == "revise"
    assert client.decided[0]["edited_idea"] == "a fishing village sim"

    # The next proposal round must query on the edited idea.
    await build_concept_propose_node(client)({"game_id": "g1", "prompt": "tides", **gate})
    assert client.proposed[-1]["idea"] == "a fishing village sim"


@pytest.mark.asyncio
async def test_rejection_without_an_edit_keeps_the_current_idea(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubStrategic()
    _resume(monkeypatch, {"approved": False, "feedback": "not sure yet"})

    result = await build_concept_gate_node(client)({"game_id": "g1", "prompt": "tides"})

    assert result["approved_idea"] == "original idea"
    assert result["concept_iteration_count"] == 1


class TestRouting:
    """The gate must always have an exit — §3.3 applies to this loop too."""

    def test_approved_goes_to_planning(self) -> None:
        route = _build_route_after_concept(3)
        assert route({"concept_approved": True}) == Stage.PLANNING

    def test_rejected_loops_back_to_proposing_not_to_the_gate(self) -> None:
        """Looping to the gate would re-show evidence for the old idea."""

        route = _build_route_after_concept(3)
        assert (
            route({"concept_approved": False, "concept_iteration_count": 1})
            == Stage.CONCEPT_PROPOSE
        )

    def test_exhausted_rounds_escalate_to_a_human(self) -> None:
        route = _build_route_after_concept(3)
        assert (
            route({"concept_approved": False, "concept_iteration_count": 3})
            == Stage.HUMAN_ESCALATION
        )

    def test_approval_wins_even_on_the_last_round(self) -> None:
        route = _build_route_after_concept(3)
        assert (
            route({"concept_approved": True, "concept_iteration_count": 99}) == Stage.PLANNING
        )
