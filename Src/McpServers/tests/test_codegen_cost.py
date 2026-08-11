"""unity/codegen.py 의 프롬프트 캐싱 표시 확인 (실제 API 호출 없음)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

from unity.codegen import SYSTEM_PROMPT, ScriptGenerator  # noqa: E402


def _fake_response(csharp_text: str) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=csharp_text)],
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


# claude-opus-5 의 최소 캐시 접두사. 이보다 짧으면 cache_control 은 오류 없이
# 무시되고 cache_creation_input_tokens 가 0 으로 남는다.
_OPUS_5_MIN_CACHEABLE_TOKENS = 512

# 영문 프롬프트의 보수적 토큰 추정(문자수/4). 실측이 아니라, 프롬프트가
# 임계값 근처까지 커졌을 때 이 테스트가 먼저 알려주게 하는 용도다.
_ESTIMATED_TOKENS_PER_CHAR = 1 / 4


async def test_generate_does_not_claim_a_cache_it_cannot_get():
    """접두사가 최소치 미달이면 breakpoint 를 붙이지 않는다.

    붙여도 오류가 나지 않기 때문에, 붙여둔 채로 두면 '캐싱했다'는 착시만
    남고 비용은 그대로다. 캐싱은 조용히 실패하므로 요청 형태로 고정한다.
    """

    estimated_tokens = len(SYSTEM_PROMPT) * _ESTIMATED_TOKENS_PER_CHAR
    assert estimated_tokens < _OPUS_5_MIN_CACHEABLE_TOKENS, (
        "SYSTEM_PROMPT 가 최소 캐시 길이에 근접했습니다. count_tokens 로 실측한 뒤 "
        "512 토큰을 넘었다면 cache_control 을 다시 넣고 이 테스트를 뒤집으세요."
    )

    generator = ScriptGenerator()
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_fake_response("public class Foo : MonoBehaviour {}")
            )
        )
    )
    generator._client = fake_client

    await generator.generate("spec-001", "간단한 이동 컴포넌트")

    system = fake_client.messages.create.call_args.kwargs["system"]

    assert isinstance(system, list)
    assert system[0]["text"] == SYSTEM_PROMPT
    assert "cache_control" not in system[0]
