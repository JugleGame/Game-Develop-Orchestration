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
