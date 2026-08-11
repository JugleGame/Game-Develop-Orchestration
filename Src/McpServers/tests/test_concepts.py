"""concepts.py 단위 테스트 — DB 없이 도는 순수 로직만.

DB 연동(``ConceptStore``)은 실제 Neon 을 태우는 확인이라 README 의 수동
절차를 따른다 (``test_strategic_server.py`` 가 ``SpecStore`` 의 DB 메서드를
단위 테스트하지 않는 것과 같은 이유).
"""

from __future__ import annotations

import os

os.environ.setdefault("RESEARCH_DSN", "postgresql://unused/unused")

from strategic.concepts import ConceptProposal, adjust_evidence  # noqa: E402


def _card(card_id: str) -> dict:
    return {"cardId": card_id, "kind": "GAME", "type": "success", "title": card_id}


# ---------------------------------------------------------------------------
# adjust_evidence — 사람이 근거 카드를 손으로 조정하는 로직
# ---------------------------------------------------------------------------
def test_adjust_evidence_excludes_requested_cards():
    evidence = {"supporting": [_card("GAME-001"), _card("GAME-002")], "counterexamples": []}

    adjusted = adjust_evidence(evidence, include=[], exclude={"GAME-001"})

    assert {c["cardId"] for c in adjusted["supporting"]} == {"GAME-002"}


def test_adjust_evidence_includes_forced_cards_without_duplicating():
    evidence = {"supporting": [_card("GAME-001")], "counterexamples": []}

    adjusted = adjust_evidence(
        evidence, include=[_card("GAME-001"), _card("GAME-003")], exclude=set()
    )

    ids = [c["cardId"] for c in adjusted["supporting"]]
    assert ids.count("GAME-001") == 1
    assert "GAME-003" in ids


def test_adjust_evidence_recomputes_citable_ids():
    evidence = {"supporting": [_card("GAME-001")], "counterexamples": [_card("GAME-002")]}

    adjusted = adjust_evidence(evidence, include=[], exclude=set())

    assert adjusted["citableCardIds"] == ["GAME-001", "GAME-002"]


def test_adjust_evidence_exclude_wins_over_include():
    """같은 카드를 포함과 제외에 동시에 넣으면 제외가 이긴다 — 사람의 최종 판단."""

    evidence = {"supporting": [], "counterexamples": []}

    adjusted = adjust_evidence(evidence, include=[_card("GAME-001")], exclude={"GAME-001"})

    assert adjusted["supporting"] == []


# ---------------------------------------------------------------------------
# ConceptProposal — 직렬화 형태
# ---------------------------------------------------------------------------
def test_concept_proposal_to_dict_uses_camel_case():
    concept = ConceptProposal(
        game_id="g1", version=2, idea="포스트 아포칼립스 동물 생존기",
        evidence={"supporting": []}, status="pending",
    )

    payload = concept.to_dict()

    assert payload["gameId"] == "g1"
    assert payload["version"] == 2
    assert payload["idea"] == "포스트 아포칼립스 동물 생존기"
    assert payload["status"] == "pending"
    assert payload["decisions"] == []


def test_concept_proposal_carries_decision_history():
    concept = ConceptProposal(
        game_id="g1", version=2, idea="아이디어", evidence={},
        decisions=[{"version": 1, "decision": "revise", "note": "적 요소 추가", "decidedAt": "t"}],
    )

    assert concept.to_dict()["decisions"][0]["decision"] == "revise"
