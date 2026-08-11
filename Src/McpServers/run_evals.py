"""품질 회귀 감지 — 프롬프트를 바꿨을 때 나빠졌는지 재는 자.

``verify_contract.py`` 가 "계약을 지키나"를 본다면, 이쪽은 "결과가 쓸 만한가"를
본다. 두 질문은 다르고, 지금까지 이 저장소에는 앞쪽만 있었다.

사용법::

    python run_evals.py                       # 돌릴 수 있는 스위트를 전부
    python run_evals.py codegen               # 하나만
    python run_evals.py --min-pass-rate 0.9   # 게이트 기준을 올린다

**외부 자원이 없으면 건너뛴다** (종료 코드 0). codegen 은 ``ANTHROPIC_API_KEY``
가, retrieval 은 ``RESEARCH_DSN`` 이 필요한데, 없는 것을 실패로 치면 CI 가
"품질 회귀"와 "키 미설정"을 구분하지 못한다. 건너뛴 사실은 출력에 남는다.

수치가 낮아 게이트에 걸리면 종료 코드 1 — 그때가 진짜 회귀다.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from common import env  # noqa: E402,F401 — .env 를 os.getenv 보다 먼저 적용한다
from common.console import use_utf8_output  # noqa: E402

use_utf8_output()


from evals import (  # noqa: E402
    CaseResult,
    Summary,
    load_cases,
    score_codegen_case,
    score_qa_case,
    score_retrieval_case,
    summarize,
)

EVAL_DIR = Path(__file__).resolve().parent / "evals"

OK = "  OK  "
BAD = " FAIL "
SKIP = " SKIP "

DEFAULT_MIN_PASS_RATE = 0.8

# 카드 DB 가 안 뜬 것을 몇 분씩 기다릴 이유가 없다. 못 붙으면 건너뛴다.
_DB_CONNECT_TIMEOUT_SECONDS = 10.0


class SuiteUnavailable(RuntimeError):
    """자원이 없거나 닿지 않아 스위트를 돌릴 수 없다 — 회귀가 아니다.

    이 구분이 이 파일의 핵심이다. 키가 없거나 DB 가 안 뜬 것을 실패로 처리하면
    CI 가 빨개지고, 그러면 진짜 품질 회귀가 났을 때 아무도 그 빨간불을 믿지
    않는다.
    """


# ---------------------------------------------------------------------------
# codegen
# ---------------------------------------------------------------------------
async def run_codegen() -> Summary:
    """생성기를 케이스마다 한 번씩 태우고 정적으로 채점한다."""

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SuiteUnavailable("ANTHROPIC_API_KEY 가 없습니다")

    from unity.codegen import CodeGenerationError, ScriptGenerator

    generator = ScriptGenerator()
    cases = load_cases(EVAL_DIR / "codegen.jsonl")
    results: list[CaseResult] = []

    for case in cases:
        try:
            script = await generator.generate(case["id"], case["spec"])
            result = score_codegen_case(case, script.contents)
        except CodeGenerationError as exc:
            result = CaseResult(case_id=case["id"], failures=(f"생성 실패: {exc}",))
        results.append(result)
        _print_case(result)

    return summarize(results)


# ---------------------------------------------------------------------------
# retrieval
# ---------------------------------------------------------------------------
async def run_retrieval() -> Summary:
    """카드 DB 를 실제로 조회해 근거가 잡히는지 본다."""

    dsn = os.getenv("RESEARCH_DSN") or os.getenv("NEON_DSN")
    if not dsn:
        raise SuiteUnavailable("RESEARCH_DSN 이 없습니다")

    import asyncpg

    from strategic.neon_http import connect_pool
    from strategic.research_repo import ResearchRepository, SentenceTransformerEmbedder

    embedder = SentenceTransformerEmbedder() if SentenceTransformerEmbedder.available() else None
    if embedder is None:
        # 트라이그램 폴백은 한국어에서 의미 검색이 아니라 글자 모양 비교다.
        # 그 상태의 점수를 벡터 검색의 점수와 같은 표에 올리면 회귀를 오독한다.
        print("  NOTE  sentence-transformers 없음 — 트라이그램 폴백 점수입니다")

    try:
        # 서버와 같은 경로로 붙는다. 사내망에서 5432 가 막히면 여기서 Neon
        # HTTP(443) 로 넘어간다 — 그 환경에서 raw asyncpg 를 쓰면 "품질을 못
        # 쟀다"가 아니라 "품질을 안 쟀다"가 매번 초록불로 지나간다.
        pool = await connect_pool(dsn, max_size=2, tcp_timeout=_DB_CONNECT_TIMEOUT_SECONDS)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        # DSN 은 있는데 닿지 않는다 (DB 미기동, 네트워크, 자격증명). 이건 검색
        # 품질에 대한 정보가 전혀 아니므로 회귀로 세면 안 된다.
        raise SuiteUnavailable(f"카드 DB 에 연결할 수 없습니다: {type(exc).__name__}") from exc

    try:
        # HTTP 폴백은 연결이 아니라 첫 요청에서 실패한다 — 여기서 한 번 찔러
        # 보지 않으면 그 실패가 케이스 루프 한복판에서 트레이스백으로 터진다.
        await pool.fetchval("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — 무엇이 됐든 "닿지 않는다"이다
        await pool.close()
        raise SuiteUnavailable(f"카드 DB 에 닿지 않습니다: {type(exc).__name__}") from exc

    try:
        repo = ResearchRepository(pool, embedder)
        cases = load_cases(EVAL_DIR / "retrieval.jsonl")
        results: list[CaseResult] = []

        for case in cases:
            evidence = await repo.gather_evidence(case["query"])
            # 아키텍처 카드는 지지 근거와 몫이 나뉘어 있지만(research_repo.py)
            # 케이스가 기대하는 것은 "이 질의에 이 카드가 잡히나" 하나다. 여기서
            # 합치지 않으면 ARCH 를 기대하는 케이스는 검색과 무관하게 늘 떨어진다.
            retrieved = [card.card_id for card in evidence.supporting + evidence.architecture]
            counter = [card.card_id for card in evidence.counterexamples]
            result = score_retrieval_case(case, retrieved, counter)
            results.append(result)
            _print_case(result)

        return summarize(results)
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# qa
# ---------------------------------------------------------------------------
async def run_qa() -> Summary:
    """QA 판정기를 케이스마다 한 번씩 태우고 방향이 맞는지 본다.

    LLM 을 쓰는 서버는 셋인데(strategic / unity / qa) 회귀를 재는 자는 둘뿐이었다.
    QA 는 파이프라인의 마지막 관문이라, 프롬프트가 무뎌지면 **깨진 게임이 PASS 로
    배포되고** 계약 검사는 그걸 못 본다 — PASS 도 FAIL 도 스키마상 적법하다.
    """

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SuiteUnavailable("ANTHROPIC_API_KEY 가 없습니다")

    from common.errors import ToolError
    from qa.judge import run_functional_verification

    cases = load_cases(EVAL_DIR / "qa.jsonl")
    results: list[CaseResult] = []

    for case in cases:
        try:
            verdict, _usage = await run_functional_verification(
                case["game_design"], case["build"], case["qa_policy"]
            )
            result = score_qa_case(case, verdict)
        except ToolError as exc:
            # 판정 자체가 실패한 것은 품질 결과다 — 스키마를 벗어난 응답이나
            # 타임아웃은 이 스위트가 잡아야 할 회귀에 포함된다.
            result = CaseResult(case_id=case["id"], failures=(f"판정 실패: {exc}",))
        results.append(result)
        _print_case(result)

    return summarize(results)


# ---------------------------------------------------------------------------
def _print_case(result: CaseResult) -> None:
    if result.ok:
        print(f"  {OK} {result.case_id}")
        return
    print(f"  {BAD} {result.case_id}")
    for failure in result.failures:
        print(f"         ↳ {failure}")


def _print_summary(name: str, summary: Summary, threshold: float) -> bool:
    metrics = "  ".join(f"{key}={value:.2f}" for key, value in sorted(summary.metrics.items()))
    verdict = "OK" if summary.pass_rate >= threshold else "REGRESSION"
    print(
        f"\n[{name}] {summary.passed}/{summary.total} 통과 "
        f"(pass_rate={summary.pass_rate:.2f}, 기준 {threshold:.2f}) {metrics}  → {verdict}"
    )
    return summary.pass_rate >= threshold


SUITES = {"codegen": run_codegen, "retrieval": run_retrieval, "qa": run_qa}


async def main(selected: list[str], threshold: float) -> int:
    targets = selected or list(SUITES)
    unknown = [name for name in targets if name not in SUITES]
    if unknown:
        print(f"알 수 없는 스위트: {unknown}. 가능한 값: {list(SUITES)}")
        return 2

    ran = 0
    regressions: list[str] = []
    for name in targets:
        print(f"\n[{name}]")
        try:
            summary = await SUITES[name]()
        except SuiteUnavailable as exc:
            print(f"  {SKIP} {exc}")
            continue
        ran += 1
        if not _print_summary(name, summary, threshold):
            regressions.append(name)

    print("\n" + "=" * 60)
    if ran == 0:
        print("⚠️  돌릴 수 있는 스위트가 없었습니다 (자원 미설정). 회귀 판정 아님.")
        return 0
    if regressions:
        print(f"❌ 품질 회귀 {len(regressions)}건: {regressions}")
        return 1
    print(f"✅ {ran}개 스위트가 기준({threshold:.2f})을 통과")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suites", nargs="*", help=f"돌릴 스위트: {list(SUITES)}")
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=DEFAULT_MIN_PASS_RATE,
        help=(
            "이 통과율 미만이면 종료 코드 1. 모델 출력은 확률적이라 1.0 을 "
            f"기준으로 두면 무작위로 빨개진다 (기본 {DEFAULT_MIN_PASS_RATE})."
        ),
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.suites, args.min_pass_rate)))
