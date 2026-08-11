"""아키텍처 카드(``ARCH-###``) → spec 에 실어 보낼 구현 지침.

기획 AI 는 **어느 아키텍처 카드를 쓸지 고르고**(``refs`` 에 카드 ID 를 넣는
것이 그 선택이다), 그 카드가 정한 구현 절차·안티패턴·검증 방법은 **이 모듈이
카드 원문에서 그대로 옮긴다.** 모델이 다시 쓰지 않는다.

왜 모델에게 맡기지 않는가. 같은 내용을 모델이 두 번 쓰면 두 번 달라진다.
spec 에 실린 절차가 카드 원문과 어긋나면 ``refs`` 의 인용은 검사를 통과하면서도
실제로는 다른 것을 지시하게 되고, 카드 인용의 실재성을 보던 검사(S3)가
내용까지는 보지 못하므로 그 어긋남을 아무도 잡지 못한다. 그래서 이 세 절은
``_SPEC_SCHEMA`` 에 자리를 주지 않는다 — 모델이 쓸 수 있는 칸이 없으면
어긋날 수도 없다.

절 제목은 연구 저장소 ``scripts/lint_card.py`` 의 ``REQUIRED_SECTIONS["ARCH"]``
가 카드마다 강제하므로 글자까지 일치한다. 잘라내는 방식은 같은 저장소의
``tools/read_section.py`` 와 같은 정규식이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# spec 문서와 개발 AI 프롬프트에 찍히는 **표시용** 제목. 사람과 모델이 읽는 말이다.
SECTION_BUILD_STEPS = "Unity 구현 절차"
SECTION_ANTI_PATTERNS = "안티패턴"
SECTION_VERIFICATION = "검증 방법"

# DB 에서 절을 **찾는** 키. 연구 저장소 ``card_schema.SECTIONS`` 의 ``section_key`` 다.
#
# 표시용 제목과 조회용 키를 나눈 이유: 전에는 위의 한국어 문자열로 카드 본문을
# 정규식으로 잘랐다. 연구 저장소가 카드를 영어로 옮기면 그 정규식은 **조용히
# 아무것도 못 찾고** 빈 지침을 만든다 — 그러면 spec 에 구현 절차가 실리지 않고
# lint_spec S6 가 반려하는데, 원인은 번역이지 기획이 아니라서 추적이 어렵다.
# section_key 는 언어가 바뀌어도 고정이므로 그 사고가 아예 불가능해진다.
KEY_BUILD_STEPS = "unity_procedure"
KEY_ANTI_PATTERNS = "antipatterns"
KEY_VERIFICATION = "verification"

# (조회 키, 표시 제목) — 순서가 곧 지침에 실리는 순서다.
GUIDANCE_SECTIONS: list[tuple[str, str]] = [
    (KEY_BUILD_STEPS, SECTION_BUILD_STEPS),
    (KEY_ANTI_PATTERNS, SECTION_ANTI_PATTERNS),
    (KEY_VERIFICATION, SECTION_VERIFICATION),
]

ARCH_ID_PATTERN = re.compile(r"ARCH-\d{3}")

# 항목의 시작. 구현 절차는 번호("1. "), 안티패턴·검증 방법은 글머리("- ").
_ITEM_START = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def is_arch_card(card_id: str) -> bool:
    """``ARCH-003`` 처럼 아키텍처 카드 ID 인가."""

    return bool(ARCH_ID_PATTERN.fullmatch(card_id))


def arch_ids(card_ids: list[str]) -> list[str]:
    """카드 ID 목록에서 아키텍처 카드만 순서를 지켜 골라낸다 (중복 제거)."""

    seen: set[str] = set()
    picked: list[str] = []
    for card_id in card_ids:
        if is_arch_card(card_id) and card_id not in seen:
            seen.add(card_id)
            picked.append(card_id)
    return picked


def extract_section(body: str, section: str) -> str:
    """``## {section}`` 부터 다음 ``## `` 까지. 없으면 빈 문자열."""

    match = re.search(
        rf"^## {re.escape(section)}\s*$(.*?)(?=^## |\Z)", body, re.S | re.M
    )
    return match.group(1).strip() if match else ""


def section_items(text: str) -> list[str]:
    """글머리·번호 목록을 항목 배열로. 이어지는 들여쓰기 줄은 앞 항목에 붙인다.

    카드는 한 항목을 한 줄로 쓰지만(ARCH-003 기준) 줄을 접어 쓴 카드가 들어와도
    항목이 쪼개지지 않게 한다.
    """

    items: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if _ITEM_START.match(line):
            items.append(_ITEM_START.sub("", line).strip())
        elif items:
            items[-1] = f"{items[-1]} {line.strip()}"
    return items


@dataclass(frozen=True)
class ArchGuidance:
    """한 아키텍처 카드가 spec 에 싣는 것. 전부 카드 원문에서 옮긴 값이다."""

    card_id: str
    title: str
    build_steps: list[str]
    anti_patterns: list[str]
    verification: list[str]

    @property
    def empty_sections(self) -> list[str]:
        """비어 있는 절 제목 목록. 비어 있으면 카드 쪽이 깨진 것이다."""

        return [
            title
            for (_key, title), items in zip(
                GUIDANCE_SECTIONS,
                (self.build_steps, self.anti_patterns, self.verification),
            )
            if not items
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cardId": self.card_id,
            "title": self.title,
            "buildSteps": self.build_steps,
            "antiPatterns": self.anti_patterns,
            "verification": self.verification,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchGuidance:
        return cls(
            card_id=data["cardId"],
            title=data.get("title", ""),
            build_steps=list(data.get("buildSteps") or []),
            anti_patterns=list(data.get("antiPatterns") or []),
            verification=list(data.get("verification") or []),
        )

    def to_markdown(self) -> str:
        """spec 문서와 개발 AI 프롬프트에 같은 형태로 실린다.

        구현 절차만 번호를 붙인다. 카드가 번호로 쓴 이유가 순서이기 때문이다 —
        "상태 저장 → 씬 언로드 → 리소스 정리" 같은 항목을 글머리로 늘어놓으면
        순서가 지시가 아니라 우연처럼 보인다. 안티패턴과 검증 방법은 순서가 없다.
        """

        lines = [f"### {self.card_id} {self.title}".rstrip()]
        for heading, items, ordered in (
            (SECTION_BUILD_STEPS, self.build_steps, True),
            (SECTION_ANTI_PATTERNS, self.anti_patterns, False),
            (SECTION_VERIFICATION, self.verification, False),
        ):
            lines.append(f"#### {heading}")
            if not items:
                lines.append("- (카드에 내용이 없음)")
            elif ordered:
                lines += [f"{i}. {item}" for i, item in enumerate(items, 1)]
            else:
                lines += [f"- {item}" for item in items]
        return "\n".join(lines)


def guidance_from_sections(card_id: str, title: str, sections: dict[str, str]) -> ArchGuidance:
    """``section_key -> 절 본문`` 에서 지침을 만든다 (DB ``card_sections`` 경로).

    카드 본문을 정규식으로 자르던 ``guidance_from_body`` 를 대신한다. 절 경계는
    이미 연구 저장소의 ``sync_db.py`` 가 표준 사전으로 나눠 DB에 넣었으므로,
    여기서 다시 파싱할 이유가 없다 — 파싱이 두 곳에 있으면 언젠가 갈라진다.

    절이 비어 있어도 예외를 내지 않는다 — 어느 카드의 어느 절이 비었는지는
    ``ArchGuidance.empty_sections`` 로 드러나고, 그 판정은 spec 검사(S6)가
    한다. 여기서 던지면 카드 하나가 깨졌을 때 기획 전체가 멈춘다.
    """

    return ArchGuidance(
        card_id=card_id,
        title=title,
        build_steps=section_items(sections.get(KEY_BUILD_STEPS, "")),
        anti_patterns=section_items(sections.get(KEY_ANTI_PATTERNS, "")),
        verification=section_items(sections.get(KEY_VERIFICATION, "")),
    )


def guidance_from_body(card_id: str, title: str, body: str) -> ArchGuidance:
    """카드 **본문**에서 세 절을 잘라 지침을 만든다 (구식 경로).

    DB 가 스키마 v1(``card_sections`` 없음)일 때의 폴백으로만 남겨 둔다.
    절 제목이 한국어라고 가정하므로 카드를 영어로 옮기면 빈 지침을 낸다 —
    새 코드는 ``guidance_from_sections`` 를 쓸 것.
    """

    return ArchGuidance(
        card_id=card_id,
        title=title,
        build_steps=section_items(extract_section(body, SECTION_BUILD_STEPS)),
        anti_patterns=section_items(extract_section(body, SECTION_ANTI_PATTERNS)),
        verification=section_items(extract_section(body, SECTION_VERIFICATION)),
    )
