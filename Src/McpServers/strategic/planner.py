"""근거 카드 → 청사진 → spec 분해.

``prompts/6_planner.md`` 의 3단계(① 아이디어 제안 → ② 청사진 → ③ spec 분해)를
구현한다. 그 문서의 규칙 중 코드로 강제할 수 있는 것은 전부 강제한다.

* 인용은 **카드 ID 로만**. 웹 지식·일반론 인용 금지 (§2).
* 반례 카드 **최소 1장**. 못 찾으면 "반례 조사 부족"이라 명시하고 비워두지 않음 (§3).
* 1 spec = 1 메커니즘. "오픈월드 전체" 같은 덩어리 금지 (§5).
* 합격 기준은 숫자 또는 관찰 가능한 사실로만. 금지어 목록 존재 (§5).

스키마 준수는 프롬프트로 부탁하지 않고 **Structured Outputs 로 강제**한다.
"""

from __future__ import annotations

import json
import os
from typing import Any

from common.usage import usage_of

from .research_repo import ResearchEvidence
from .specs import BANNED_WORDS, OBSERVABLE_WORDS

MODEL = os.getenv("STRATEGIC_MODEL", "claude-opus-5")


class PlanningError(RuntimeError):
    """기획 생성 실패."""


# ---------------------------------------------------------------------------
# 출력 스키마 — 모델이 벗어날 수 없게 만든다
# ---------------------------------------------------------------------------
_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "specId": {"type": "string", "description": "spec-001 형식"},
        "title": {"type": "string", "description": "메커니즘 이름 (한 개)"},
        "goal": {"type": "string", "description": "이 메커니즘이 무엇을 달성하는가, 2~3문장"},
        "implementationScope": {
            "type": "array", "items": {"type": "string"},
            "description": "구현해야 할 것. Unity 개발자가 그대로 착수할 수 있게 구체적으로.",
        },
        "outOfScope": {
            "type": "array", "items": {"type": "string"},
            "description": "이 spec 에서 하지 않을 것. 범위 확장을 막는다.",
        },
        "acceptanceCriteria": {
            "type": "array", "items": {"type": "string"},
            "description": "숫자 또는 관찰 가능한 사실로만. 주관 형용사 금지.",
        },
        "refs": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "근거 카드 ID 목록 (ELEM/GENRE/GAME/ARCH-XXX). 제공된 것만 사용. "
                "이 spec 이 아키텍처 카드의 구조를 구현하면 그 ARCH ID 도 넣는다."
            ),
        },
        "dependencies": {
            "type": "array", "items": {"type": "string"},
            "description": "먼저 구현되어야 하는 다른 specId 목록",
        },
        "unityHints": {
            "type": "object",
            "properties": {
                # 기능 경계까지가 기획의 몫이다. 클래스 분해는 개발 AI 가
                # design_architecture 에서 정한다 (06 문서 §3.1).
                "components": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "이 메커니즘이 수행해야 할 **기능 덩어리**를 우리말로 쓴다 "
                        '(예: "청크 좌표 계산", "타일 생성", "활성 범위 관리"). '
                        "C# 클래스 이름을 짓지 않는다 — 코드 경계는 개발 AI 가 정한다."
                    ),
                },
                "sceneObjects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "게임 화면에 존재해야 하는 것을 우리말로 쓴다 "
                        '(예: "월드 전체를 담는 루트", "플레이어 캐릭터"). '
                        "GameObject 계층 경로를 지정하지 않는다."
                    ),
                },
                "assetsNeeded": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
            },
            "required": ["components", "sceneObjects", "assetsNeeded", "notes"],
            "additionalProperties": False,
        },
    },
    "required": [
        "specId", "title", "goal", "implementationScope", "outOfScope",
        "acceptanceCriteria", "refs", "dependencies", "unityHints",
    ],
    "additionalProperties": False,
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "genre": {"type": "string", "description": "2D 게임의 세부 장르"},
        "oneLine": {"type": "string"},
        "coreMechanics": {
            "type": "array", "items": {"type": "string"},
            "description": "3~6개. 각각 한 문장으로 검증 가능하게.",
        },
        "artStyle": {"type": "string"},
        "structureOverview": {"type": "string", "description": "월드 구조와 진행 흐름"},
        "synergyRationale": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cardId": {"type": "string"},
                    "reason": {"type": "string", "description": "이 카드가 왜 이 기획을 지지하는가"},
                },
                "required": ["cardId", "reason"],
                "additionalProperties": False,
            },
            "description": "지지 근거. 최소 2개.",
        },
        "counterEvidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cardId": {"type": "string"},
                    "risk": {"type": "string", "description": "이 사례가 경고하는 실패 요인"},
                    "mitigation": {"type": "string", "description": "그 위험을 어떻게 피하는가"},
                },
                "required": ["cardId", "risk", "mitigation"],
                "additionalProperties": False,
            },
            "description": "반례. 근거 카드가 있으면 반드시 채운다.",
        },
        "counterEvidenceNote": {
            "type": "string",
            "description": "반례 카드를 못 찾았을 때만 '반례 조사 부족'. 찾았으면 빈 문자열.",
        },
        "maxRisk": {"type": "string", "description": "가장 큰 위험 한 줄"},
        "specs": {"type": "array", "items": _SPEC_SCHEMA, "description": "3개 이상"},
    },
    "required": [
        "title", "genre", "oneLine", "coreMechanics", "artStyle", "structureOverview",
        "synergyRationale", "counterEvidence", "counterEvidenceNote", "maxRisk", "specs",
    ],
    "additionalProperties": False,
}


SYSTEM_PROMPT = f"""당신은 **아이디어를 심문하는 기획자**다. 칭찬이 아니라 검증이 임무다.

[청사진 출력 언어 — 2026-08-02 결정]
이 JSON 의 모든 문자열 필드(title/oneLine/coreMechanics/artStyle/
structureOverview/synergyRationale[].reason/counterEvidence[].risk,mitigation/
maxRisk, 그리고 각 spec 의 title/goal/implementationScope/outOfScope/
acceptanceCriteria/unityHints.components,sceneObjects,notes)를 **영어로** 쓴다.
assetsNeeded 는 원래도 영어였다 — 이 JSON 전체가 그대로 개발 AI 의
design_architecture 인자로 들어가므로, 여기 쓴 언어가 곧 개발 AI 가 받는
언어다. (사용자와의 대화는 이 프롬프트 밖의 일이라 이 규칙과 무관하다.)

[지식 규칙 — 위반 시 산출물 폐기]
- 인용은 아래에 주어진 리서치 카드 ID(ELEM/GENRE/GAME/ARCH-XXX)로만 한다.
- 주어지지 않은 카드 ID를 지어내지 않는다. 일반 웹 지식을 근거로 대지 않는다.
- 지지 근거는 최소 2장. 반례 카드가 제공되면 반드시 counterEvidence 에 담는다.
- 반례 카드가 하나도 제공되지 않았을 때만 counterEvidenceNote 에 "반례 조사 부족"을
  적고, counterEvidence 는 빈 배열로 둔다. 반례를 지어내지 않는다.

[아키텍처 카드(ARCH-XXX) 규칙]
- 이 카드는 근거가 아니라 **구현 구조**다. spec 이 그 구조를 만드는 것이면
  그 spec 의 refs 에 ARCH ID 를 넣는다. 관련이 없으면 넣지 않는다.
- **spec 마다 따로 고른다.** 후보 목록은 아이디어 전체로 한 번 뽑은 것이라
  spec 별로 맞는 카드가 다르다. 앞 spec 이 쓴 카드를 관성으로 다시 넣지 않는다
  — 몬스터 spec 에 플레이어 이동 카드를 넣으면 그 카드의 구현 절차("PlayerInput
  을 만든다")가 그대로 실려 앞 spec 의 결과물을 다시 만들라는 지시가 된다.
- 후보에 맞는 카드가 없으면 **refs 에 ARCH 를 넣지 않는다.** 억지로 가까운
  카드를 넣는 것이 안 넣는 것보다 나쁘다. 남의 게임 조항이 딸려 오기 때문이다.
- 규약 카드(폴더·로그·이벤트 버스·매니저 수명)는 눈에 띄지 않아도 거의 모든
  게임에 해당한다. 해당 spec 이 파일을 만들거나 기록을 남기면 넣는다.
- 그 카드의 구현 절차·안티패턴·검증 방법은 **한 줄도 옮겨 적지 않는다.**
  발행 단계에서 카드 원문이 그대로 붙는다. 요약하거나 바꿔 쓰면 원문과 어긋난다.
- 카드가 못 박은 수치나 범위를 바꾸지 않는다. 그 변경은 사람 승인 사항이다.
- implementationScope 에는 이 spec 고유의 것만 쓴다. 카드가 이미 정한 절차를
  다시 쓰면 같은 지시가 두 벌이 되고, 둘이 어긋났을 때 어느 쪽이 맞는지 알 수 없다.

[spec 분해 규칙]
- 1 spec = 1 메커니즘. "오픈월드 전체", "게임 완성" 같은 덩어리는 금지.
  예: "chunk loader", "chest interaction", "day-night cycle" 각각이 별개의 spec.
- specId 는 spec-001, spec-002 … 형식.
- dependencies 에는 먼저 구현되어야 하는 specId 만 넣는다. 순환 금지.
- unityHints 는 **기능 경계까지만** 쓴다. 코드 경계(어떤 클래스로 나눌지)는
  개발 AI 가 정하므로 여기서 짓지 않는다.
  - components: 이 메커니즘이 해야 할 일을 영어로. "chunk coordinate calculation",
    "tile generation" 은 좋고, "ChunkLoader", "PlayerController" 같은 C# 타입명은
    **쓰지 않는다.**
  - sceneObjects: 화면에 존재해야 하는 것을 영어로. "world root" 는 좋고
    "WorldRoot/Grid" 같은 계층 경로는 쓰지 않는다.
  - assetsNeeded: 필요한 그림을 종류와 크기가 드러나게, 영어 문구로 쓴다.
    (예: "sword icon 32x32", "player idle sprite") — 에셋 생성 API 가 영어
    프롬프트만 안정적으로 처리하기 때문이다. **캐릭터가 드는 무기·도구는
    캐릭터와 분리한 별도 항목으로 쓴다** — PixelLab 실측(12문서 §3-5)에서
    무기를 포함한 캐릭터 묘사가 5점 만점에 2점, 무기를 뺀 캐릭터 단독 묘사가
    5점이었다. `["knight character", "sword icon 32x32"]` 는 좋고
    `"knight character holding a sword"` 하나로 묶지 않는다.

[합격 기준 규칙 — 가장 중요]
- 숫자 또는 관찰 가능한 사실로만 쓴다.
- 금지어(쓰면 검사 실패): {", ".join(BANNED_WORDS)}
- 각 줄에는 숫자가 있거나 다음 중 하나가 포함되어야 한다: {", ".join(OBSERVABLE_WORDS)}
- **판정하는 쪽의 능력 안에서 쓴다.** QA 는 사람처럼 플레이하지 못하고 다음
  4가지로만 판정한다: ① 콘솔의 에러·경고 ② 자동 테스트(EditMode/PlayMode)
  ③ 씬·폴더 구조 검사 ④ Logs/ 아래 파일 로그.
  이 4가지로 확인할 수 없는 기준은 QA 가 BLOCKED 로 넘기고, BLOCKED 가 30%를
  넘으면 **스펙 자체가 반려된다.** 즉 못 재는 기준은 개발이 아니라 기획을 벌한다.
- 따라서 (영어 예시 — acceptanceCriteria 자체가 영어이므로 예시도 영어다):
  - 시간·확률 기준에는 **테스트 이름을 함께 적는다.**
    좋은 예: "opening the chest activates the inventory UI within 0.5s, verified
    by PlayMode test Test_Chest_OpensInventory_500ms"
    나쁜 예: "opening the chest activates the inventory UI within 0.5s" (재는
    방법 없음)
  - 확률은 눈으로 못 본다. 시드를 고정한 다회 시행 테스트로 바꾼다.
    좋은 예: "drop rate is 30±3% over 1000 fixed-seed trials, verified by test
    Test_DropRate_30Percent"
  - "console prints 1 log line" 은 쓰지 않는다. QA 가 콘솔에서 읽는 것은
    에러·경고뿐이다. 정보성 기록은 `Logs/` 파일 로그로 요구한다 (ARCH-010).
    좋은 예: "defeating a monster leaves 1 line in Logs/game.log formatted as
    [timestamp] [eventId] [summary]"

[대상]
2D 게임이다. 3D 전용 개념(Terrain, NavMesh 3D 등)을 쓰지 않는다."""


_CARD_SUMMARY_LIMIT = 100


def _truncate(text: str, limit: int = _CARD_SUMMARY_LIMIT) -> str:
    """카드 요약을 프롬프트에 넣기 전에 자른다 — 인용은 cardId 로만 하므로

    전문을 다 보여줄 필요가 없고, 카드 수가 늘어날수록 이 부분이 토큰 비용을
    선형으로 늘리는 지점이었다.
    """

    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _format_cards(evidence: ResearchEvidence) -> str:
    lines: list[str] = ["[지지 근거 카드]"]
    if evidence.supporting:
        for card in evidence.supporting:
            lines.append(
                f"- {card.card_id} [{card.kind}/{card.type}] {card.title}\n"
                f"    요약: {_truncate(card.summary)}\n"
                f"    태그: {', '.join(card.tags) or '-'} | 신뢰도: {card.confidence}"
            )
    else:
        lines.append("- (없음)")

    lines.append("")
    lines.append("[반례 카드 — 실패/혼재 사례]")
    if evidence.counterexamples:
        for card in evidence.counterexamples:
            lines.append(
                f"- {card.card_id} [{card.kind}/{card.type}] {card.title}\n"
                f"    요약: {_truncate(card.summary)}"
            )
    else:
        lines.append("- (없음 — counterEvidenceNote 에 '반례 조사 부족' 이라고 적을 것)")

    # 본문은 싣지 않는다 — 고르기만 하면 발행 단계가 원문을 붙인다.
    lines.append("")
    lines.append("[아키텍처 카드 — 구현 구조. 해당하는 spec 의 refs 에 ID 만 넣을 것]")
    if evidence.architecture:
        for card in evidence.architecture:
            lines.append(
                f"- {card.card_id} [{card.type}] {card.title}\n"
                f"    요약: {_truncate(card.summary)}"
            )
    else:
        lines.append("- (없음 — 어느 spec 에도 ARCH 를 인용하지 않는다)")
    return "\n".join(lines)


class Planner:
    """Claude 로 기획을 만든다."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model or MODEL
        self._client: Any | None = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise PlanningError(
                    "ANTHROPIC_API_KEY 가 없어 기획을 생성할 수 없습니다. "
                    "리서치 근거 조회(research_idea)는 키 없이도 동작합니다."
                )
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic()
        return self._client

    async def plan(
        self, idea: str, evidence: ResearchEvidence, feedback: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """아이디어 + 근거 → (청사진 + spec 목록, 토큰 사용량).

        사용량을 함께 돌려주는 이유: 이 프로세스만 Anthropic 응답을 본다.
        오케스트레이터가 잡별 비용을 기록하려면 여기서 넘겨줘야 한다.
        """

        client = self._ensure_client()

        user_prompt = (
            f"[사용자 아이디어]\n{idea}\n\n"
            f"{_format_cards(evidence)}\n\n"
            "위 카드만 근거로 삼아 2D 게임 기획을 만들고, 기능 단위 spec 으로 분해하라."
        )
        if feedback.strip():
            user_prompt += (
                "\n\n[이전 산출물에 대한 피드백 — 반드시 반영]\n"
                f"{feedback.strip()}"
            )

        response = await client.messages.create(
            model=self._model,
            max_tokens=32000,
            # plan() 과 revise_spec() 이 이 SYSTEM_PROMPT 를 매번 그대로 다시 보낸다
            # (재기획 루프에서는 최대 5회까지).
            #
            # 다만 캐시는 접두사가 claude-opus-5 의 최소 512토큰을 넘을 때만 성립하고,
            # 미달이면 오류 없이 무시된다. 이 프롬프트는 933자 한국어라 512토큰을
            # 넘을 가능성이 높지만 아직 실측하지 않았다. 확인 방법은 응답의
            # usage.cache_read_input_tokens 가 두 번째 호출부터 0보다 큰지 보는 것 —
            # common/usage.py::usage_of 가 이미 그 값을 싣고 있다.
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user_prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        )

        if response.stop_reason == "refusal":
            raise PlanningError(f"기획 생성이 거부되었습니다: {response.stop_details}")

        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            return json.loads(text), usage_of(response, self._model)
        except ValueError as exc:
            raise PlanningError(f"모델이 유효한 JSON 을 반환하지 않았습니다: {exc}") from exc

    async def add_spec(
        self,
        idea: str,
        evidence: ResearchEvidence,
        genre: str,
        existing_titles: list[str],
        next_spec_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """이미 발행된 게임에 spec 한 장을 더한다 → (새 spec, 토큰 사용량).

        ``revise_spec`` 이 기존 spec 하나를 고치는 것과 대칭이다 — 이건 새
        spec 하나를 **더한다.** ``plan()`` 과 같은 카드 규칙을 쓰되 청사진
        전체가 아니라 spec 하나만 낸다. ``existing_titles`` 는 같은 메커니즘을
        중복해서 만들지 않게 하는 문맥이고, ``next_spec_id`` 는 서버가 이미
        정한 번호다 — 모델이 번호를 고르게 하면 기존 spec 과 겹칠 수 있어서
        고정해 지시만 한다 (실제 배정은 호출자가 한다).
        """

        client = self._ensure_client()

        user_prompt = (
            f"[게임 장르]\n{genre}\n\n"
            "[이미 있는 기능들 — 이것과 겹치지 않는 새 기능을 만들 것]\n"
            + ("\n".join(f"- {t}" for t in existing_titles) or "(없음)")
            + f"\n\n[추가할 기능 아이디어]\n{idea}\n\n"
            f"{_format_cards(evidence)}\n\n"
            "위 카드만 근거로 삼아 이 게임에 spec 한 장을 새로 추가하라. "
            f'specId 는 "{next_spec_id}" 로 고정한다.'
        )

        response = await client.messages.create(
            model=self._model,
            max_tokens=16000,
            # SYSTEM_PROMPT 는 plan()/revise_spec() 과 캐시를 공유하도록 별도
            # 블록으로 캐싱하고, 이번 작업 전용 지시문만 캐시하지 않는 블록으로
            # 뒤에 붙인다 (revise_spec 과 같은 분할 이유).
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
                {
                    "type": "text",
                    "text": (
                        "\n\n[이번 작업]\n이미 발행된 게임에 새 기능(spec) 하나를 "
                        "추가한다. 기존 기능과 겹치지 않게, 1 spec = 1 메커니즘 "
                        "규칙을 그대로 지킨다."
                    ),
                },
            ],
            messages=[{"role": "user", "content": user_prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": _SPEC_SCHEMA}},
        )

        if response.stop_reason == "refusal":
            raise PlanningError(f"spec 추가가 거부되었습니다: {response.stop_details}")

        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            return json.loads(text), usage_of(response, self._model)
        except ValueError as exc:
            raise PlanningError(f"모델이 유효한 JSON 을 반환하지 않았습니다: {exc}") from exc

    async def revise_spec(
        self, spec_markdown: str, feedback: str, source: str, evidence_cards: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """개발/QA 피드백을 반영해 spec 한 장을 고친다 → (수정된 spec, 토큰 사용량)."""

        client = self._ensure_client()

        response = await client.messages.create(
            model=self._model,
            max_tokens=16000,
            # SYSTEM_PROMPT 는 plan() 과 캐시를 공유할 수 있게 별도 블록으로 캐싱하고,
            # revise 전용 지시문만 캐시하지 않는 블록으로 뒤에 붙인다.
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
                {
                    "type": "text",
                    "text": (
                        "\n\n[이번 작업]\n기존 spec 한 장을 피드백에 맞춰 수정한다. "
                        "specId 는 유지한다. 바꾼 이유가 없는 부분은 그대로 둔다."
                    ),
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"[기존 spec]\n{spec_markdown}\n\n"
                        f"[인용 가능한 카드]\n{evidence_cards}\n\n"
                        f"[{source} 피드백]\n{feedback}\n\n"
                        "이 피드백을 반영한 spec 을 출력하라."
                    ),
                }
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": _SPEC_SCHEMA}},
        )

        if response.stop_reason == "refusal":
            raise PlanningError(f"spec 수정이 거부되었습니다: {response.stop_details}")

        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            return json.loads(text), usage_of(response, self._model)
        except ValueError as exc:
            raise PlanningError(f"모델이 유효한 JSON 을 반환하지 않았습니다: {exc}") from exc
