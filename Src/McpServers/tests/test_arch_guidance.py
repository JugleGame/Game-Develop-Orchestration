"""아키텍처 카드 원문이 spec 까지 그대로 실려 가는지 본다.

Research MCP가 카드를 읽고 호스트는 spec을 사용한다. 그 구조가 성립하려면
카드의 세 절(구현 절차·안티패턴·검증 방법)이 **재작성 없이** spec 에
도착해야 한다. 여기서는 그 경로가 실제로 코드 경로인지, 그리고 인용만 남고
내용이 사라진 spec 이 발행되지 않는지를 고정한다.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("RESEARCH_DSN", "postgresql://unused/unused")

from strategic.arch_cards import (  # noqa: E402
    ArchGuidance,
    KEY_ANTI_PATTERNS,
    KEY_BUILD_STEPS,
    KEY_VERIFICATION,
    arch_ids,
    guidance_from_sections,
    is_arch_card,
    section_items,
)
from strategic.server import _architecture_for, _to_feature_prompt, _to_spec  # noqa: E402
from strategic.specs import SpecDocument, lint_spec  # noqa: E402

CARD_SECTIONS = {
    KEY_BUILD_STEPS: """1. Create `Scripts/World/ChunkLoader.cs` with chunk size, current coordinates, and active-set fields.
2. Implement coordinate calculation from a world position.
3. Refresh only when the chunk coordinates change.""",
    KEY_ANTI_PATTERNS: """- Full recomputation every frame wastes work when the result is unchanged.
- Synchronous unloading stalls the current frame.""",
    KEY_VERIFICATION: """- Active-count check: no more than nine chunk scenes are active.
- Log check: entering a chunk appends an event to `Logs/chunk_loader.log`.""",
}

KNOWN = {"ELEM-003", "GENRE-006", "ARCH-003", "ARCH-002"}


def _guidance(card_id: str = "ARCH-003") -> ArchGuidance:
    return guidance_from_sections(card_id, "Chunk Loader (3x3 Active Rule)", CARD_SECTIONS)


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
def test_sections_are_mapped_from_stable_keys():
    guidance = _guidance()

    assert len(guidance.build_steps) == 3
    assert guidance.build_steps[0].startswith("Create `Scripts/World/ChunkLoader.cs`")
    assert len(guidance.anti_patterns) == 2
    assert len(guidance.verification) == 2
    assert guidance.empty_sections == []


def test_numbered_and_bulleted_items_both_parse():
    assert section_items("1. one\n2. two") == ["one", "two"]
    assert section_items("- one\n* two\n+ three") == ["one", "two", "three"]


def test_wrapped_lines_stay_in_one_item():
    """한 항목을 두 줄로 접어 쓴 카드가 들어와도 항목이 쪼개지지 않아야 한다."""

    assert section_items("- first part\n  continued detail\n- next item") == [
        "first part continued detail",
        "next item",
    ]


def test_missing_section_yields_empty_not_exception():
    """카드 하나가 깨졌을 때 기획 전체가 멈추면 안 된다 — 판정은 lint 가 한다."""

    guidance = guidance_from_sections("ARCH-003", "t", {})

    assert guidance.build_steps == []
    assert guidance.empty_sections == [
        "Unity Implementation Steps",
        "Anti-patterns",
        "Verification",
    ]


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
# 호스트 입력이 카드 원문을 덮어쓸 수 없어야 한다
# ---------------------------------------------------------------------------
def test_host_spec_cannot_override_architecture_text():
    raw = {
        "specId": "spec-001",
        "title": "청크 로더",
        "goal": "g",
        "implementationScope": ["a"],
        "outOfScope": ["b"],
        "acceptanceCriteria": ["청크 9개 이하가 활성이다"],
        "refs": ["ARCH-003"],
        "architecture": [{"cardId": "ARCH-003", "buildSteps": ["변조"]}],
    }

    spec = _to_spec(raw, "g1", 1, {"ARCH-003": _guidance()})

    assert spec.architecture[0].build_steps[0] != "변조"
    assert spec.architecture[0].build_steps == _guidance().build_steps


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
    broken = guidance_from_sections("ARCH-003", "t", {})

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
# 호스트가 받는 형태
# ---------------------------------------------------------------------------
def test_feature_prompt_carries_the_card_text_to_codegen():
    """호스트가 별도 카드 조회 없이 구현할 수 있도록 원문을 포함한다."""

    description = _to_feature_prompt(_spec())["description"]

    assert "## Architecture guidance" in description
    assert "### ARCH-003 Chunk Loader (3x3 Active Rule)" in description
    for heading in ("#### Unity Implementation Steps", "#### Anti-patterns", "#### Verification"):
        assert heading in description
    assert "- Full recomputation every frame" in description
    assert "no more than nine chunk scenes" in description
    # 절차는 순서가 지시다 — 번호를 잃으면 순서가 우연처럼 보인다.
    assert "1. Create `Scripts/World/ChunkLoader.cs`" in description
    assert "3. Refresh only when" in description


def test_spec_markdown_and_dict_round_trip_the_guidance():
    """재발행(get_spec)이 지침을 잃으면 호스트가 두 번째부터 다른 것을 받는다."""

    from strategic.specs import _from_dict

    spec = _spec()
    restored = _from_dict(spec.to_dict())

    assert [g.to_dict() for g in restored.architecture] == [
        g.to_dict() for g in spec.architecture
    ]
    assert "## Architecture guidance" in spec.to_markdown()
    assert lint_spec(restored, KNOWN) == []


def test_feature_prompt_without_arch_cards_has_no_empty_section():
    description = _to_feature_prompt(_spec(refs=["GENRE-006"], architecture=[]))["description"]

    assert "Architecture guidance" not in description
