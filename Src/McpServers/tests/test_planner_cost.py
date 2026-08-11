"""planner.py 의 토큰 절감 로직 — 요약 길이 절단과 프롬프트 캐싱 표시.

실제 Anthropic 호출은 하지 않는다(API 키 없이 도는 단위 테스트). 캐싱은
``AsyncAnthropic`` 을 스텁으로 바꿔치기해 ``messages.create`` 에 넘어가는
``system`` 파라미터의 실제 모양만 확인한다 — 서버가 실제로 캐시 적중을
얻는지는 라이브 API 호출로만 확인 가능하지만, 요청 본문이 SDK 가 요구하는
``cache_control`` 형태를 갖추는지는 여기서 고정할 수 있다.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("RESEARCH_DSN", "postgresql://unused/unused")
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

from strategic.planner import SYSTEM_PROMPT, Planner, _format_cards, _truncate  # noqa: E402
from strategic.research_repo import Card, ResearchEvidence  # noqa: E402


def _card(card_id: str, summary: str) -> Card:
    return Card(
        card_id=card_id, kind="GAME", type="success", title="t", summary=summary,
        tags=["tag"], elements=[], genres=[], confidence="high", updated="2026-01-01",
    )


# ---------------------------------------------------------------------------
# _truncate / _format_cards — 요약 절단
# ---------------------------------------------------------------------------
def test_truncate_leaves_short_text_untouched():
    assert _truncate("짧은 요약") == "짧은 요약"


def test_truncate_cuts_long_text_with_ellipsis():
    long_summary = "가" * 150
    result = _truncate(long_summary, limit=100)

    assert len(result) == 101  # 100자 + '…'
    assert result.endswith("…")


def test_format_cards_truncates_supporting_and_counterexample_summaries():
    evidence = ResearchEvidence(
        query="q",
        supporting=[_card("GAME-001", "가" * 200)],
        counterexamples=[_card("GAME-002", "나" * 200)],
    )

    formatted = _format_cards(evidence)

    assert "가" * 101 not in formatted  # 전문이 그대로 들어가지 않는다
    assert "…" in formatted


# ---------------------------------------------------------------------------
# 프롬프트 캐싱 — messages.create 에 넘어가는 system 파라미터 모양
# ---------------------------------------------------------------------------
def _fake_response(json_text: str) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json_text)],
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


@pytest.mark.asyncio
async def test_plan_marks_system_prompt_as_cacheable():
    planner = Planner()
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_response('{"specs": []}'))
        )
    )
    planner._client = fake_client  # 실제 AsyncAnthropic 생성을 건너뛴다

    evidence = ResearchEvidence(query="q", supporting=[_card("GAME-001", "요약")])
    await planner.plan("아이디어", evidence)

    call_kwargs = fake_client.messages.create.call_args.kwargs
    system = call_kwargs["system"]

    assert isinstance(system, list)
    assert system[0]["text"] == SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_add_spec_keeps_shared_prefix_cacheable_and_separate():
    planner = Planner()
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_response('{"specId": "spec-003"}'))
        )
    )
    planner._client = fake_client

    evidence = ResearchEvidence(query="q", supporting=[_card("GAME-001", "요약")])
    await planner.add_spec(
        "이중 점프를 추가한다", evidence, "플랫포머",
        ["청크 로더", "상자 상호작용"], "spec-003",
    )

    call_kwargs = fake_client.messages.create.call_args.kwargs
    system = call_kwargs["system"]
    user_prompt = call_kwargs["messages"][0]["content"]

    assert len(system) == 2
    assert system[0]["text"] == SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]  # 이번 작업 지시문은 캐시하지 않는다
    assert "이번 작업" in system[1]["text"]
    # 기존 기능과 겹치지 않도록 문맥으로 실어 보낸다.
    assert "청크 로더" in user_prompt
    assert "상자 상호작용" in user_prompt
    # 번호는 서버가 정한 값으로 고정 지시한다 — 모델이 고르게 두지 않는다.
    assert "spec-003" in user_prompt


@pytest.mark.asyncio
async def test_revise_spec_keeps_shared_prefix_cacheable_and_separate():
    planner = Planner()
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_response('{"specId": "spec-001"}'))
        )
    )
    planner._client = fake_client

    await planner.revise_spec("spec markdown", "피드백", "developer", "GAME-001")

    call_kwargs = fake_client.messages.create.call_args.kwargs
    system = call_kwargs["system"]

    assert len(system) == 2
    assert system[0]["text"] == SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]  # revise 전용 지시문은 캐시하지 않는다
    assert "이번 작업" in system[1]["text"]
