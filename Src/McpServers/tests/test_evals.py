"""Research 검색 회귀 평가의 결정론적 채점 규칙."""

from pathlib import Path

import pytest

from evals import CaseResult, load_cases, score_retrieval_case, summarize


def test_retrieval_cases_parse() -> None:
    path = Path(__file__).resolve().parents[1] / "evals" / "retrieval.jsonl"
    cases = load_cases(path)
    assert cases
    assert all(case["id"] and case["query"] for case in cases)


def test_broken_jsonl_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text('{"id":"ok"}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="broken.jsonl:2"):
        load_cases(path)


def test_retrieval_requires_expected_evidence_and_counterexample() -> None:
    case = {
        "id": "r1",
        "must_retrieve": ["ELEM-001", "GAME-001"],
        "must_have_counterexample": True,
        "counterexample_must_include": ["GAME-099"],
    }
    result = score_retrieval_case(case, ["ELEM-001"], [])
    assert not result.ok
    assert result.metrics["recall"] == 0.5
    assert result.metrics["counterexample"] == 0.0


def test_irrelevant_counterexample_is_rejected() -> None:
    case = {
        "id": "r2",
        "counterexample_must_not_include": ["GAME-404"],
    }
    result = score_retrieval_case(case, [], ["GAME-404"])
    assert not result.ok
    assert result.metrics["counterexample_precision"] == 0.0


def test_summary_averages_only_reported_metrics() -> None:
    summary = summarize(
        [
            CaseResult("a", metrics={"recall": 1.0}),
            CaseResult("b", failures=("miss",), metrics={"recall": 0.0, "precision": 1.0}),
        ]
    )
    assert summary.total == 2
    assert summary.passed == 1
    assert summary.pass_rate == 0.5
    assert summary.metrics == {"recall": 0.5, "precision": 1.0}
