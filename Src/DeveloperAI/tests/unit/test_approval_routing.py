"""Unit tests for the bounded ApprovalGate rejection loop (§3.3 spirit).

Approval routes to CodeGen; rejection re-plans until the cap, after which the
job escalates to a human instead of burning LLM budget forever.
"""

from app.graph.graph import _build_route_after_approval
from app.graph.state import Stage


def test_approved_routes_to_codegen():
    route = _build_route_after_approval(5)

    assert route({"approved": True, "replan_count": 0}) == Stage.CODE_GEN


def test_rejection_under_cap_routes_back_to_planning():
    route = _build_route_after_approval(5)

    assert route({"approved": False, "replan_count": 4}) == Stage.PLANNING


def test_rejection_at_cap_escalates_to_human():
    route = _build_route_after_approval(5)

    assert route({"approved": False, "replan_count": 5}) == Stage.HUMAN_ESCALATION


def test_approval_wins_even_at_cap():
    # A user who finally approves on the last round must not be escalated.
    route = _build_route_after_approval(5)

    assert route({"approved": True, "replan_count": 5}) == Stage.CODE_GEN
