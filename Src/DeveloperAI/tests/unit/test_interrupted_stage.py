"""The runner must name the gate it actually paused at.

There are two interrupting nodes now. The runner used to hardcode
``ApprovalGate`` for any interrupt, which — once ConceptGate existed — would
tell the UI that a concept review is a design approval and show the wrong
screen with the wrong payload. The stage is read from the interrupt value
instead, and these tests pin the shapes LangGraph delivers it in.
"""

from types import SimpleNamespace

from app.graph.state import Stage
from app.services.workflow_runner import _interrupted_stage


def _interrupt(value):
    """LangGraph wraps the node's payload in an object exposing ``.value``."""

    return SimpleNamespace(value=value)


def test_reads_the_stage_from_a_single_interrupt() -> None:
    payload = (_interrupt({"stage": Stage.CONCEPT_GATE, "concept": {}}),)
    assert _interrupted_stage(payload) == Stage.CONCEPT_GATE


def test_reads_the_approval_gate_stage() -> None:
    payload = (_interrupt({"stage": Stage.APPROVAL_GATE, "game_design": {}}),)
    assert _interrupted_stage(payload) == Stage.APPROVAL_GATE


def test_accepts_a_bare_value_not_wrapped_in_a_sequence() -> None:
    assert _interrupted_stage(_interrupt({"stage": Stage.CONCEPT_GATE})) == Stage.CONCEPT_GATE


def test_accepts_a_plain_dict_without_the_wrapper() -> None:
    assert _interrupted_stage([{"stage": Stage.CONCEPT_GATE}]) == Stage.CONCEPT_GATE


def test_unrecognised_shape_falls_back_to_the_gate_that_predates_this() -> None:
    """Degrade to the old behaviour rather than persist an empty stage."""

    assert _interrupted_stage(()) == Stage.APPROVAL_GATE
    assert _interrupted_stage([_interrupt("just a string")]) == Stage.APPROVAL_GATE
    assert _interrupted_stage([_interrupt({"no_stage_key": 1})]) == Stage.APPROVAL_GATE


def test_first_entry_with_a_stage_wins() -> None:
    payload = (_interrupt({"noise": True}), _interrupt({"stage": Stage.CONCEPT_GATE}))
    assert _interrupted_stage(payload) == Stage.CONCEPT_GATE
