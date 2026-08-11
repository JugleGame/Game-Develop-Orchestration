"""하이브리드 검색 — ``research_repo.py::_search`` 가 어떤 SQL 을 어떤 인자로
보내는지 본다.

DB 를 타지 않는다. 실제 검색 품질은 ``evals/retrieval.jsonl`` 의 ret-009~013 이
재고(고유명사 질의: 벡터 단독이면 5개 전부 떨어진다), 여기서 지키는 것은 그
품질을 만들어 내는 **구조**다.

특히 두 가지는 눈으로 잡히지 않는다.

* 융합은 점수가 아니라 **순위**를 더해야 한다. 코사인(0~1)과 트라이그램
  (여기서는 0.03 대)은 척도가 달라서 점수를 직접 섞으면 코사인이 항상 이긴다.
* 보고하는 ``score`` 는 **코사인이어야** 한다. 반례 하한선이 그 척도 위에서
  실측으로 정해졌기 때문에, 여기에 융합 점수를 흘리면 하한선이 의미를 잃는다.
"""

from typing import Any

import pytest

from strategic import research_repo
from strategic.research_repo import ResearchRepository


class _RecordingPool:
    """``fetch`` 로 들어온 SQL 과 인자를 그대로 적어 둔다."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self.rows


_VECTOR = [0.1, 0.2, 0.3]


def _row(card_id: str, score: float, matched_by: str) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "kind": "GAME",
        "type": "success",
        "title": "제목",
        "summary": "요약",
        "tags": ["a"],
        "elements": [],
        "genres": [],
        "confidence": "high",
        "updated": "2026-07-29",
        "score": score,
        "matched_by": matched_by,
    }


class TestHybridQueryShape:
    async def test_both_signals_are_computed_in_one_query(self) -> None:
        pool = _RecordingPool()
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        await repo._search("질의", _VECTOR, 6, "")

        sql, _args = pool.calls[0]
        assert "embedding <=> $1::vector" in sql
        assert "similarity(" in sql

    async def test_fusion_adds_ranks_not_scores(self) -> None:
        """점수를 직접 더하면 척도가 큰 쪽이 항상 이긴다."""

        pool = _RecordingPool()
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        await repo._search("질의", _VECTOR, 6, "")

        sql, _args = pool.calls[0]
        assert "ROW_NUMBER() OVER" in sql
        assert "1.0 / ($4 + vec_rank)" in sql
        assert "1.0 / ($4 + trg_rank)" in sql

    async def test_reported_score_is_cosine_not_the_fused_value(self) -> None:
        """반례 하한선이 이 값 위에서 정해졌다 — 바뀌면 하한선이 무의미해진다."""

        pool = _RecordingPool()
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        await repo._search("질의", _VECTOR, 6, "")

        sql, _args = pool.calls[0]
        assert "vec_score AS score" in sql

    async def test_the_candidate_window_is_wide_enough_for_the_corpus(self) -> None:
        """창이 좁으면 한쪽에서만 걸린 정답이 표 하나만 받고 밀려난다 — 실측으로
        20 에서 40 으로 넓혔다 (recall@6 15/17 → 17/17)."""

        pool = _RecordingPool()
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        await repo._search("질의", _VECTOR, 6, "")

        _sql, args = pool.calls[0]
        literal, query, window, rrf_k, limit = args
        assert literal == "[0.1,0.2,0.3]"
        assert query == "질의"
        assert window == research_repo._MIN_CANDIDATE_WINDOW == 40
        assert rrf_k == research_repo.RRF_K == 60
        assert limit == 6

    async def test_the_window_grows_with_the_requested_limit(self) -> None:
        pool = _RecordingPool()
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        await repo._search("질의", _VECTOR, 30, "")

        _sql, args = pool.calls[0]
        assert args[2] == 30 * research_repo._CANDIDATE_WINDOW_FACTOR

    async def test_the_extra_filter_is_applied_before_ranking(self) -> None:
        """반례 질의는 실패/혼재 카드 안에서 순위를 매겨야 한다. 순위를 먼저 매기고
        거르면 창 안에 반례가 한 장도 안 남는다."""

        pool = _RecordingPool()
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        await repo._search("질의", _VECTOR, 3, "AND kind = 'GAME'")

        sql, _args = pool.calls[0]
        before_ranked = sql.split("ranked AS")[0]
        assert "AND kind = 'GAME'" in before_ranked


class TestTrigramFallbackIsUntouched:
    async def test_without_a_vector_the_old_single_signal_query_is_used(self) -> None:
        """임베딩이 없으면 섞을 두 번째 신호가 없다 — 하이브리드가 성립하지 않는다."""

        pool = _RecordingPool()
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        await repo._search("질의", None, 6, "")

        sql, args = pool.calls[0]
        assert "ROW_NUMBER() OVER" not in sql
        assert "'trigram' AS matched_by" in sql
        assert args == ("질의", 6)


class TestRowsBecomeCards:
    @pytest.mark.parametrize("matched_by", ["vector", "trigram", "vector+trigram"])
    async def test_the_matching_leg_is_reported_to_the_caller(self, matched_by: str) -> None:
        """어느 신호가 이 카드를 찾았는지는 검색을 의심할 때 가장 먼저 보는 값이다."""

        pool = _RecordingPool([_row("GAME-002", 0.32, matched_by)])
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        cards = await repo._search("질의", _VECTOR, 6, "")

        assert cards[0].card_id == "GAME-002"
        assert cards[0].matched_by == matched_by
        assert cards[0].score == pytest.approx(0.32)

    async def test_a_card_without_an_embedding_scores_zero(self) -> None:
        """트라이그램만으로 걸린 카드는 코사인이 NULL 이다. 0 으로 떨어져야 반례
        하한선이 그것을 걸러낸다 — 의미적 근거 없이 글자만 겹친 실패 사례는
        반례가 아니다."""

        pool = _RecordingPool([_row("GAME-002", None, "trigram")])  # type: ignore[arg-type]
        repo = ResearchRepository(pool)  # type: ignore[arg-type]

        cards = await repo._search("질의", _VECTOR, 6, "")

        assert cards[0].score == 0.0
        assert ResearchRepository._above_floor(cards, _VECTOR) == []
