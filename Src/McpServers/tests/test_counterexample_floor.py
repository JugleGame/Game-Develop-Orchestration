"""반례 유사도 하한선 — ``research_repo.py`` (06_3-4군_인수인계.md §2.3).

DB 를 타지 않는다. 여기서 재는 것은 순수한 판정 로직이고, 실제 점수 분포는
``inspect_retrieval_scores.py`` 로 따로 실측했다.

이 검사가 지키는 것은 하나다: **반례가 하나도 없는 것이 정상 결과다.** 반례
질의는 ``kind='GAME' AND type IN ('failure','mixed')`` 하드 필터라 하한선이
없으면 무관한 카드가 항상 올라오고, 기획자는 그걸 이 아이디어의 반례로 읽는다.
빈 채로 "반례 조사 부족"이라고 말하는 쪽이 정직하다.
"""

import pytest

from strategic.research_repo import (
    COUNTEREXAMPLE_MISSING,
    Card,
    ResearchEvidence,
    ResearchRepository,
)


def _card(card_id: str, score: float) -> Card:
    return Card(
        card_id=card_id,
        kind="GAME",
        type="mixed",
        title=f"제목 {card_id}",
        summary="요약",
        tags=[],
        elements=[],
        genres=[],
        confidence="medium",
        updated="2026-07-29",
        score=score,
        matched_by="vector",
    )


# 벡터 검색이었음을 뜻하는 자리표시자. 값 자체는 쓰이지 않는다.
_VECTOR = [0.0] * 4


class TestAboveFloor:
    def test_cards_below_the_floor_are_dropped(self) -> None:
        cards = [_card("GAME-005", 0.49), _card("GAME-022", 0.40)]

        kept = ResearchRepository._above_floor(cards, _VECTOR)

        assert [card.card_id for card in kept] == ["GAME-005"]

    def test_dropping_everything_is_a_valid_outcome(self) -> None:
        """이 저장소의 반례 풀은 10장뿐이라, 대부분의 질의에 진짜 반례가 없다."""

        cards = [_card("GAME-022", 0.40), _card("GAME-004", 0.39)]

        assert ResearchRepository._above_floor(cards, _VECTOR) == []

    def test_the_floor_is_inclusive(self) -> None:
        cards = [_card("GAME-005", 0.45)]

        assert len(ResearchRepository._above_floor(cards, _VECTOR)) == 1

    def test_trigram_results_are_left_alone(self) -> None:
        """트라이그램 점수는 0.03 대라, 같은 하한선을 대면 반례가 전멸한다.

        §2.3 의 실측이 "임베딩 없이는 이 필터를 넣을 재료가 없다"고 결론낸
        지점이다 — 폴백 상태에서 하한선을 적용하면 그 결론을 뒤집게 된다.
        """

        cards = [_card("GAME-022", 0.0281), _card("GAME-004", 0.0300)]

        assert ResearchRepository._above_floor(cards, None) == cards


class TestEvidenceReportsTheGap:
    def test_empty_counterexamples_produce_the_required_phrase(self) -> None:
        """비워두는 것과 "부족하다"고 말하는 것은 다르다 (6_planner.md §3)."""

        evidence = ResearchEvidence(query="아이디어", search_mode="vector+trigram")

        assert evidence.has_counterexample is False
        assert evidence.counterexample_note() == COUNTEREXAMPLE_MISSING
        assert evidence.to_dict()["counterexampleNote"] == COUNTEREXAMPLE_MISSING

    def test_a_surviving_counterexample_clears_the_note(self) -> None:
        evidence = ResearchEvidence(query="아이디어", counterexamples=[_card("GAME-005", 0.49)])

        assert evidence.has_counterexample is True
        assert evidence.counterexample_note() == ""


class TestFloorIsConfigurable:
    def test_the_env_var_is_read_at_import_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """카드가 늘면 값을 다시 재야 한다. 코드를 고치지 않고 실험할 수 있어야
        그 재측정이 실제로 일어난다."""

        import importlib

        monkeypatch.setenv("RESEARCH_COUNTEREXAMPLE_MIN_SCORE", "0.9")
        module = importlib.reload(importlib.import_module("strategic.research_repo"))
        try:
            assert module.COUNTEREXAMPLE_MIN_SCORE == pytest.approx(0.9)
        finally:
            monkeypatch.delenv("RESEARCH_COUNTEREXAMPLE_MIN_SCORE")
            importlib.reload(module)
