"""StrategicMcpServer 검증 — DB·LLM 없이 도는 규칙 부분.

실제 Neon/Claude 를 태우는 확인은 README 의 수동 절차를 따른다. 여기서는
``prompts/6_planner.md`` 가 정한 규칙이 코드로 강제되는지를 고정한다.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("RESEARCH_DSN", "postgresql://unused/unused")

from strategic.research_repo import COUNTEREXAMPLE_MISSING, Card, ResearchEvidence  # noqa: E402
from strategic.server import (  # noqa: E402
    _unity_project_setup_guidance,
    _next_spec_id,
    _publication_state,
    _to_feature_prompt,
    _to_spec,
    _visual_dimension,
)
from strategic.specs import SpecDocument, dependency_errors, dependency_order, lint_spec  # noqa: E402

KNOWN = {"ELEM-003", "GAME-013", "GENRE-006"}


def test_unity_project_setup_guidance_routes_2d_and_3d_without_mixing_templates():
    assert _unity_project_setup_guidance("2D")["unityHubTemplate"] == "Universal 2D"
    assert _unity_project_setup_guidance("3D")["unityHubTemplate"] == "Universal 3D"

    with pytest.raises(Exception, match="requires visualDimension"):
        _unity_project_setup_guidance("hybrid")


def _spec(**overrides) -> SpecDocument:
    base = dict(
        spec_id="g1__spec-001",
        game_id="g1",
        title="청크 로더",
        version=1,
        blueprint_version=1,
        refs=["GENRE-006"],
        goal="플레이어 주변 월드를 끊김 없이 스트리밍한다.",
        implementation_scope=["ChunkLoader MonoBehaviour 작성"],
        out_of_scope=["세이브/로드"],
        acceptance_criteria=["화면 경계 도달 전 청크 3개가 미리 로드된다"],
    )
    base.update(overrides)
    return SpecDocument(**base)


# ---------------------------------------------------------------------------
# lint — scripts/lint_spec.py 와 같은 판정을 내야 한다
# ---------------------------------------------------------------------------
def test_valid_spec_passes():
    assert lint_spec(_spec(), KNOWN) == []


def test_unknown_card_is_rejected():
    """§2: 존재하지 않는 카드 ID 를 인용할 수 없다."""

    errors = lint_spec(_spec(refs=["ELEM-999"]), KNOWN)

    assert any("존재하지 않는 카드" in e for e in errors)


def test_malformed_card_id_is_rejected():
    errors = lint_spec(_spec(refs=["ELEM-3"]), KNOWN)

    assert any("카드 ID 형식" in e for e in errors)


def test_empty_refs_is_rejected():
    """근거 없는 spec 은 발행할 수 없다."""

    assert any("refs" in e for e in lint_spec(_spec(refs=[]), KNOWN))


@pytest.mark.parametrize("banned", ["재미", "좋은", "멋진", "자연스러운", "적절한", "재치있는"])
def test_subjective_acceptance_criteria_are_rejected(banned: str):
    """§5: 합격 기준에 주관적 표현을 쓸 수 없다."""

    errors = lint_spec(_spec(acceptance_criteria=[f"조작감이 {banned} 상태여야 한다"]), KNOWN)

    assert any("측정 불가 표현" in e for e in errors)


def test_acceptance_criteria_without_number_or_observable_is_rejected():
    errors = lint_spec(_spec(acceptance_criteria=["플레이어가 이동할 수 있어야 한다"]), KNOWN)

    assert any("숫자/관찰 키워드 없음" in e for e in errors)


@pytest.mark.parametrize(
    "criterion",
    [
        "청크 3개가 미리 로드된다",
        "콘솔에 예외가 기록되지 않는다",
        "테스트 씬에서 확인된다",
        "0.5초 안에 UI 가 활성화된다 (UI활성화_테스트로 확인)",
    ],
)
def test_measurable_acceptance_criteria_pass(criterion: str):
    assert lint_spec(_spec(acceptance_criteria=[criterion]), KNOWN) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("goal", ""), ("implementation_scope", []), ("out_of_scope", []), ("acceptance_criteria", [])],
)
def test_missing_required_section_is_rejected(field: str, value):
    assert any("필수 섹션" in e for e in lint_spec(_spec(**{field: value}), KNOWN))


# ---------------------------------------------------------------------------
# spec 문서 형식
# ---------------------------------------------------------------------------
def test_markdown_has_toml_frontmatter_and_required_sections():
    markdown = _spec().to_markdown()

    assert markdown.startswith("+++\n")
    assert 'spec_id = "g1__spec-001"' in markdown
    assert "blueprint_version = 1" in markdown
    for section in (
        "## Goal",
        "## Implementation scope",
        "## Out of scope",
        "## Acceptance criteria",
    ):
        assert section in markdown


def test_change_log_is_rendered_after_revision():
    spec = _spec(version=2, change_log=[{"version": 2, "source": "qa", "change": "낙하 판정 추가"}])

    assert "## change_log" in spec.to_markdown()
    assert "v2 (qa): 낙하 판정 추가" in spec.to_markdown()


def test_contamination_acceptance_survives_documents_and_feature_prompt():
    from strategic.specs import _from_dict

    acceptance = [
        {
            "cardId": "ARCH-001",
            "guardId": "chunk-streaming",
            "reason": "이 게임에는 청크가 없고 카드의 상호 참조만 수용한다.",
        }
    ]
    spec = _spec(contamination_acceptance=acceptance)
    restored = _from_dict(spec.to_dict())
    prompt = _to_feature_prompt(restored)

    assert restored.contamination_acceptance == acceptance
    assert "## Contamination acceptance" in restored.to_markdown()
    assert "ARCH-001 / chunk-streaming" in prompt["description"]
    assert prompt["contaminationAcceptance"] == acceptance


# ---------------------------------------------------------------------------
# 개발 AI 에게 전달되는 형태
# ---------------------------------------------------------------------------
def test_feature_prompt_carries_everything_needed_to_implement():
    """청사진은 넘기지 않으므로 프롬프트 하나로 구현이 가능해야 한다 (§4)."""

    spec = _spec(
        unity_hints={
            "components": ["Rigidbody2D", "TilemapRenderer"],
            "sceneObjects": ["WorldRoot"],
            "assetsNeeded": ["terrain tile"],
            "notes": "카메라 경계 기준으로 계산",
        }
    )

    prompt = _to_feature_prompt(spec)

    assert prompt["feature_id"] == "g1__spec-001"
    for heading in (
        "## Goal",
        "## Implementation scope",
        "## Out of scope",
        "## Acceptance criteria",
        "## Unity hints",
    ):
        assert heading in prompt["description"]
    assert "Rigidbody2D" in prompt["description"]
    assert "GENRE-006" in prompt["description"]  # 근거 카드가 함께 간다


def test_assets_needed_travels_as_a_list_not_only_as_prose():
    """AssetGen 이 자산을 하나씩 요청하려면 구조가 있어야 한다 (§05 §4.4).

    문장으로만 주면 오케스트레이터는 description 전체를 프롬프트로 쓰게 되고,
    어떤 자산이 만들어질지가 키워드 등장 순서로 정해진다.
    """

    spec = _spec(unity_hints={"assetsNeeded": ["플레이어 스프라이트", "체력 HUD"]})

    prompt = _to_feature_prompt(spec)

    assert prompt["assets_needed"] == ["플레이어 스프라이트", "체력 HUD"]
    # 문장 쪽도 그대로 남는다 — 코드 생성기가 읽는 것은 여전히 description 이다.
    assert "체력 HUD" in prompt["description"]


def test_assets_needed_is_empty_when_the_plan_omits_hints():
    """힌트를 안 채운 기획도 예전처럼 동작해야 한다 (필드는 추가 변경)."""

    assert _to_feature_prompt(_spec(unity_hints={}))["assets_needed"] == []


def test_structured_asset_specs_travel_without_prose_parsing():
    asset_spec = {"assetId": "black-laptop", "assetName": "Black laptop"}
    prompt = _to_feature_prompt(_spec(unity_hints={"assetSpecs": [asset_spec]}))

    assert prompt["asset_specs"] == [asset_spec]
    assert prompt["assets_needed"] == ["Black laptop"]


def test_structured_asset_specs_reject_non_object_entries():
    errors = lint_spec(_spec(unity_hints={"assetSpecs": ["black laptop"]}), KNOWN)

    assert "asset handoff: unityHints.assetSpecs[0] must be an object" in errors


def test_visual_dimension_normalizes_and_rejects_unknown_values():
    assert _visual_dimension({"visualDimension": "3D"}) == "3d"
    assert _visual_dimension({}) == "unspecified"
    with pytest.raises(Exception, match="visualDimension"):
        _visual_dimension({"visualDimension": "VR"})


def test_dependencies_are_namespaced_per_game():
    """spec-002 가 spec-001 에 의존한다면, 게임 단위로 유일해야 한다."""

    spec = _to_spec(
        {
            "specId": "spec-002",
            "title": "상자 상호작용",
            "goal": "g",
            "implementationScope": ["a"],
            "outOfScope": ["b"],
            "acceptanceCriteria": ["1초 안에 열린다"],
            "refs": ["GENRE-006"],
            "dependencies": ["spec-001"],
            "unityHints": {},
        },
        game_id="g1",
        blueprint_version=3,
    )

    assert spec.spec_id == "g1__spec-002"
    assert spec.dependencies == ["g1__spec-001"]
    assert spec.blueprint_version == 3


def test_to_spec_accepts_host_contamination_judgment():
    acceptance = [
        {
            "cardId": "ARCH-001",
            "guardId": "chunk-streaming",
            "reason": "상호 참조만 수용한다.",
        }
    ]
    spec = _to_spec(
        {
            "specId": "spec-001",
            "title": "이벤트 전달",
            "goal": "g",
            "implementationScope": ["a"],
            "outOfScope": ["b"],
            "acceptanceCriteria": ["테스트가 통과한다"],
            "refs": ["ARCH-001"],
            "contaminationAcceptance": acceptance,
        },
        game_id="g1",
        blueprint_version=1,
    )

    assert spec.contamination_acceptance == acceptance


def test_feature_prompt_priority_reflects_dependency_order():
    """의존성이 없는 spec 이 먼저 구현되도록 P0 를 준다."""

    assert _to_feature_prompt(_spec(dependencies=[]))["priority"] == "P0"
    assert _to_feature_prompt(_spec(dependencies=["g1__spec-001"]))["priority"] == "P1"


def test_game_designs_are_drafts_until_the_planner_explicitly_publishes_them():
    assert _publication_state({}) == "draft"
    assert _publication_state({"status": "published"}) == "published"


def test_dependency_graph_rejects_missing_self_and_cyclic_dependencies():
    first = _spec(spec_id="g1__spec-001", dependencies=["g1__spec-002"])
    second = _spec(spec_id="g1__spec-002", dependencies=["g1__spec-001"])
    broken = _spec(spec_id="g1__spec-003", dependencies=["g1__spec-003", "g1__spec-999"])

    errors = dependency_errors([first, second, broken])

    assert any("cycle detected" in error for error in errors)
    assert any("cannot depend on itself" in error for error in errors)
    assert any("missing specification" in error for error in errors)


def test_dependency_order_places_prerequisites_before_their_dependents():
    first = _spec(spec_id="g1__spec-001")
    second = _spec(spec_id="g1__spec-002", dependencies=["g1__spec-001"])

    ordered = dependency_order([second, first])

    assert [spec.spec_id for spec in ordered] == ["g1__spec-001", "g1__spec-002"]


# ---------------------------------------------------------------------------
# add_spec — 다음 spec 번호 산정 (겹치면 기존 spec 을 덮어쓴다)
# ---------------------------------------------------------------------------
def test_next_spec_id_starts_at_one_for_a_new_game():
    assert _next_spec_id([]) == "spec-001"


def test_next_spec_id_follows_the_highest_existing_number():
    existing = [{"specId": "g1__spec-001"}, {"specId": "g1__spec-002"}]

    assert _next_spec_id(existing) == "spec-003"


def test_next_spec_id_ignores_list_order_and_gaps():
    """번호는 목록 순서가 아니라 최댓값 기준이다 — 중간이 비어도 안전하다."""

    existing = [{"specId": "g1__spec-005"}, {"specId": "g1__spec-002"}]

    assert _next_spec_id(existing) == "spec-006"


def test_next_spec_id_ignores_malformed_entries():
    existing = [{"specId": "g1__spec-001"}, {"specId": ""}, {}]

    assert _next_spec_id(existing) == "spec-002"


# ---------------------------------------------------------------------------
# 반례 규칙
# ---------------------------------------------------------------------------
def _card(card_id: str, kind: str, type_: str) -> Card:
    return Card(
        card_id=card_id, kind=kind, type=type_, title="t", summary="s",
        tags=[], elements=[], genres=[], confidence="high", updated="2026-01-01",
    )


def test_missing_counterexample_produces_the_required_phrase():
    """§3: 반례가 없으면 비워두지 말고 '반례 조사 부족'이라 명시해야 한다."""

    evidence = ResearchEvidence(query="q", supporting=[_card("GENRE-006", "GENRE", "genre")])

    assert evidence.has_counterexample is False
    assert evidence.counterexample_note() == COUNTEREXAMPLE_MISSING
    assert evidence.to_dict()["counterexampleNote"] == COUNTEREXAMPLE_MISSING


def test_present_counterexample_leaves_the_note_empty():
    evidence = ResearchEvidence(
        query="q",
        supporting=[_card("GENRE-006", "GENRE", "genre")],
        counterexamples=[_card("GAME-004", "GAME", "failure")],
    )

    assert evidence.counterexample_note() == ""


def test_citable_ids_cover_both_sides():
    """LLM 이 인용해도 되는 ID 는 조회된 카드로 한정된다."""

    evidence = ResearchEvidence(
        query="q",
        supporting=[_card("ELEM-003", "ELEM", "mechanic")],
        counterexamples=[_card("GAME-004", "GAME", "failure")],
    )

    assert evidence.to_dict()["citableCardIds"] == ["ELEM-003", "GAME-004"]
