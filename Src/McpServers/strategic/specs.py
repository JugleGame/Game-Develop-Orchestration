"""Spec 문서 — 검사 규칙과 영속화.

``prompts/6_planner.md`` §5 가 정한 spec 형식을 그대로 따른다. 검사 규칙은
연구 저장소의 ``scripts/lint_spec.py`` 를 옮긴 것이라 두 곳이 같은 판정을 낸다.

**왜 DB에 저장하는가.** 요구사항이 "진행 상황을 확인할 수 있게 문서화하고
언제든 재발행 가능해야 한다" 이기 때문이다. 개발 AI 가 spec 을 잃어버리거나,
QA 가 지적해 고친 뒤 다시 넘겨야 하거나, 사람이 "지금 어디까지 됐나" 를 물을 때
답이 있어야 한다. 리서치 카드 거울(``cards``)은 md 가 원본이라 건드리지 않고,
``strategic_`` 접두어로 분리한 테이블에만 쓴다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from .arch_cards import ArchGuidance, arch_ids, is_arch_card

# 검사 규칙은 코드가 아니라 데이터다 — 연구 저장소의 ``scripts/lint_spec.py`` 가
# 같은 판정을 내야 하는데 파이썬 임포트로는 저장소 경계를 못 넘기 때문이다.
# 두 사본이 같은지는 ``tests/test_spec_rules_sync.py`` 가 본다.
SPEC_RULES_PATH = Path(__file__).resolve().parent / "spec_rules.json"
SPEC_RULES: dict[str, Any] = json.loads(SPEC_RULES_PATH.read_text(encoding="utf-8"))

REQUIRED_SECTIONS: list[str] = SPEC_RULES["requiredSections"]
BANNED_WORDS: list[str] = SPEC_RULES["bannedWords"]
OBSERVABLE_WORDS: list[str] = SPEC_RULES["observableWords"]
MEASURABLE_GUARDS: list[dict[str, Any]] = SPEC_RULES.get("measurableGuards", [])
CONTAMINATION_GUARDS: list[dict[str, Any]] = SPEC_RULES.get("contaminationGuards", [])

# S8 — 기획이 코드 경계를 침범했는지. 영문 한 덩어리(PlayerController,
# InventoryManager, Rigidbody2D)는 C# 타입명이지 기능 이름이 아니다.
# ``docs/contracts.md``: 기능 경계는 기획, 코드 경계는 호스트의 design_architecture.
_CSHARP_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# 규칙 파일의 패턴은 앵커가 없는 알맹이다. 여기서는 ``\b`` 를 둘러 본문에서
# 카드 ID 를 긁는 용도와 ``fullmatch`` 로 형식을 검사하는 용도에 함께 쓴다.
CARD_ID_PATTERN = re.compile(rf"\b{SPEC_RULES['cardIdPattern']}\b")

SPEC_STATUSES = ("draft", "published", "revising", "implemented", "verified")


_HANGUL = re.compile(r"[가-힣]")


def _word_present(word: str, line: str) -> bool:
    """Korean → substring, English → whole-word. Not the same check for both.

    청사진이 영어로 바뀌면서(12문서 §10-7 이후 결정) ``BANNED_WORDS``/
    ``OBSERVABLE_WORDS``에 영어 항목이 늘었다. 영어는 substring 검사가 사고를
    낸다 — ``"fun"``이 ``"function"``에, ``"cool"``이 ``"cooldown"``에,
    ``"proper"``가 ``"property"``에 그냥 들어 있다.

    한글 어간은 반대로 substring 이 **필요하다.** 이 목록의 한글 항목은 완성된
    단어가 아니라 어간이다(``"자연스러"``는 ``"자연스러운"``/``"자연스럽게"``
    양쪽을 다 잡으려고 일부러 활용어미를 뗀 것) — 어미가 공백 없이 바로 붙으므로
    단어 경계(`\\b`)를 걸면 이 활용형을 전부 놓친다(실측:
    ``test_subjective_acceptance_criteria_are_rejected`` 가 이걸로 깨졌었다).
    그래서 한글이 섞인 항목은 기존 그대로 substring 을 쓰고, 순수 영단어만
    단어 경계를 건다.
    """

    if _HANGUL.search(word) or not re.search(r"\w", word, re.UNICODE):
        return word in line
    return re.search(rf"\b{re.escape(word)}\b", line, re.IGNORECASE) is not None


@dataclass
class SpecDocument:
    """1 spec = 1 메커니즘. '오픈월드 전체' 같은 덩어리는 금지."""

    spec_id: str
    game_id: str
    title: str
    version: int
    blueprint_version: int
    refs: list[str]
    goal: str
    implementation_scope: list[str]
    out_of_scope: list[str]
    acceptance_criteria: list[str]
    unity_hints: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    status: str = "draft"
    change_log: list[dict[str, Any]] = field(default_factory=list)
    # refs 에 인용된 ARCH 카드의 구현 절차·안티패턴·검증 방법. 모델이 쓰지 않고
    # arch_cards.py 가 카드 원문에서 옮긴다 (그 모듈 docstring 참고).
    architecture: list[ArchGuidance] = field(default_factory=list)

    def to_markdown(self) -> str:
        """``+++`` TOML frontmatter + 필수 섹션 형식으로 렌더한다."""

        refs = ", ".join(f'"{ref}"' for ref in self.refs)
        lines = [
            "+++",
            f'spec_id = "{self.spec_id}"',
            f"version = {self.version}",
            f"blueprint_version = {self.blueprint_version}",
            f"refs = [{refs}]",
            "+++",
            "",
            f"# {self.title}",
            "",
            "## Goal",
            self.goal,
            "",
            "## Implementation scope",
        ]
        lines += [f"- {item}" for item in self.implementation_scope]
        lines += ["", "## Out of scope"]
        lines += [f"- {item}" for item in self.out_of_scope]
        lines += ["", "## Acceptance criteria"]
        lines += [f"- {item}" for item in self.acceptance_criteria]

        # 아키텍처 지침은 카드 원문이므로 개발 AI 가 읽을 spec 안에 함께 남는다.
        # 필수 섹션이 아니라 추가 섹션이라 lint_spec 의 S4 와 무관하다.
        if self.architecture:
            lines += ["", "## Architecture guidance"]
            for guidance in self.architecture:
                lines += ["", guidance.to_markdown()]

        if self.change_log:
            lines += ["", "## change_log"]
            lines += [
                f"- v{entry['version']} ({entry.get('source', '?')}): {entry['change']}"
                for entry in self.change_log
            ]
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "specId": self.spec_id,
            "gameId": self.game_id,
            "title": self.title,
            "version": self.version,
            "blueprintVersion": self.blueprint_version,
            "refs": self.refs,
            "goal": self.goal,
            "implementationScope": self.implementation_scope,
            "outOfScope": self.out_of_scope,
            "acceptanceCriteria": self.acceptance_criteria,
            "unityHints": self.unity_hints,
            "dependencies": self.dependencies,
            "status": self.status,
            "changeLog": self.change_log,
            "architecture": [g.to_dict() for g in self.architecture],
            "markdown": self.to_markdown(),
        }


def lint_spec(spec: SpecDocument, known_card_ids: set[str]) -> list[str]:
    """spec 을 발행 전에 검사한다. 빈 리스트면 통과.

    ``scripts/lint_spec.py`` 의 S2~S5 를 구조체 대상으로 옮긴 것. S1(TOML 파싱)
    은 우리가 렌더링하므로 구조적으로 발생할 수 없다.
    """

    errors: list[str] = []

    # S2 — 필수 키
    if not spec.spec_id:
        errors.append("S2: spec_id 누락")
    if spec.version < 1:
        errors.append("S2: version 은 1 이상이어야 함")
    if spec.blueprint_version < 1:
        errors.append("S2: blueprint_version 은 1 이상이어야 함")
    if not spec.refs:
        errors.append("S2: refs 가 비어 있음 — 근거 카드 없이 발행할 수 없음")

    # S3 — 존재하는 카드만 인용
    for ref in spec.refs:
        if not CARD_ID_PATTERN.fullmatch(ref):
            errors.append(f"S3: 카드 ID 형식이 아님 — {ref}")
        elif ref not in known_card_ids:
            errors.append(f"S3: 존재하지 않는 카드 인용 — {ref}")

    # S4 — 필수 섹션 (내용이 비면 섹션이 없는 것과 같다)
    for name, value in (
        ("Goal", spec.goal),
        ("Implementation scope", spec.implementation_scope),
        ("Out of scope", spec.out_of_scope),
        ("Acceptance criteria", spec.acceptance_criteria),
    ):
        if not value:
            errors.append(f"S4: 필수 섹션 비어 있음 — {name}")

    # S5 — 합격 기준은 숫자 또는 관찰 가능한 사실로만
    for line in spec.acceptance_criteria:
        for banned in BANNED_WORDS:
            if _word_present(banned, line):
                errors.append(f'S5: 측정 불가 표현 "{banned}" — "{line}"')
        if not (re.search(r"\d", line) or any(_word_present(w, line) for w in OBSERVABLE_WORDS)):
            errors.append(f'S5: 숫자/관찰 키워드 없음 — "{line}"')

    errors += _lint_measurable(spec)
    errors += _lint_architecture(spec)
    errors += _lint_contamination(spec)
    errors += _lint_role_boundary(spec)
    return errors


def _lint_measurable(spec: SpecDocument) -> list[str]:
    """S5b — 합격 기준이 QA 의 판정 수단으로 환원되는지.

    S5 는 **글자**를 본다(숫자가 있거나 관찰 키워드가 있으면 통과). QA 는
    **수단**을 본다(콘솔·자동테스트·씬검사·로그 4가지로 확인 불가하면 BLOCKED).
    두 문지기의 기준이 달라서, S5 를 통과한 기준이 QA 에서 무더기로 BLOCKED 가
    되는 일이 벌어진다 — 그리고 QA 정책 §6 은 BLOCKED 가 30% 를 넘으면 **스펙
    자체의 결함**으로 반송한다. 즉 개발이 아무리 잘 돼도 왕복 한 번이 통째로
    버려진다.

    실제 사고 (2026-08-02, warrior-loot): 기준 18개 중 8개(44%)가 BLOCKED 였다.
    "공격 입력 후 0.3초 안에 히트박스가 활성화된다" 는 observableWords 에 `초`
    가 있어서 S5 를 그냥 통과했지만, QA 에게는 스톱워치가 없다.

    그래서 이 검사는 "잴 수 없는 기준"이 아니라 **"재는 방법이 적히지 않은
    기준"**을 잡는다. 고치는 법은 기준을 지우는 게 아니라 테스트 이름을 붙이는
    것이다: "…0.3초 안에 활성화되며 PlayMode 테스트 Test_Attack_Hitbox_300ms
    가 통과한다".
    """

    errors: list[str] = []
    for line in spec.acceptance_criteria:
        for guard in MEASURABLE_GUARDS:
            # trigger 는 정규식이다 — 규칙 파일의 _measurableGuardsNote 참고.
            if not any(re.search(pattern, line, re.IGNORECASE) for pattern in guard["trigger"]):
                continue
            if any(_word_present(word, line) for word in guard["require"]):
                continue
            errors.append(f'S5b: 판정 수단 없음 — "{line}"\n     {guard["reason"]}')
    return errors


def _lint_architecture(spec: SpecDocument) -> list[str]:
    """S6 — 아키텍처 카드 인용과 실려 나가는 지침이 어긋나지 않는지.

    이 검사가 없으면 spec 이 ``ARCH-003`` 을 인용하면서 그 카드가 정한 구현
    절차는 한 줄도 싣지 않을 수 있다. 개발 AI 는 spec 하나만 받으므로(§4)
    인용만 있고 내용이 없으면 그 인용은 개발 AI 에게 아무 지시도 하지 않는다.

    이 저장소에서 ARCH 는 **spec 발행 단계에서만** 카드 원문이 붙는 유일한
    종류다. 나머지 카드는 근거일 뿐이라 이 검사 대상이 아니다.
    """

    errors: list[str] = []
    cited = arch_ids(spec.refs)
    carried = {g.card_id: g for g in spec.architecture}

    for card_id in cited:
        if card_id not in carried:
            errors.append(
                f"S6: {card_id} 을 인용했는데 아키텍처 지침이 실리지 않음 "
                "— 카드 원문에서 옮겨야 함"
            )

    for card_id, guidance in carried.items():
        if not is_arch_card(card_id):
            errors.append(f"S6: 아키텍처 지침의 카드 ID 가 ARCH 형식이 아님 — {card_id}")
        elif card_id not in cited:
            errors.append(f"S6: refs 에 없는 카드의 지침이 실려 있음 — {card_id}")
        empty = guidance.empty_sections
        if empty:
            errors.append(f"S6: {card_id} 의 절이 비어 있음 — {', '.join(empty)}")

    return errors


def _lint_contamination(spec: SpecDocument) -> list[str]:
    """S7 — 카드 원문이 이 게임에 없는 시스템을 지시하는지.

    ``arch_cards.py`` 는 카드의 세 절을 **일부러** 원문 그대로 복사한다. 모델이
    다시 쓰면 원문과 어긋나기 때문이고, 그래서 모델에게는 고칠 칸조차 없다.
    옳은 설계지만 대가가 하나 있다 — 카드가 다른 게임을 전제로 쓴 문장까지
    딸려 온다. 기획도 개발도 그걸 걸러낼 권한이 없으므로, 걸러내는 일을 **아무도
    맡지 않은** 상태가 된다. 이 검사가 그 빈자리를 메운다.

    실제 사고 (2026-08-02, warrior-loot): 단일 필드 씬 게임인데 ARCH-004 를
    인용하자 "청크 연동 — ARCH-003 로더가 언로드하기 직전…" 단계가 그대로
    실렸다. 해설자가 없는 게임인데 ARCH-009 의 "commentator.log 에 줄이
    남아야 한다" 검증도 함께 실렸다. 개발 AI 는 없는 시스템을 만들라는 지시를
    받고, QA 는 영원히 통과 못 할 검사를 받는다.

    판정 원리는 단순하다: **지침이 X 를 말하는데 X 를 소유한 카드가 refs 에
    없으면**, 그 문장은 이 spec 의 것이 아니다. 지우지 않고 잡아만 낸다 —
    자동으로 지우면 정당한 상호 참조("ARCH-003 과 함께 쓸 때 주의")까지
    사라져 새 위험이 생긴다. 판단은 사람이 한다.
    """

    errors: list[str] = []
    cited = set(spec.refs)
    for guidance in spec.architecture:
        text = " ".join(guidance.build_steps + guidance.anti_patterns + guidance.verification)
        for guard in CONTAMINATION_GUARDS:
            if not any(word in text for word in guard["keywords"]):
                continue
            if cited & set(guard["requiresRef"]):
                continue
            errors.append(
                f"S7: {guidance.card_id} 의 지침이 "
                f"{'/'.join(guard['keywords'])} 를 지시하는데 "
                f"{'/'.join(guard['requiresRef'])} 이 refs 에 없음 — {guard['reason']}"
            )
    return errors


def _lint_role_boundary(spec: SpecDocument) -> list[str]:
    """S8 — 기획이 개발의 몫(코드 경계)을 미리 정해버렸는지.

    ``docs/contracts.md``가 역할을 나눈다: **기획은 기능 경계**("이 메커니즘은 어떤
    일들을 해야 하는가"), **개발은 코드 경계**("그 일들을 어떤 클래스·파일로
    나누는가"). 기획 프롬프트도 "ChunkLoader, PlayerController 같은 C# 타입명은
    쓰지 않는다"고 명시한다.

    그런데 그 금지를 확인하는 검사가 없었다. 실제 사고 (2026-08-02,
    warrior-loot): unityHints 에 PlayerController / MonsterController /
    InventoryManager / SaveManager / GameProgressManager 가 그대로 들어왔고
    전부 통과했다. 개발 AI 의 design_architecture 는 스스로 나누라고 만든
    단계인데 이미 지어진 이름을 받으면 그냥 따라 쓴다 — 역할 분담이 **조용히**
    무너진다. 조용해서 나쁘다. 아무도 위반한 줄 모른 채 두 번째, 세 번째 기획도
    같은 모양으로 나온다.

    ARCH-008 의 안티패턴이 같은 말을 한다 — "규약을 문서에만 두고 검사하지
    않기: 규약은 지켜지는지 확인할 방법이 있을 때만 유지된다."
    """

    errors: list[str] = []
    for field_name in ("components", "sceneObjects"):
        for item in spec.unity_hints.get(field_name) or []:
            if _CSHARP_TYPE_NAME.fullmatch(str(item).strip()):
                errors.append(
                    f'S8: unityHints.{field_name} 에 C# 타입명 — "{item}". '
                    "코드 경계는 개발 AI 의 몫이다. "
                    '기능을 우리말로 쓸 것 (예: "몬스터 체력 관리").'
                )
    return errors


# ---------------------------------------------------------------------------
# 영속화
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS strategic_blueprints (
    game_id      TEXT PRIMARY KEY,
    version      INT  NOT NULL DEFAULT 1,
    seed_prompt  TEXT NOT NULL,
    title        TEXT NOT NULL,
    genre        TEXT NOT NULL,
    document     JSONB NOT NULL,
    evidence     JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS strategic_specs (
    spec_id      TEXT PRIMARY KEY,
    game_id      TEXT NOT NULL,
    version      INT  NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'draft',
    document     JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_strategic_specs_game ON strategic_specs(game_id);

-- 개발 AI / QA AI 가 보낸 피드백과 그 처리 결과 (6_planner.md §6 Inbox).
CREATE TABLE IF NOT EXISTS strategic_feedback (
    feedback_id  BIGSERIAL PRIMARY KEY,
    spec_id      TEXT NOT NULL,
    source       TEXT NOT NULL,
    feedback     TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    from_version INT  NOT NULL,
    to_version   INT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_strategic_feedback_spec ON strategic_feedback(spec_id);
"""


class SpecStore:
    """기획 산출물 저장소. 리서치 카드 거울에는 쓰지 않는다."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    # -- blueprint ------------------------------------------------------
    async def save_blueprint(
        self,
        game_id: str,
        seed_prompt: str,
        title: str,
        genre: str,
        document: dict[str, Any],
        evidence: dict[str, Any],
    ) -> int:
        """청사진을 저장하고 버전을 돌려준다. 재실행하면 버전이 오른다."""

        import json

        row = await self._pool.fetchrow(
            """
            INSERT INTO strategic_blueprints (game_id, seed_prompt, title, genre, document, evidence)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb)
            ON CONFLICT (game_id) DO UPDATE SET
                version    = strategic_blueprints.version + 1,
                seed_prompt= EXCLUDED.seed_prompt,
                title      = EXCLUDED.title,
                genre      = EXCLUDED.genre,
                document   = EXCLUDED.document,
                evidence   = EXCLUDED.evidence,
                updated_at = now()
            RETURNING version
            """,
            game_id, seed_prompt, title, genre, json.dumps(document, ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False),
        )
        return int(row["version"])

    async def get_blueprint(self, game_id: str) -> dict[str, Any] | None:
        import json

        row = await self._pool.fetchrow(
            "SELECT game_id, version, seed_prompt, title, genre, document, evidence,"
            " created_at::text, updated_at::text FROM strategic_blueprints WHERE game_id=$1",
            game_id,
        )
        if row is None:
            return None
        return {
            "gameId": row["game_id"],
            "version": row["version"],
            "seedPrompt": row["seed_prompt"],
            "title": row["title"],
            "genre": row["genre"],
            "document": json.loads(row["document"]),
            "evidence": json.loads(row["evidence"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    # -- specs ----------------------------------------------------------
    async def save_spec(self, spec: SpecDocument) -> None:
        import json

        await self._pool.execute(
            """
            INSERT INTO strategic_specs (spec_id, game_id, version, status, document)
            VALUES ($1,$2,$3,$4,$5::jsonb)
            ON CONFLICT (spec_id) DO UPDATE SET
                version    = EXCLUDED.version,
                status     = EXCLUDED.status,
                document   = EXCLUDED.document,
                updated_at = now()
            """,
            spec.spec_id, spec.game_id, spec.version, spec.status,
            json.dumps(spec.to_dict(), ensure_ascii=False),
        )

    async def get_spec(self, spec_id: str) -> SpecDocument | None:
        import json

        row = await self._pool.fetchrow(
            "SELECT document FROM strategic_specs WHERE spec_id=$1", spec_id
        )
        if row is None:
            return None
        return _from_dict(json.loads(row["document"]))

    async def list_specs(self, game_id: str) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT spec_id, version, status, document->>'title' AS title, updated_at::text"
            " FROM strategic_specs WHERE game_id=$1 ORDER BY spec_id",
            game_id,
        )
        return [
            {
                "specId": row["spec_id"],
                "title": row["title"],
                "version": row["version"],
                "status": row["status"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    async def record_feedback(
        self,
        spec_id: str,
        source: str,
        feedback: str,
        decision: str,
        reason: str,
        from_version: int,
        to_version: int | None,
    ) -> int:
        row = await self._pool.fetchrow(
            """
            INSERT INTO strategic_feedback
                (spec_id, source, feedback, decision, reason, from_version, to_version)
            VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING feedback_id
            """,
            spec_id, source, feedback, decision, reason, from_version, to_version,
        )
        return int(row["feedback_id"])

    async def feedback_history(self, spec_id: str) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT feedback_id, source, feedback, decision, reason, from_version, to_version,"
            " created_at::text FROM strategic_feedback WHERE spec_id=$1 ORDER BY feedback_id",
            spec_id,
        )
        return [dict(row) for row in rows]


def _from_dict(data: dict[str, Any]) -> SpecDocument:
    return SpecDocument(
        spec_id=data["specId"],
        game_id=data["gameId"],
        title=data["title"],
        version=data["version"],
        blueprint_version=data["blueprintVersion"],
        refs=data["refs"],
        goal=data["goal"],
        implementation_scope=data["implementationScope"],
        out_of_scope=data["outOfScope"],
        acceptance_criteria=data["acceptanceCriteria"],
        unity_hints=data.get("unityHints", {}),
        dependencies=data.get("dependencies", []),
        status=data.get("status", "draft"),
        change_log=data.get("changeLog", []),
        architecture=[ArchGuidance.from_dict(g) for g in data.get("architecture") or []],
    )


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
