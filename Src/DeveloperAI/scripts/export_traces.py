"""Read the ``pipeline_traces`` table (Doc/설계/06_3-4군_인수인계.md §4).

Writing was already wired; nothing read the table until this script. It
exposes exactly the two selection patterns ``PipelineTrace``'s docstring
promises — verified (prompt, code) pairs and before/after retry pairs — plus
a volume report, since the handoff doc's own conclusion is that collection
comes before fine-tuning: one game in ``Games/`` is nowhere near the ~200-row
SFT floor (~20-30 games' worth of specs).

Usage::

    python scripts/export_traces.py status
    python scripts/export_traces.py verified --out sft_pairs.jsonl
    python scripts/export_traces.py pairs --out dpo_pairs.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.orm import PipelineTrace  # noqa: E402
from app.repository.database import async_session_factory  # noqa: E402
from app.repository.trace_repository import TraceRepository  # noqa: E402

SFT_MINIMUM_ROWS = 200


def _verified_record(row: PipelineTrace) -> dict[str, Any]:
    return {
        "game_id": row.game_id,
        "feature_id": row.feature_id,
        "prompt": row.prompt,
        "project_context": row.project_context,
        "file": row.file,
        "generated_source": row.generated_source,
    }


def _pair_record(before: PipelineTrace, after: PipelineTrace) -> dict[str, Any]:
    return {
        "game_id": after.game_id,
        "feature_id": after.feature_id,
        "prompt": after.prompt,
        "project_context": after.project_context,
        "rejected": {
            "generated_source": before.generated_source,
            "compile_errors": before.compile_errors,
            "qa_report": before.qa_report,
        },
        "chosen": {"generated_source": after.generated_source},
    }


def _write_jsonl(records: list[dict[str, Any]], out: str | None) -> None:
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    if out:
        Path(out).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        print(f"{len(records)}행을 {out} 에 썼다.")
    else:
        for line in lines:
            print(line)


async def _run_status() -> None:
    async with async_session_factory() as session:
        repo = TraceRepository(session)
        verified = await repo.list_verified_pairs()
        pairs = await repo.list_before_after_pairs()
        recent = await repo.list_recent(limit=1)

    print(f"검증된 (프롬프트, 코드) 쌍: {len(verified)}행")
    print(f"  SFT 최소치({SFT_MINIMUM_ROWS}행) 대비: {len(verified)}/{SFT_MINIMUM_ROWS}")
    print(f"전후(DPO) 쌍: {len(pairs)}개")
    if not recent:
        print("적재된 trace 가 없다.")


async def _run_verified(out: str | None) -> None:
    async with async_session_factory() as session:
        rows = await TraceRepository(session).list_verified_pairs()
    _write_jsonl([_verified_record(row) for row in rows], out)


async def _run_pairs(out: str | None) -> None:
    async with async_session_factory() as session:
        pairs = await TraceRepository(session).list_before_after_pairs()
    _write_jsonl([_pair_record(before, after) for before, after in pairs], out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="현재 적재량과 SFT/DPO 후보 수를 보고한다.")

    verified_parser = subparsers.add_parser("verified", help="검증된 (prompt, code) 쌍을 뽑는다.")
    verified_parser.add_argument("--out", help="JSONL 출력 경로 (생략하면 표준출력).")

    pairs_parser = subparsers.add_parser("pairs", help="attempt=1 실패/attempt=2 성공 쌍을 뽑는다.")
    pairs_parser.add_argument("--out", help="JSONL 출력 경로 (생략하면 표준출력).")

    args = parser.parse_args()

    if args.command == "status":
        asyncio.run(_run_status())
    elif args.command == "verified":
        asyncio.run(_run_verified(args.out))
    elif args.command == "pairs":
        asyncio.run(_run_pairs(args.out))


if __name__ == "__main__":
    main()
