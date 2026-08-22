"""S5b·S7·S8 자체 점검 — 2026-08-02 warrior-loot 사고의 회귀 방지.

실제로 통과해버린 값을 그대로 넣어, 그때 잡지 못했던 것을 지금은 잡는지 본다.
프레임워크 없이 돌아간다: ``python -m strategic.test_spec_guards``
"""

from __future__ import annotations

from .arch_cards import ArchGuidance
from .specs import SpecDocument, _lint_contamination, _lint_measurable, _lint_role_boundary


def _spec(**overrides) -> SpecDocument:
    base = dict(
        spec_id="spec-001",
        game_id="warrior-loot",
        title="플레이어 이동 및 근접 전투",
        version=1,
        blueprint_version=1,
        refs=["ARCH-009"],
        goal="전사가 8방향으로 이동한다.",
        implementation_scope=["Rigidbody2D 기반 8방향 이동"],
        out_of_scope=["콤보"],
        acceptance_criteria=["플레이어 이동 속도는 초당 5유닛이다."],
    )
    base.update(overrides)
    return SpecDocument(**base)


def test_measurable_catches_unmeasurable_time() -> None:
    """S5b — 그때 통과했던 "0.3초 안에"를 지금은 잡는다."""

    spec = _spec(acceptance_criteria=["공격 입력 후 0.3초 안에 검 히트박스가 활성화된다."])
    assert _lint_measurable(spec), "재는 방법 없는 시간 기준을 놓쳤다"

    fixed = _spec(
        acceptance_criteria=[
            "공격 입력 후 0.3초 안에 히트박스가 활성화되며 "
            "PlayMode 테스트 Test_Attack_Hitbox_300ms 가 통과한다."
        ]
    )
    assert not _lint_measurable(fixed), "테스트 이름을 적었는데도 반려했다"


def test_measurable_catches_console_info_log() -> None:
    """S5b — QA 가 콘솔에서 읽는 것은 에러·경고뿐이다."""

    spec = _spec(acceptance_criteria=["드랍 발생 시 콘솔에 로그가 1건 출력된다."])
    assert _lint_measurable(spec), "정보성 콘솔 로그 기준을 놓쳤다"

    fixed = _spec(acceptance_criteria=["초과 획득 시 콘솔에 경고가 1건 출력된다."])
    assert not _lint_measurable(fixed), "경고는 QA 가 읽을 수 있으므로 통과해야 한다"


def test_contamination_catches_foreign_system() -> None:
    """S7 — 청크가 없는 게임에 청크 조항이 딸려 온 것을 잡는다."""

    spec = _spec(
        refs=["ARCH-004"],
        architecture=[
            ArchGuidance(
                card_id="ARCH-004",
                title="세이브 시스템",
                build_steps=["청크 연동 — ARCH-003 로더가 청크를 언로드하기 직전 상태를 넘긴다."],
                anti_patterns=["절대 경로 하드코딩"],
                verification=["왕복 검사"],
            )
        ],
    )
    assert _lint_contamination(spec), "남의 게임 조항(청크)을 놓쳤다"

    # 청크를 실제로 쓰는 게임이면 같은 문장이 정당하다.
    legit = _spec(refs=["ARCH-004", "ARCH-003"], architecture=spec.architecture)
    assert not _lint_contamination(legit), "정당한 상호 참조를 오탐했다"


def test_contamination_acceptance_applies_to_one_card_and_guard() -> None:
    guidance = ArchGuidance(
        card_id="ARCH-001",
        title="이벤트 버스",
        build_steps=["청크와 연동할 때 이벤트를 발행한다."],
        anti_patterns=["해설자 시스템을 직접 호출하지 않는다."],
        verification=["이벤트 왕복 검사"],
    )
    chunk_only = _spec(
        refs=["ARCH-001"],
        architecture=[guidance],
        contamination_acceptance=[
            {
                "cardId": "ARCH-001",
                "guardId": "chunk-streaming",
                "reason": "이 게임에는 청크 스트리밍이 없고 카드의 상호 참조만 수용한다.",
            }
        ],
    )

    errors = _lint_contamination(chunk_only)

    assert not any("청크/Chunk" in error for error in errors)
    assert any("commentator/해설자" in error for error in errors)

    accepted = _spec(
        refs=["ARCH-001"],
        architecture=[guidance],
        contamination_acceptance=[
            *chunk_only.contamination_acceptance,
            {
                "cardId": "ARCH-001",
                "guardId": "commentator",
                "reason": "이 게임에는 해설자가 없고 카드의 상호 참조만 수용한다.",
            },
        ],
    )
    assert not _lint_contamination(accepted)


def test_contamination_acceptance_does_not_cover_another_card() -> None:
    cards = [
        ArchGuidance(
            card_id=card_id,
            title="상호 참조 카드",
            build_steps=["청크 로더와 함께 쓸 때만 적용한다."],
            anti_patterns=["직접 결합"],
            verification=["왕복 검사"],
        )
        for card_id in ("ARCH-001", "ARCH-005", "ARCH-032")
    ]
    spec = _spec(
        refs=[card.card_id for card in cards],
        architecture=cards,
        contamination_acceptance=[
            {
                "cardId": "ARCH-001",
                "guardId": "chunk-streaming",
                "reason": "이 게임에는 청크가 없고 ARCH-001의 상호 참조만 수용한다.",
            }
        ],
    )

    errors = _lint_contamination(spec)

    assert len([error for error in errors if "청크/Chunk" in error]) == 2
    assert any("ARCH-005" in error for error in errors)
    assert any("ARCH-032" in error for error in errors)

    accepted = _spec(
        refs=[card.card_id for card in cards],
        architecture=cards,
        contamination_acceptance=[
            {
                "cardId": card.card_id,
                "guardId": "chunk-streaming",
                "reason": f"이 게임에는 청크가 없고 {card.card_id}의 상호 참조만 수용한다.",
            }
            for card in cards
        ],
    )
    assert not _lint_contamination(accepted)


def test_contamination_acceptance_rejects_unused_or_unexplained_records() -> None:
    guidance = ArchGuidance(
        card_id="ARCH-001",
        title="이벤트 버스",
        build_steps=["이벤트를 발행한다."],
        anti_patterns=["직접 결합"],
        verification=["왕복 검사"],
    )
    spec = _spec(
        refs=["ARCH-001"],
        architecture=[guidance],
        contamination_acceptance=[
            {"cardId": "ARCH-001", "guardId": "chunk-streaming", "reason": ""},
            {
                "cardId": "ARCH-001",
                "guardId": "commentator",
                "reason": "해설자 없는 게임의 상호 참조만 수용한다.",
            },
        ],
    )

    errors = _lint_contamination(spec)

    assert any("reason" in error for error in errors)
    assert any("실제 감지된 오염" in error for error in errors)


def test_contamination_acceptance_must_be_an_array() -> None:
    spec = _spec(contamination_acceptance={"cardId": "ARCH-001"})

    assert "S7: contaminationAcceptance 는 배열이어야 함" in _lint_contamination(spec)


def test_role_boundary_catches_csharp_names() -> None:
    """S8 — 기획이 클래스 이름을 지어버린 것을 잡는다."""

    spec = _spec(unity_hints={"components": ["PlayerController", "Animator"], "sceneObjects": []})
    errors = _lint_role_boundary(spec)
    assert len(errors) == 2, f"C# 타입명 2건을 잡아야 하는데 {len(errors)}건"

    fixed = _spec(
        unity_hints={
            "components": ["8방향 이동 처리", "근접 공격 판정"],
            "sceneObjects": ["플레이어 캐릭터"],
        }
    )
    assert not _lint_role_boundary(fixed), "우리말 기능 이름을 오탐했다"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("전부 통과")


if __name__ == "__main__":
    main()
