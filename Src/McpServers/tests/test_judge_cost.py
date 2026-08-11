"""qa/judge.py 의 프롬프트 캐싱 표시 확인 (실제 API 호출 없음)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

from qa.judge import _BASE_SYSTEM_PROMPT, _Judge  # noqa: E402


def _fake_response(json_text: str) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json_text)],
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


async def test_ask_marks_base_system_prompt_as_cacheable():
    judge = _Judge()
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_fake_response('{"match": true, "missing": []}'))
        )
    )
    judge._client = fake_client

    await judge.ask("판정하라", {"type": "object"}, {"gameDesign": {}})

    call_kwargs = fake_client.messages.create.call_args.kwargs
    system = call_kwargs["system"]

    assert isinstance(system, list)
    assert system[0]["text"] == _BASE_SYSTEM_PROMPT
    assert system[0]["cache_control"] == {"type": "ephemeral"}
