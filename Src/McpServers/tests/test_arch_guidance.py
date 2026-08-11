"""아키텍처 카드 원문이 spec 까지 그대로 실려 가는지 본다.

기획 AI 만 카드를 읽고, 개발 AI 와 QA 는 spec 만 읽는다. 그 구조가 성립하려면
카드의 세 절(구현 절차·안티패턴·검증 방법)이 **모델을 거치지 않고** spec 에
도착해야 한다. 여기서는 그 경로가 실제로 코드 경로인지, 그리고 인용만 남고
내용이 사라진 spec 이 발행되지 않는지를 고정한다.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("RESEARCH_DSN", "postgresql://unused/unused")

from strategic.arch_cards import (  # noqa: E402
    ArchGuidance,
    arch_ids,
    extract_section,
    guidance_from_body,
    is_arch_card,
    section_items,
)
from strategic.planner import _SPEC_SCHEMA, PLAN_SCHEMA  # noqa: E402
from strategic.server import _architecture_for, _to_feature_prompt, _to_spec  # noqa: E402
from strategic.specs import SpecDocument, lint_spec  # noqa: E402

# ARCH-003 (003_chunk_loader.md) 에서 형식을 그대로 가져온 축약본. 절 제목은
# 연구 저장소 lint_card.py 의 REQUIRED_SECTIONS["ARCH"] 가 강제하는 글자다.
CARD_BODY = """## 문제

세계를 청크로 쪼개도 언제 어느 청크를 켜고 끌지 정하는 주체가 없으면 소용이 없다.

## 구조

- 위치: `Assets/Scripts/World/ChunkLoader` — 플레이어 주변 3x3 청크만 활성화한다.

## 핵심 규칙

- 활성 범위는 중심 포함 3x3, 즉 9칸으로 고정한다.

## Unity 구현 절차

1. `Scripts/World/ChunkLoader.cs` 생성 — 청크 크기, 현재 좌표, 활성 집합 필드를 둔다.
2. 좌표 계산 함수 작성 — 월드 위치를 받아 청크 좌표를 돌려준다.
3. 갱신 판단 — 청크 좌표 변화 시에만 갱신 함수를 호출한다.

## 안티패턴

- 매 프레임 전체 재계산: 대부분의 프레임에서 결과가 같으므로 순수한 낭비다.
- 동기 언로드: 언로드를 동기로 처리하면 그 프레임이 멈춘다.

## 검증 방법

- 활성 개수 검사: 활성 청크 씬 개수가 9개 이하여야 한다.
- 로그 검사: 청크 진입 시 `Logs/chunk_loader.log`에 진입 이벤트 줄이 남아야 한다.

## 조합 궁합

- ARCH-002 (씬 스트리밍): 로더가 켜고 끄는 대상 구조.
"""

KNOWN = {"ELEM-003", "GENRE-006", "ARCH-003", "ARCH-002"}


def _guidance(card_id: str = "ARCH-003") -> ArchGuidance:
    return guidance_from_body(card_id, "청크 로더 (3x3 활성 규칙)", CARD_BODY)


def _spec(**overrides) -> SpecDocument:
    base = dict(
        spec_id="g1__spec-001",
        game_id="g1",
        title="청크 로더",
        version=1,
        blueprint_version=1,
        refs=["GENRE-006", "ARCH-003"],
        goal="플레이어 주변 월드를 끊김 없이 스트리밍한다.",
        implementation_scope=["ChunkLoader MonoBehaviour 작성"],
        out_of_scope=["세이브/로드"],
        acceptance_criteria=["화면 경계 도달 전 청크 3개가 미리 로드된다"],
        architecture=[_guidance()],
    )
    base.update(overrides)
    return SpecDocument(**base)


# ---------------------------------------------------------------------------
# 절 잘라내기 — tools/read_section.py 와 같은 결과를 내야 한다
# ---------------------------------------------------------------------------
def test_sections_are_extracted_verbatim():
    guidance = _guidance()

    assert len(guidance.build_steps) == 3
    assert guidance.build_steps[0].startswith("`Scripts/World/ChunkLoader.cs` 생성")
    assert len(guidance.anti_patterns) == 2
    assert len(guidance.verification) == 2
    assert guidance.empty_sections == []


def test_extraction_stops_at_the_next_section():
    """다음 ``## `` 까지만 자른다 — 넘치면 '조합 궁합' 이 절차에 섞인다."""

    assert "조합 궁합" not in extract_section(CARD_BODY, "검증 방법")
    assert "ARCH-002" not in " ".join(_guidance().verification)


def test_numbered_and_bulleted_items_both_parse():
    assert section_items("1. 하나\n2. 둘") == ["하나", "둘"]
    assert section_items("- 하나\n* 둘\n+ 셋") == ["하나", "둘", "셋"]


def test_wrapped_lines_stay_in_one_item():
    """한 항목을 두 줄로 접어 쓴 카드가 들어와도 항목이 쪼개지지 않아야 한다."""

    assert section_items("- 앞부분\n  뒤에 이어지는 설명\n- 다음 항목") == [
        "앞부분 뒤에 이어지는 설명",
        "다음 항목",
    ]


def test_missing_section_yields_empty_not_exception():
    """카드 하나가 깨졌을 때 기획 전체가 멈추면 안 된다 — 판정은 lint 가 한다."""

    guidance = guidance_from_body("ARCH-003", "t", "## 문제\n\n본문뿐이다.\n")

    assert guidance.build_steps == []
    assert guidance.empty_sections == ["Unity 구현 절차", "안티패턴", "검증 방법"]


@pytest.mark.parametrize(
    ("card_id", "expected"),
    [("ARCH-003", True), ("ARCH-3", False), ("ELEM-003", False), ("arch-003", False)],
)
def test_is_arch_card(card_id: str, expected: bool):
    assert is_arch_card(card_id) is expected


def test_arch_ids_keeps_citation_order_and_drops_duplicates():
    assert arch_ids(["GENRE-006", "ARCH-002", "ELEM-003", "ARCH-003", "ARCH-002"]) == [
        "ARCH-002",
        "ARCH-003",
    ]


# ---------------------------------------------------------------------------
# 모델이 이 세 절을 쓸 칸은 없어야 한다
# ---------------------------------------------------------------------------
def test_model_has_no_slot_to_author_architecture_text():
    """칸이 없으면 어긋날 수도 없다 — 이게 원문 보존의 근거다.

    모델은 refs 로 '어느 카드' 만 고른다. 절차·안티패턴·검증 방법을 담는
    필드가 스키마에 생기면 그 순간 모델이 카드를 바꿔 쓸 수 있게 된다.
    """

    fields = set(_SPEC_SCHEMA["properties"])

    assert "architecture" not in fields
    assert not {f for f in fields if "arch" in f.lower() or "antiPattern" in f}
    assert "refs" in _SPEC_SCHEMA["required"]
    assert _SPEC_SCHEMA["additionalProperties"] is False
    assert PLAN_SCHEMA["additionalProperties"] is False


def test_arch_cards_are_offered_for_citation_in_the_prompt():
    from strategic.planner import SYSTEM_PROMPT

    assert "ARCH" in SYSTEM_PROMPT
    # 카드 원문을 다시 쓰지 말라는 지시가 프롬프트에 남아 있어야 한다.
    assert "옮겨 적지 않는다" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 인용과 실린 내용이 어긋나면 발행하지 않는다 (S6)
# ---------------------------------------------------------------------------
def test_spec_carrying_its_cited_guidance_passes():
    assert lint_spec(_spec(), KNOWN) == []


def test_cited_arch_card_without_guidance_is_rejected():
    """인용만 있고 내용이 없으면 개발 AI 에게 아무 지시도 하지 않는다."""

    errors = lint_spec(_spec(architecture=[]), KNOWN)

    assert any("아키텍처 지침이 실리지 않음" in e for e in errors)


def test_guidance_for_an_uncited_card_is_rejected():
    errors = lint_spec(_spec(refs=["GENRE-006"]), KNOWN)

    assert any("refs 에 없는 카드의 지침" in e for e in errors)


def test_empty_section_in_a_cited_card_is_rejected():
    broken = guidance_from_body("ARCH-003", "t", "## 문제\n\n본문뿐이다.\n")

    errors = lint_spec(_spec(architecture=[broken]), KNOWN)

    assert any("절이 비어 있음" in e for e in errors)


def test_spec_without_any_arch_card_is_unaffected():
    """ARCH 를 인용하지 않는 spec 은 예전과 똑같이 통과해야 한다."""

    assert lint_spec(_spec(refs=["GENRE-006"], architecture=[]), KNOWN) == []


# ---------------------------------------------------------------------------
# 발행 경로 — refs 에서 지침을 붙인다
# ---------------------------------------------------------------------------
def test_architecture_is_attached_from_refs_not_from_model_output():
    raw = {
        "specId": "spec-001",
        "title": "청크 로더",
        "goal": "g",
        "implementationScope": ["a"],
        "outOfScope": ["b"],
        "acceptanceCriteria": ["청크 9개 이하가 활성이다"],
        "refs": ["GENRE-006", "ARCH-003"],
        "dependencies": [],
        "unityHints": {},
    }

    spec = _to_spec(raw, "g1", 1, {"ARCH-003": _guidance()})

    assert [g.card_id for g in spec.architecture] == ["ARCH-003"]
    assert lint_spec(spec, KNOWN) == []


def test_unknown_arch_card_is_dropped_so_lint_can_reject_the_spec():
    """DB 에 없는 ARCH 를 인용했다면 조용히 통과시키지 않는다."""

    assert _architecture_for(["ARCH-999"], {"ARCH-003": _guidance()}) == []
    assert any(
        "아키텍처 지침이 실리지 않음" in e
        for e in lint_spec(_spec(refs=["ARCH-999"], architecture=[]), KNOWN | {"ARCH-999"})
    )


# ---------------------------------------------------------------------------
# 개발 AI 와 QA 가 받는 형태
# ---------------------------------------------------------------------------
def test_feature_prompt_carries_the_card_text_to_codegen():
    """개발 AI 는 카드를 직접 읽지 않는다 — 프롬프트 안에 원문이 있어야 한다."""

    description = _to_feature_prompt(_spec())["description"]

    assert "## 아키텍처 지침" in description
    assert "### ARCH-003 청크 로더 (3x3 활성 규칙)" in description
    for heading in ("#### Unity 구현 절차", "#### 안티패턴", "#### 검증 방법"):
        assert heading in description
    assert "- 매 프레임 전체 재계산" in description  # 안티패턴이 그대로 간다
    assert "9개 이하여야 한다" in description  # QA 기준도 그대로 간다
    # 절차는 순서가 지시다 — 번호를 잃으면 순서가 우연처럼 보인다.
    assert "1. `Scripts/World/ChunkLoader.cs` 생성" in description
    assert "3. 갱신 판단" in description


def test_spec_markdown_and_dict_round_trip_the_guidance():
    """재발행(get_spec)이 지침을 잃으면 개발 AI 가 두 번째부터 다른 것을 받는다."""

    from strategic.specs import _from_dict

    spec = _spec()
    restored = _from_dict(spec.to_dict())

    assert [g.to_dict() for g in restored.architecture] == [
        g.to_dict() for g in spec.architecture
    ]
    assert "## 아키텍처 지침" in spec.to_markdown()
    assert lint_spec(restored, KNOWN) == []


def test_feature_prompt_without_arch_cards_has_no_empty_section():
    description = _to_feature_prompt(_spec(refs=["GENRE-006"], architecture=[]))["description"]

    assert "아키텍처 지침" not in description


# ---------------------------------------------------------------------------
# 실물 카드 — 절 제목이나 파일 형식이 바뀌면 조용히 빈 지침이 실린다
# ---------------------------------------------------------------------------
def _architecture_card_dir():
    """자매 저장소의 ARCH 카드 폴더. 없으면 None (CI 에 없을 수 있다)."""

    import os
    from pathlib import Path

    candidates = []
    if os.getenv("RESEARCH_REPO"):
        candidates.append(Path(os.environ["RESEARCH_REPO"]))
    # tests → McpServers → Src → Game-Developer-AI → (두 저장소가 나란히 있는 곳)
    candidates.append(Path(__file__).resolve().parents[4] / "Game-Design-and-Planning_resarch")
    for root in candidates:
        path = root / "research" / "architecture"
        if path.is_dir():
            return path
    return None


def test_every_real_arch_card_yields_all_three_sections():
    """빈 지침은 S6 가 잡지만, 그건 기획이 이미 돌아간 뒤다.

    카드 쪽 형식(절 제목, frontmatter, CRLF 줄끝)이 바뀌면 잘라내기가 조용히
    빈 결과를 내고, 그 spec 은 발행 단계에서야 반려된다. 여기서 미리 깨뜨린다.
    """

    import re
    import tomllib

    card_dir = _architecture_card_dir()
    if card_dir is None:
        pytest.skip("자매 저장소(Game-Design-and-Planning_resarch)를 찾지 못해 건너뜀")

    # tools/sync_db.py 의 FM_PAT 와 같다 — `+++ ` 뒤 공백과 CRLF 를 견뎌야 한다.
    frontmatter = re.compile(r"^\+\+\+\s*\n(.*?)\n\+\+\+\s*\n(.*)$", re.S)
    cards = sorted(card_dir.glob("*.md"))
    assert cards, f"ARCH 카드가 한 장도 없다: {card_dir}"

    for path in cards:
        match = frontmatter.match(path.read_text(encoding="utf-8"))
        assert match is not None, f"{path.name}: frontmatter 를 읽지 못했다"
        meta = tomllib.loads(match.group(1))
        guidance = guidance_from_body(
            str(meta["card_id"]), str(meta.get("title", "")), match.group(2).strip()
        )

        assert guidance.empty_sections == [], f"{path.name}: 빈 절 {guidance.empty_sections}"
        # 절차는 순서가 지시다 — 한 단계짜리 '절차'는 잘라내기가 실패한 신호다.
        assert len(guidance.build_steps) >= 2, f"{path.name}: 구현 절차가 {guidance.build_steps}"
