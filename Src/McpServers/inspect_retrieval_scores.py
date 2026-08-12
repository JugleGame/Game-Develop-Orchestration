"""검색 회귀 케이스마다 반례 유사도 원점수를 출력하는 일회성 진단 도구.

무관한 실패/혼재 카드가 후보 부족만으로 반례 슬롯을 채우지 않는지 확인하고,
``RESEARCH_COUNTEREXAMPLE_MIN_SCORE`` 조정 근거를 수집한다.

Uses ``strategic.neon_http.connect_pool``, which falls back to Neon's
SQL-over-HTTP(443) endpoint when TCP 5432 is blocked (e.g. a corporate
network that only opens 443) — this was hit and fixed while writing this
script.

Usage::

    python inspect_retrieval_scores.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from common import env  # noqa: E402,F401 — import applies .env before os.getenv below
from common.console import use_utf8_output  # noqa: E402

use_utf8_output()

from evals import load_cases  # noqa: E402
from strategic.neon_http import connect_pool  # noqa: E402
from strategic.research_repo import ResearchRepository, SentenceTransformerEmbedder  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent / "evals"


async def main() -> None:
    dsn = os.getenv("RESEARCH_DSN") or os.getenv("NEON_DSN")
    if not dsn:
        print("RESEARCH_DSN 이 없습니다.")
        return

    try:
        pool = await connect_pool(dsn, max_size=2)
    except Exception as exc:  # noqa: BLE001 — 진단 스크립트라 원인 그대로 보여준다
        print(f"DB 연결 실패: {type(exc).__name__}: {exc}")
        return

    try:
        embedder = SentenceTransformerEmbedder() if SentenceTransformerEmbedder.available() else None
        repo = ResearchRepository(pool, embedder)
        print(f"검색 모드: {repo.search_mode}\n")

        cases = load_cases(EVAL_DIR / "retrieval.jsonl")
        for case in cases:
            query = case["query"]
            evidence = await repo.gather_evidence(query, support_k=6, counter_k=5)
            print(f"[{case['id']}] {query}")
            if not evidence.counterexamples:
                print("  반례 없음 (COUNTEREXAMPLE_MISSING)")
            for card in evidence.counterexamples:
                print(f"  score={card.score:.4f}  {card.card_id}  {card.title}")
            print()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
