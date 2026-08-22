"""spec 검사 규칙이 갈라지지 않는지 본다.

규칙은 원래 세 벌 있었다 — ``strategic/specs.py``, 연구 저장소의
``scripts/lint_spec.py``, 그리고 ``tests/test_evals.py`` 의 정규식. 층 B
(``UNITY-###``) 카드를 추가하려면 셋 다 고쳐야 했고, **하나를 놓쳐도 아무 소리가
나지 않았다.** 새 카드가 한쪽 검사기에서만 반려될 뿐이라, 증상이 "검색이 왜
이러지"로 나타나 원인까지 가는 길이 멀다.

지금은 ``spec_rules.json`` 한 벌뿐이다. 저장소 경계를 넘던 사본은 연구
저장소가 2026-07-31 (``88d624c``) 에 RAG 데이터 수집 전용으로 정리하면서
``scripts/lint_spec.py``·``spec_rules.json``·``spec_pipeline.py`` 를 함께
지워 사라졌다. **사본 감시 검사도 그때 같이 죽었어야 했는데 남아 있어서**,
없는 파일을 요구하며 계속 실패했다. 연구 저장소에 spec 검사를 다시 들이면
git 이력에서 되살린다 (지운 검사 이름: ``TestMirrorStaysInSync``).

남은 것은 규칙 파일과 그것을 읽는 코드가 어긋나지 않는지다. 호스트별 스킬에
규칙을 복제하지 않고 Research MCP의 단일 규칙 파일만 검증한다.
"""

from strategic.specs import (
    BANNED_WORDS,
    CARD_ID_PATTERN,
    CONTAMINATION_GUARDS,
    OBSERVABLE_WORDS,
    REQUIRED_SECTIONS,
    SPEC_RULES,
)

class TestRulesLoadFromData:
    def test_constants_come_from_the_rules_file(self) -> None:
        """상수를 코드에 다시 적어두면 규칙 파일을 고쳐도 안 바뀐다."""

        assert REQUIRED_SECTIONS == SPEC_RULES["requiredSections"]
        assert BANNED_WORDS == SPEC_RULES["bannedWords"]
        assert OBSERVABLE_WORDS == SPEC_RULES["observableWords"]

    def test_card_id_pattern_still_matches_the_three_layers(self) -> None:
        """규칙 파일을 잘못 고쳐 패턴이 무너지는 것을 잡는다."""

        for card_id in ("ELEM-001", "GENRE-010", "GAME-123"):
            assert CARD_ID_PATTERN.fullmatch(card_id), card_id
        for bogus in ("ELEM-1", "elem-001", "SPEC-001", "GAME-1234"):
            assert not CARD_ID_PATTERN.fullmatch(bogus), bogus

    def test_contamination_guards_have_unique_stable_ids(self) -> None:
        ids = [guard.get("id") for guard in CONTAMINATION_GUARDS]

        assert all(isinstance(guard_id, str) and guard_id for guard_id in ids)
        assert len(ids) == len(set(ids))
