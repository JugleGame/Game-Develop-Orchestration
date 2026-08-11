"""평가(eval) 채점 — 프롬프트를 바꿨을 때 좋아졌는지 나빠졌는지를 재는 자.

지금 이 저장소의 테스트는 전부 "형태가 맞나 / 비용이 맞나"를 본다. 그건
필요하지만 **"결과가 나아졌나"는 하나도 재지 않는다.** 그 자가 없으면 프롬프트
수정은 개선이 아니라 도박이 된다 — 바꾸고 나서 좋아졌다고 말할 근거가 없다.

채점 로직을 러너에서 떼어 둔 이유는 하나다: **채점 자체가 외부 자원 없이
검증 가능해야** 한다. LLM 키도 DB 도 없는 CI 에서 이 모듈의 테스트는 돌고,
그래야 "eval 이 통과했다"는 말을 믿을 수 있다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "CaseResult",
    "Summary",
    "load_cases",
    "score_codegen_case",
    "score_qa_case",
    "score_retrieval_case",
    "summarize",
]


@dataclass(frozen=True)
class CaseResult:
    """한 케이스의 판정. ``failures`` 가 비면 통과."""

    case_id: str
    failures: tuple[str, ...] = ()
    # 케이스별 수치 (recall 등). 요약에서 평균을 낸다.
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class Summary:
    total: int = 0
    passed: int = 0
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def load_cases(path: Path) -> list[dict[str, Any]]:
    """JSONL 을 읽는다. 빈 줄과 ``#`` 주석 줄은 건너뛴다.

    한 줄이 깨졌으면 그 줄 번호와 함께 실패한다 — 조용히 건너뛰면 케이스가
    사라진 줄 모른 채 "전부 통과"를 보게 된다.
    """

    cases: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            decoded = json.loads(stripped)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{number} JSON 파싱 실패: {exc}") from exc
        if not isinstance(decoded, dict) or "id" not in decoded:
            raise ValueError(f"{path.name}:{number} 케이스에 'id' 가 없습니다")
        cases.append(decoded)
    return cases


# ---------------------------------------------------------------------------
# codegen — 생성된 C# 을 정적으로 채점한다
# ---------------------------------------------------------------------------
def score_codegen_case(case: dict[str, Any], source: str) -> CaseResult:
    """생성 결과를 케이스의 기대와 대조한다.

    실행하지 않고 재는 것만 본다. Unity 없이 CI 에서 돌아야 하고, 여기서 잡고
    싶은 것(3D 개념 사용, 네임스페이스 위반, 구문 파손)은 전부 정적으로 보인다.
    """

    from unity import csharp_check

    failures: list[str] = []
    metrics: dict[str, float] = {}

    gate = csharp_check.check_structure(source)
    metrics["syntax_ok"] = 1.0 if gate.ok else 0.0
    if case.get("must_compile", True) and not gate.ok:
        failures.append(f"문법: {gate.summary()}")

    namespace = case.get("namespace")
    if namespace and namespace not in source:
        failures.append(f"네임스페이스 '{namespace}' 가 없습니다")

    missing = [token for token in case.get("must_contain", []) if token not in source]
    if missing:
        failures.append(f"필수 토큰 누락: {missing}")

    banned = [token for token in case.get("must_not_contain", []) if token in source]
    if banned:
        failures.append(f"금지 토큰 발견: {banned}")

    expected_tokens = len(case.get("must_contain", []))
    metrics["token_coverage"] = (
        (expected_tokens - len(missing)) / expected_tokens if expected_tokens else 1.0
    )

    failures.extend(_score_structure(case, source, metrics))
    return CaseResult(case_id=case["id"], failures=tuple(failures), metrics=metrics)


def _score_structure(case: dict[str, Any], source: str, metrics: dict[str, float]) -> list[str]:
    """구조 채점 — 토큰이 아니라 **선언된 타입**을 본다.

    ``must_contain`` 은 단순 부분 문자열이라 주석이나 문자열 안에 있어도 통과한다.
    "클래스 이름이 문서 번호에서 왔다" 같은 결함은 그 방식으로는 잡히지 않는다
    (`Doc/설계/06_코드생성_아키텍처_진단_260730.md` §2.1 — eval 이 이 결함을
    놓친 이유가 정확히 이것이다).

    판정은 ``project_layout`` 의 것을 그대로 쓴다. 같은 규칙의 두 번째 구현을
    두면 eval 과 레이아웃 검사기가 서로 다른 답을 내게 된다.
    """

    from project_layout import DOCUMENT_NAME_PATTERN, looks_like_runtime_bootstrap, parse_types

    failures: list[str] = []
    declared = parse_types(source)
    names = [item.name for item in declared]
    metrics["declared_types"] = float(len(declared))

    # 기본값 True — 이 규칙은 케이스가 명시적으로 끄지 않는 한 항상 적용된다.
    if case.get("forbid_document_names", True):
        numbered = [name for name in names if DOCUMENT_NAME_PATTERN.search(name)]
        if numbered:
            failures.append(f"문서 번호에서 온 타입 이름: {numbered}")

    missing_types = [name for name in case.get("must_declare", []) if name not in names]
    if missing_types:
        failures.append(f"선언되지 않은 타입: {missing_types} (선언된 것: {names})")

    minimum = case.get("min_types")
    if minimum is not None and len(declared) < minimum:
        failures.append(f"타입 {minimum}개 이상으로 나뉘어야 하는데 {len(declared)}개다: {names}")

    if case.get("forbid_runtime_bootstrap", False) and looks_like_runtime_bootstrap(source):
        failures.append(
            "런타임에 GameObject 를 만들어 AddComponent 로 붙이고 있다 "
            "— 프리팹·씬 구성으로 옮겨야 한다"
        )

    return failures


# ---------------------------------------------------------------------------
# retrieval — 카드 검색이 근거를 실제로 찾아오는지 채점한다
# ---------------------------------------------------------------------------
def score_retrieval_case(
    case: dict[str, Any], retrieved_ids: list[str], counterexample_ids: list[str]
) -> CaseResult:
    """Recall@k 와 반례 확보 여부를 잰다.

    반례를 따로 보는 이유: 기획 규칙(``6_planner.md`` §3)이 반례를 **최소 1장**
    요구하고, 못 찾으면 비워두는 대신 "반례 조사 부족"이라고 밝히도록 정해 두었기
    때문이다. 아무 반례나 채워 넣는 것과 정직하게 비운 것은 다르게 채점해야 한다.
    """

    failures: list[str] = []
    metrics: dict[str, float] = {}

    expected = case.get("must_retrieve", [])
    found = [card_id for card_id in expected if card_id in retrieved_ids]
    metrics["recall"] = len(found) / len(expected) if expected else 1.0

    absent = [card_id for card_id in expected if card_id not in retrieved_ids]
    if absent:
        failures.append(f"검색되지 않은 근거 카드: {absent}")

    if case.get("must_have_counterexample"):
        metrics["counterexample"] = 1.0 if counterexample_ids else 0.0
        if not counterexample_ids:
            failures.append("반례가 한 장도 없습니다")

    missing_counter = [
        card_id
        for card_id in case.get("counterexample_must_include", [])
        if card_id not in counterexample_ids
    ]
    if missing_counter:
        failures.append(f"올라와야 할 반례가 없습니다: {missing_counter}")

    # 반례는 "찾았나"만큼 "아무거나 채우지 않았나"가 중요하다. 반례 질의는
    # 실패/혼재 카드에 대한 하드 필터라, 유사도 하한선이 없으면 풀이 작을 때
    # 무관한 카드가 항상 올라온다 — 기획자는 그걸 이 아이디어의 반례로 읽는다.
    # 여기 적히는 카드는 그 질의에 대해 **사람이 무관하다고 판정한** 것이다.
    noise = [
        card_id
        for card_id in case.get("counterexample_must_not_include", [])
        if card_id in counterexample_ids
    ]
    if noise:
        failures.append(f"무관한 카드가 반례로 올라왔습니다: {noise}")
    if case.get("counterexample_must_not_include"):
        metrics["counterexample_precision"] = 0.0 if noise else 1.0

    return CaseResult(case_id=case["id"], failures=tuple(failures), metrics=metrics)


# ---------------------------------------------------------------------------
# qa — 판정이 맞는 방향으로 나오는지 채점한다
# ---------------------------------------------------------------------------
def score_qa_case(case: dict[str, Any], verdict: dict[str, Any]) -> CaseResult:
    """``run_functional_verification`` 의 판정을 케이스의 기대와 대조한다.

    **여기서 방향을 두 갈래로 나눠 센다.** 놓친 실패(FAIL 이어야 하는데 PASS)와
    헛경보(PASS 여야 하는데 FAIL)는 값이 다르다 — 앞쪽은 깨진 게임이 배포되고,
    뒤쪽은 멀쩡한 게임이 재생성 루프를 돌다 사람에게 넘어간다. 통과율 하나로
    합치면 프롬프트를 "무조건 FAIL" 쪽으로 밀어도 수치가 좋아 보인다.
    """

    failures: list[str] = []
    metrics: dict[str, float] = {}

    expected = case["expect_result"]
    actual = verdict.get("result")
    correct = actual == expected
    metrics["verdict_correct"] = 1.0 if correct else 0.0
    if expected == "FAIL":
        metrics["caught_failure"] = 1.0 if correct else 0.0
    else:
        metrics["no_false_alarm"] = 1.0 if correct else 0.0
    if not correct:
        failures.append(f"판정이 {expected} 이어야 하는데 {actual!r} 입니다")

    if expected != "FAIL":
        return CaseResult(case_id=case["id"], failures=tuple(failures), metrics=metrics)

    # FAIL 은 보고서까지 있어야 쓸모가 있다. ErrorCorrection 이 그걸 받아 다음
    # 재생성을 만들기 때문에, 내용 없는 FAIL 은 루프만 한 바퀴 태운다.
    report = verdict.get("errorReport")
    if not isinstance(report, dict):
        failures.append("FAIL 인데 errorReport 가 없습니다")
        return CaseResult(case_id=case["id"], failures=tuple(failures), metrics=metrics)

    expected_type = case.get("expect_error_type")
    if expected_type:
        matched = report.get("error_type") == expected_type
        metrics["error_type_correct"] = 1.0 if matched else 0.0
        if not matched:
            failures.append(
                f"error_type 이 {expected_type!r} 이어야 하는데 {report.get('error_type')!r} 입니다"
            )

    if not (report.get("message") or "").strip():
        failures.append("errorReport.message 가 비어 있습니다")

    return CaseResult(case_id=case["id"], failures=tuple(failures), metrics=metrics)


def summarize(results: list[CaseResult]) -> Summary:
    """통과율과 지표 평균. 지표는 **그 지표를 보고한 케이스에 대해서만** 평균낸다."""

    summary = Summary(total=len(results), passed=sum(1 for r in results if r.ok))

    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for result in results:
        for name, value in result.metrics.items():
            sums[name] = sums.get(name, 0.0) + value
            counts[name] = counts.get(name, 0) + 1
    summary.metrics = {name: sums[name] / counts[name] for name in sums}
    return summary
