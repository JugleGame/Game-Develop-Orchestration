"""채점 로직 검증 — 외부 자원 없이 도는 부분.

eval 이 의미를 가지려면 **채점기 자체가 먼저 신뢰돼야** 한다. 채점이 조용히
관대하면 "전부 통과"는 품질이 좋다는 뜻이 아니라 자를 잘못 만들었다는 뜻이다.
그래서 여기서는 케이스 파일이 실제로 유효한지, 그리고 채점기가 나쁜 출력을
정말 떨어뜨리는지 둘 다 고정한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import (
    load_cases,
    score_codegen_case,
    score_qa_case,
    score_retrieval_case,
    summarize,
)

EVAL_DIR = Path(__file__).resolve().parents[1] / "evals"

_GOOD_SOURCE = """
using UnityEngine;

namespace Game.Gameplay
{
    // cg-001
    public sealed class ChunkLoader : MonoBehaviour
    {
        [SerializeField] private int radius = 1;
        private Vector2Int _current;
    }
}
"""


# ---------------------------------------------------------------------------
# 케이스 파일 자체
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["codegen.jsonl", "retrieval.jsonl"])
def test_case_files_parse(name: str) -> None:
    cases = load_cases(EVAL_DIR / name)

    assert cases, f"{name} 에 케이스가 없습니다"
    assert len({case["id"] for case in cases}) == len(cases), "케이스 id 가 중복입니다"


def test_broken_line_fails_loudly(tmp_path: Path) -> None:
    """조용히 건너뛰면 케이스가 사라진 줄 모른 채 '전부 통과'를 보게 된다."""

    path = tmp_path / "broken.jsonl"
    path.write_text('{"id": "a"}\n{not json}\n', encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_cases(path)

    assert "broken.jsonl:2" in str(exc_info.value)


def test_case_without_id_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "noid.jsonl"
    path.write_text('{"spec": "x"}\n', encoding="utf-8")

    with pytest.raises(ValueError):
        load_cases(path)


def test_comments_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text('# note\n\n{"id": "a"}\n', encoding="utf-8")

    assert [case["id"] for case in load_cases(path)] == ["a"]


def test_retrieval_cases_reference_plausible_card_ids() -> None:
    """존재하지 않는 카드를 기대하면 검색이 아니라 케이스가 틀린 것이고, 그
    지표는 회귀 감지에 못 쓴다.

    규칙은 ``strategic.specs`` 에서 가져온다 — 예전에는 여기에 정규식을 한 벌 더
    적어 두었는데, 층 B 카드를 추가할 때 이쪽만 안 고치면 새 카드를 쓴 케이스가
    "형식이 아님"으로 죽는다.
    """

    from strategic.specs import CARD_ID_PATTERN

    for case in load_cases(EVAL_DIR / "retrieval.jsonl"):
        for card_id in case["must_retrieve"]:
            assert CARD_ID_PATTERN.fullmatch(card_id), (
                f"{case['id']}: 카드 ID 형식이 아님 — {card_id}"
            )


# ---------------------------------------------------------------------------
# codegen 채점
# ---------------------------------------------------------------------------
def test_good_source_passes() -> None:
    case = {
        "id": "cg-001",
        "namespace": "Game.Gameplay",
        "must_contain": ["MonoBehaviour", "Vector2Int"],
        "must_not_contain": ["Terrain"],
    }

    result = score_codegen_case(case, _GOOD_SOURCE)

    assert result.ok, result.failures
    assert result.metrics["token_coverage"] == 1.0


def test_missing_required_token_fails() -> None:
    case = {"id": "cg-001", "must_contain": ["Vector2Int", "Rigidbody2D"]}

    result = score_codegen_case(case, _GOOD_SOURCE)

    assert not result.ok
    assert any("Rigidbody2D" in failure for failure in result.failures)
    assert result.metrics["token_coverage"] == 0.5


def test_banned_token_fails() -> None:
    """이 파이프라인은 2D 전용이다 — 3D 개념이 새어 들어오는 것이 잡고 싶은 것."""

    case = {"id": "cg-001", "must_not_contain": ["NavMeshAgent"]}
    source = _GOOD_SOURCE.replace("private Vector2Int _current;", "private NavMeshAgent _agent;")

    result = score_codegen_case(case, source)

    assert not result.ok
    assert any("NavMeshAgent" in failure for failure in result.failures)


def test_wrong_namespace_fails() -> None:
    case = {"id": "cg-001", "namespace": "Game.Systems"}

    result = score_codegen_case(case, _GOOD_SOURCE)

    assert not result.ok


def test_broken_syntax_fails() -> None:
    case = {"id": "cg-001", "must_compile": True}

    result = score_codegen_case(case, "public class A { void B() {")

    assert not result.ok
    assert result.metrics["syntax_ok"] == 0.0


# ---------------------------------------------------------------------------
# retrieval 채점
# ---------------------------------------------------------------------------
def test_recall_is_measured_not_just_pass_fail() -> None:
    case = {"id": "ret-001", "must_retrieve": ["GENRE-006", "ELEM-013"]}

    result = score_retrieval_case(case, ["GENRE-006", "GAME-018"], [])

    assert not result.ok
    assert result.metrics["recall"] == 0.5


def test_full_recall_passes() -> None:
    case = {"id": "ret-001", "must_retrieve": ["GENRE-006"]}

    result = score_retrieval_case(case, ["GENRE-006", "GAME-018"], ["GAME-004"])

    assert result.ok
    assert result.metrics["recall"] == 1.0


def test_missing_counterexample_fails_when_required() -> None:
    """기획 규칙이 반례 최소 1장을 요구한다 — 없으면 근거가 한쪽만 남는다."""

    case = {"id": "ret-001", "must_retrieve": [], "must_have_counterexample": True}

    result = score_retrieval_case(case, ["GENRE-006"], [])

    assert not result.ok
    assert result.metrics["counterexample"] == 0.0


def test_a_named_counterexample_that_never_arrives_fails() -> None:
    """사람이 "이게 이 질의의 반례다"라고 판정한 카드를 놓치면 회귀다."""

    case = {"id": "ret-004", "must_retrieve": [], "counterexample_must_include": ["GAME-005"]}

    result = score_retrieval_case(case, [], ["GAME-022"])

    assert not result.ok
    assert "GAME-005" in result.failures[0]


def test_irrelevant_counterexamples_fail_even_when_something_was_found() -> None:
    """반례는 "찾았나"만큼 "아무거나 채우지 않았나"가 중요하다.

    하한선이 없으면 실패/혼재 카드 풀이 작을 때 무관한 카드가 항상 올라오고,
    반례 개수만 세는 검사는 그 상태를 만점으로 읽는다.
    """

    case = {
        "id": "ret-001",
        "must_retrieve": [],
        "counterexample_must_not_include": ["GAME-022"],
    }

    result = score_retrieval_case(case, [], ["GAME-022"])

    assert not result.ok
    assert result.metrics["counterexample_precision"] == 0.0


def test_an_empty_counterexample_list_is_precise_by_definition() -> None:
    """대부분의 질의에는 진짜 반례가 없다. 그때 비우는 것이 정답이므로 이 케이스는
    통과해야 하고, 그래야 카드가 늘어도 검사가 낡지 않는다."""

    case = {
        "id": "ret-001",
        "must_retrieve": [],
        "counterexample_must_not_include": ["GAME-022"],
    }

    result = score_retrieval_case(case, [], [])

    assert result.ok
    assert result.metrics["counterexample_precision"] == 1.0


def test_retrieval_cases_name_only_real_looking_counterexample_cards() -> None:
    """반례 기대에 적힌 ID 도 카드 형식이어야 한다 — 오타면 검사가 조용히 통과한다."""

    from strategic.specs import CARD_ID_PATTERN

    for case in load_cases(EVAL_DIR / "retrieval.jsonl"):
        for key in ("counterexample_must_include", "counterexample_must_not_include"):
            for card_id in case.get(key, []):
                assert CARD_ID_PATTERN.fullmatch(card_id), f"{case['id']}: {key} — {card_id}"


# ---------------------------------------------------------------------------
# qa 채점
# ---------------------------------------------------------------------------
_REPORT = {
    "error_type": "runtime",
    "message": "NullReferenceException at PlayerJump.Update",
    "file": "Assets/Scripts/PlayerJump.cs",
    "line": 34,
    "suggested_fix": "Rigidbody2D 참조를 Awake 에서 캐시한다",
    "related_feature_id": "f-2",
}


def test_a_correct_pass_is_scored_as_no_false_alarm() -> None:
    case = {"id": "qa-001", "expect_result": "PASS"}

    result = score_qa_case(case, {"result": "PASS"})

    assert result.ok
    assert result.metrics["no_false_alarm"] == 1.0
    # PASS 케이스는 놓친 실패 지표를 내지 않는다 — 냈다면 평균이 왜곡된다.
    assert "caught_failure" not in result.metrics


def test_a_missed_failure_is_counted_separately_from_a_false_alarm() -> None:
    """깨진 게임이 배포되는 것과 멀쩡한 게임이 루프를 도는 것은 값이 다르다.

    통과율 하나로 합치면 "무조건 FAIL" 프롬프트도 수치가 좋아 보인다.
    """

    missed = score_qa_case({"id": "qa-004", "expect_result": "FAIL"}, {"result": "PASS"})
    false_alarm = score_qa_case(
        {"id": "qa-001", "expect_result": "PASS"}, {"result": "FAIL", "errorReport": _REPORT}
    )

    assert missed.metrics["caught_failure"] == 0.0
    assert "no_false_alarm" not in missed.metrics
    assert false_alarm.metrics["no_false_alarm"] == 0.0
    assert "caught_failure" not in false_alarm.metrics


def test_a_fail_without_a_report_fails_the_case() -> None:
    """ErrorCorrection 이 그 보고서로 다음 재생성을 만든다 — 없으면 루프만 돈다."""

    case = {"id": "qa-002", "expect_result": "FAIL"}

    result = score_qa_case(case, {"result": "FAIL"})

    assert not result.ok
    assert "errorReport" in result.failures[0]


def test_the_error_type_is_checked_when_the_case_names_one() -> None:
    case = {"id": "qa-004", "expect_result": "FAIL", "expect_error_type": "compile"}

    result = score_qa_case(case, {"result": "FAIL", "errorReport": _REPORT})

    assert not result.ok
    assert result.metrics["error_type_correct"] == 0.0


def test_a_correct_failure_with_a_matching_report_passes() -> None:
    case = {"id": "qa-004", "expect_result": "FAIL", "expect_error_type": "runtime"}

    result = score_qa_case(case, {"result": "FAIL", "errorReport": _REPORT})

    assert result.ok
    assert result.metrics["caught_failure"] == 1.0
    assert result.metrics["error_type_correct"] == 1.0


def test_an_empty_message_fails_even_with_the_right_error_type() -> None:
    case = {"id": "qa-004", "expect_result": "FAIL", "expect_error_type": "runtime"}

    result = score_qa_case(case, {"result": "FAIL", "errorReport": {**_REPORT, "message": "   "}})

    assert not result.ok


def test_qa_cases_are_shaped_the_way_the_runner_reads_them() -> None:
    """케이스 파일이 깨지면 스위트는 회귀가 아니라 트레이스백으로 죽는다."""

    import json

    cases = load_cases(EVAL_DIR / "qa.jsonl")
    assert cases, "qa.jsonl 이 비어 있습니다"

    for case in cases:
        assert case["expect_result"] in {"PASS", "FAIL"}, case["id"]
        assert case["game_design"]["game_id"], case["id"]
        assert case["qa_policy"]["test_cases"], case["id"]
        # build 는 §03 상 불투명 문자열이지만 실제로는 qa_common 이 만든 JSON 이다.
        json.loads(case["build"])
        if case["expect_result"] == "PASS":
            assert "expect_error_type" not in case, case["id"]


def test_runtime_check_evidence_is_actually_exercised() -> None:
    """§3 배선(판정이 runtimeCheck.errors 를 실패 증거로 읽는다)을 지키는 케이스가
    실제로 존재하는지 본다 — 케이스가 사라지면 그 규칙은 감시받지 않는다."""

    import json

    cases = load_cases(EVAL_DIR / "qa.jsonl")
    with_runtime_errors = [
        case
        for case in cases
        if (json.loads(case["build"]).get("runtimeCheck") or {}).get("errors")
    ]

    assert with_runtime_errors, "runtimeCheck.errors 가 있는 케이스가 하나도 없습니다"
    assert all(case["expect_result"] == "FAIL" for case in with_runtime_errors)


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------
def test_summary_averages_only_reporting_cases() -> None:
    """지표를 안 낸 케이스를 0 으로 세면 평균이 실제보다 낮게 나온다."""

    results = [
        score_retrieval_case({"id": "a", "must_retrieve": ["X"]}, ["X"], []),
        score_retrieval_case(
            {"id": "b", "must_retrieve": ["Y"], "must_have_counterexample": True},
            ["Y"],
            ["Z"],
        ),
    ]

    summary = summarize(results)

    assert summary.total == 2
    assert summary.passed == 2
    assert summary.pass_rate == 1.0
    # counterexample 은 한 케이스만 보고했고, 그 케이스에서 1.0 이다.
    assert summary.metrics["counterexample"] == 1.0
    assert summary.metrics["recall"] == 1.0


def test_summary_of_nothing_is_not_a_division_error() -> None:
    assert summarize([]).pass_rate == 0.0


# ---------------------------------------------------------------------------
# 러너 — "자원 없음"과 "품질 회귀"를 구분하는지
# ---------------------------------------------------------------------------
async def test_codegen_suite_is_skipped_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_evals

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(run_evals.SuiteUnavailable):
        await run_evals.run_codegen()


async def test_retrieval_suite_is_skipped_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_evals

    monkeypatch.delenv("RESEARCH_DSN", raising=False)
    monkeypatch.delenv("NEON_DSN", raising=False)

    with pytest.raises(run_evals.SuiteUnavailable):
        await run_evals.run_retrieval()


async def test_skipped_suites_do_not_fail_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """이게 이 러너의 핵심 성질이다. 키가 없다고 CI 를 빨갛게 만들면, 진짜
    회귀가 났을 때 아무도 그 빨간불을 믿지 않는다."""

    import run_evals

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RESEARCH_DSN", raising=False)
    monkeypatch.delenv("NEON_DSN", raising=False)

    assert await run_evals.main([], run_evals.DEFAULT_MIN_PASS_RATE) == 0


async def test_low_pass_rate_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """반대 방향도 고정한다 — 실제로 나빠졌을 때는 반드시 종료 코드 1."""

    import run_evals
    from evals import CaseResult

    async def _failing_suite():
        return summarize(
            [
                CaseResult(case_id="a"),
                CaseResult(case_id="b", failures=("금지 토큰 발견: ['NavMeshAgent']",)),
            ]
        )

    monkeypatch.setitem(run_evals.SUITES, "codegen", _failing_suite)

    assert await run_evals.main(["codegen"], 0.8) == 1
    # 같은 결과라도 기준이 낮으면 통과한다 — 기준이 판정을 정한다.
    assert await run_evals.main(["codegen"], 0.5) == 0


async def test_unknown_suite_name_is_a_usage_error() -> None:
    import run_evals

    assert await run_evals.main(["nope"], 0.8) == 2
