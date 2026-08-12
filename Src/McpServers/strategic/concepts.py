"""아이디어 제안(concept proposal) — 청사진을 쓰기 전에 근거만 먼저 사람이 검토한다.

청사진을 저장하기 전에 사람이 아이디어와 근거를 검토할 수 있게 한다.

    research_idea 로 DB 근거를 모은다
        → propose_concept 으로 그 근거 + 아이디어를 저장하고 사람 앞에 세운다
        → decide_concept 으로 사람이 승인(approve) / 수정(revise) / 거부(reject) 한다
        → 승인된 idea 텍스트를 호스트가 청사진 작성에 사용한다

모델을 호출하지 않는다.

``strategic_concepts`` 는 ``game_id`` 를 PK 로 하는 단일 행 테이블이다. 청사진
(``strategic_blueprints``)과 같은 버저닝 관례(재저장 시 version + 1)를 따른다:
한 게임에 대한 제안은 항상 최신 한 장만 살아있고, 그 이전 결정들은 ``decisions``
배열에 감사 기록으로 남는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg

STATUSES = ("pending", "approved", "rejected")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConceptProposal:
    """한 게임에 대한 최신 아이디어 제안. ``game_id`` 당 한 장만 존재한다."""

    game_id: str
    version: int
    idea: str
    evidence: dict[str, Any]
    status: str = "pending"
    decisions: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "gameId": self.game_id,
            "version": self.version,
            "idea": self.idea,
            "evidence": self.evidence,
            "status": self.status,
            "decisions": self.decisions,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def adjust_evidence(
    evidence: dict[str, Any],
    include: list[dict[str, Any]],
    exclude: set[str],
) -> dict[str, Any]:
    """사람이 지정한 카드 포함/제외를 근거 목록에 반영한다.

    ``include`` 는 ``ResearchRepository.get_cards()`` 결과를 ``Card.to_dict()``
    로 바꾼 것들이다 — trigram 검색이 놓친 카드를 사람이 직접 강제로 끼워 넣을
    때 쓴다. ``exclude`` 는 검색은 됐지만 근거로 삼기엔 부적절하다고 사람이
    판단한 카드 ID 집합이다.
    """

    def _filter(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [c for c in cards if c["cardId"] not in exclude]
        existing_ids = {c["cardId"] for c in kept}
        for card in include:
            # 같은 카드가 include 와 exclude 에 동시에 있으면 exclude 가 이긴다 —
            # 명시적 제외는 사람의 최종 판단이므로 강제 포함보다 우선한다.
            if card["cardId"] in exclude:
                continue
            if card["cardId"] not in existing_ids:
                kept.append(card)
                existing_ids.add(card["cardId"])
        return kept

    adjusted = dict(evidence)
    adjusted["supporting"] = _filter(evidence.get("supporting", []))
    adjusted["counterexamples"] = _filter(evidence.get("counterexamples", []))
    adjusted["citableCardIds"] = sorted(
        {c["cardId"] for c in adjusted["supporting"]}
        | {c["cardId"] for c in adjusted["counterexamples"]}
    )
    return adjusted


_DDL = """
CREATE TABLE IF NOT EXISTS strategic_concepts (
    game_id     TEXT PRIMARY KEY,
    version     INT  NOT NULL DEFAULT 1,
    idea        TEXT NOT NULL,
    evidence    JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    decisions   JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class ConceptStore:
    """아이디어 제안의 영속화. 리서치 카드 거울(``cards``)에는 쓰지 않는다."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def save(self, concept: ConceptProposal) -> None:
        await self._pool.execute(
            """
            INSERT INTO strategic_concepts (game_id, version, idea, evidence, status, decisions)
            VALUES ($1,$2,$3,$4::jsonb,$5,$6::jsonb)
            ON CONFLICT (game_id) DO UPDATE SET
                version    = EXCLUDED.version,
                idea       = EXCLUDED.idea,
                evidence   = EXCLUDED.evidence,
                status     = EXCLUDED.status,
                decisions  = EXCLUDED.decisions,
                updated_at = now()
            """,
            concept.game_id,
            concept.version,
            concept.idea,
            json.dumps(concept.evidence, ensure_ascii=False),
            concept.status,
            json.dumps(concept.decisions, ensure_ascii=False),
        )

    async def get(self, game_id: str) -> ConceptProposal | None:
        row = await self._pool.fetchrow(
            "SELECT game_id, version, idea, evidence, status, decisions,"
            " created_at::text, updated_at::text FROM strategic_concepts WHERE game_id=$1",
            game_id,
        )
        if row is None:
            return None
        return ConceptProposal(
            game_id=row["game_id"],
            version=row["version"],
            idea=row["idea"],
            evidence=json.loads(row["evidence"]),
            status=row["status"],
            decisions=json.loads(row["decisions"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_pending(self) -> list[dict[str, Any]]:
        """검토 대기 중인 제안 전체 — 게임 하나가 아니라 리뷰어의 받은편지함이다."""

        rows = await self._pool.fetch(
            "SELECT game_id, version, idea, updated_at::text FROM strategic_concepts"
            " WHERE status='pending' ORDER BY updated_at DESC"
        )
        return [
            {
                "gameId": row["game_id"],
                "version": row["version"],
                "idea": row["idea"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]
