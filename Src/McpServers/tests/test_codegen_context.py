"""CodeGen 의 캐시 접두사 분할과 수정(repair) 경로 검증.

프롬프트 캐싱은 최소 접두사에 미달하면 **조용히** 실패한다 (claude-opus-5 는
512토큰). 오류도 경고도 없이 ``cache_creation_input_tokens: 0`` 이 남을 뿐이라
코드 리뷰를 그냥 통과하는 종류의 퇴행이다. 그래서 주석을 믿지 않고 요청 모양을
직접 고정한다.

여기서 지키는 성질은 두 가지다.

* 한 게임 안에서 **변하지 않는 것**(규칙 + 청사진 요약)만 breakpoint 앞에 온다.
* **호출마다 달라지는 것**(이미 만든 타입 목록, 수정 지시, 이전 소스)은 전부
  그 뒤에 온다 — 앞에 섞이면 매 호출이 캐시 쓰기가 되어 절감이 사라진다.
"""

from __future__ import annotations

from typing import Any

import pytest

from unity.codegen import SYSTEM_PROMPT, ScriptGenerator, _estimate_tokens

# 실제 청사진(Games/game-post-apoc-critters/blueprint.json)에서 뽑은 모양.
# 길이가 이 테스트의 요점이라 임의로 줄이면 안 된다 — 접두사가 512토큰에
# 못 미치면 breakpoint 를 붙이지 않는 것이 올바른 동작이기 때문이다.
_CONTEXT = (
    "Genre: 도트 그래픽 2D 오픈월드 생존 탐험 (샌드박스)\n"
    "Art style: 32x32 픽셀 아트. 배경 폐허는 채도를 낮춘 회녹색 팔레트로 깔고, "
    "동물 캐릭터에만 높은 채도를 써서 톤을 분리한다. 귀여움은 캐릭터 실루엣과 "
    "색에만 싣고 세계 자체는 황폐하게 유지한다.\n"
    "Target platform: PC\n"
    "Core mechanics this game implements:\n"
    "- 플레이어는 동물 캐릭터를 8방향으로 조작해, 청크 단위로 이어붙는 폐허 "
    "월드를 경계 없이 탐험한다.\n"
    "- 폐허에 흩어진 자원 세 종류(고철·씨앗·천)를 주워 인벤토리에 종류별 "
    "수량으로 쌓는다.\n"
    "- 낮밤이 정해진 주기로 교대하며, 밤에는 화면 밝기가 내려가고 플레이어 "
    "주변만 원형으로 남는다.\n"
    "- 무너진 관람차·기울어진 송전탑 같은 랜드마크가 멀리서 보여, 지도 마커 "
    "없이 다음 목적지를 정하게 한다.\n"
    "- 구조한 동물 동료 한 마리가 플레이어를 따라다니며 주변 자원 노드를 "
    "느낌표로 표시한다.\n"
    "World structure: 중앙 거점에서 사방으로 무한히 확장되는 청크 기반 2D "
    "월드. 플레이어 주변 3x3 청크만 활성 상태로 유지되고 나머지는 해제된다.\n"
    "Every script below belongs to this one game."
)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _Reply("namespace Game.Gameplay { public class F1 : MonoBehaviour { } }")


class _Reply:
    stop_reason = "end_turn"
    usage = None

    class _Block:
        type = "text"

        def __init__(self, text: str) -> None:
            self.text = text

    def __init__(self, text: str) -> None:
        self.content = [self._Block(text)]


@pytest.fixture
def generator(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptGenerator, _Recorder]:
    rec = _Recorder()

    class _Client:
        messages = rec

    gen = ScriptGenerator(namespace="Game.Gameplay", script_root="Assets/Scripts")
    monkeypatch.setattr(gen, "_client", _Client())
    monkeypatch.setattr(gen, "_ensure_client", lambda: _Client())
    return gen, rec


def _system_text(rec: _Recorder) -> str:
    return "".join(block["text"] for block in rec.calls[-1]["system"])


def _cached_text(rec: _Recorder) -> str:
    """breakpoint 를 포함해 그 앞까지 — 실제로 캐시되는 구간."""

    blocks = rec.calls[-1]["system"]
    cached: list[str] = []
    for block in blocks:
        cached.append(block["text"])
        if "cache_control" in block:
            break
    return "".join(cached)


def _user_text(rec: _Recorder) -> str:
    return rec.calls[-1]["messages"][0]["content"]


# ---------------------------------------------------------------------------
# 토큰 추정 — breakpoint 를 붙일지 말지를 이 값이 정한다
# ---------------------------------------------------------------------------
def test_system_prompt_alone_cannot_be_cached() -> None:
    """이 전제가 깨지면(규칙이 길어지면) 아래 설계 전체를 다시 봐야 한다."""

    assert _estimate_tokens(SYSTEM_PROMPT) < 512


def test_estimator_accounts_for_korean_being_token_dense() -> None:
    """문자 수만 세면 판정이 뒤집힌다 — 청사진 요약은 한국어로 오는 일이 많다."""

    korean = "가" * 300
    english = "a" * 300

    assert _estimate_tokens(korean) > _estimate_tokens(english) * 2


def test_realistic_blueprint_context_crosses_the_minimum() -> None:
    assert _estimate_tokens(SYSTEM_PROMPT + _CONTEXT) >= 512


# ---------------------------------------------------------------------------
# 캐시 접두사
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_project_context_forms_its_own_cached_block(generator) -> None:
    gen, rec = generator
    await gen.generate("f-1", "Add a jump.", project_context=_CONTEXT)

    blocks = rec.calls[-1]["system"]
    assert len(blocks) == 2
    assert "cache_control" not in blocks[0], "규칙만으로는 512토큰에 못 미친다"
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}
    assert "낮밤이 정해진 주기로" in blocks[-1]["text"]


@pytest.mark.asyncio
async def test_no_breakpoint_without_context(generator) -> None:
    """접두사가 최소치에 미달하면 breakpoint 는 무시된다. 붙여두면 절감했다는
    착시만 남으므로 아예 붙이지 않는다."""

    gen, rec = generator
    await gen.generate("f-1", "Add a jump.")

    blocks = rec.calls[-1]["system"]
    assert len(blocks) == 1
    assert "cache_control" not in blocks[0]


@pytest.mark.asyncio
async def test_no_breakpoint_when_context_is_too_short(generator) -> None:
    gen, rec = generator
    await gen.generate("f-1", "Add a jump.", project_context="Genre: platformer")

    assert all("cache_control" not in block for block in rec.calls[-1]["system"])


@pytest.mark.asyncio
async def test_prefix_is_byte_identical_across_features_of_one_game(generator) -> None:
    """절감 전체가 이 성질에 걸려 있다: 같은 바이트가 아니면 캐시는 안 맞는다."""

    gen, rec = generator
    await gen.generate("f-1", "Add a jump.", project_context=_CONTEXT)
    await gen.generate("f-2", "Add a dash.", project_context=_CONTEXT)
    await gen.generate("f-3", "Add crafting.", project_context=_CONTEXT)

    prefixes = {
        "".join(
            block["text"]
            for block in call["system"][
                : 1 + next(i for i, b in enumerate(call["system"]) if "cache_control" in b)
            ]
        )
        for call in rec.calls
    }
    assert len(rec.calls) == 3
    assert len(prefixes) == 1


# ---------------------------------------------------------------------------
# 변동 구간은 breakpoint 뒤에
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_existing_types_stay_out_of_the_cached_prefix(generator) -> None:
    """호출마다 늘어나는 목록이다 — 접두사에 넣으면 매번 캐시가 깨진다."""

    gen, rec = generator
    await gen.generate(
        "f-2",
        "Add a dash.",
        project_context=_CONTEXT,
        existing_types=["PlayerController (Assets/Scripts/PlayerController.cs)"],
    )

    assert "PlayerController" not in _cached_text(rec)
    assert "PlayerController" in _user_text(rec)


@pytest.mark.asyncio
async def test_repair_rules_stay_out_of_the_cached_prefix(generator) -> None:
    """재시도에서만 붙는 지시문. 접두사에 들어가면 첫 호출과 재시도가 서로의
    캐시를 깬다."""

    gen, rec = generator
    await gen.generate(
        "f-1",
        "Fix CS1002.",
        project_context=_CONTEXT,
        previous_source="public class F1 { }",
    )

    assert "Repair rules" not in _cached_text(rec)
    assert "Repair rules" in _system_text(rec)


@pytest.mark.asyncio
async def test_previous_source_stays_out_of_the_cached_prefix(generator) -> None:
    gen, rec = generator
    await gen.generate(
        "f-1",
        "Fix CS1002.",
        project_context=_CONTEXT,
        previous_source="public class Marker { }",
    )

    assert "Marker" not in _cached_text(rec)
    assert "Marker" in _user_text(rec)


# ---------------------------------------------------------------------------
# 수정 경로
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_previous_source_switches_the_framing_to_repair(generator) -> None:
    """백지 재작성이 아니라 수정이어야 한다 — 재작성은 고쳐진 곳 옆을 새로
    깨뜨릴 수 있고, 매 라운드가 사실상 무작위 재추첨이 된다."""

    gen, rec = generator
    await gen.generate(
        "f-1",
        "CS1002: ; expected",
        previous_source="public class F1 { void A() }",
    )

    user_turn = _user_text(rec)
    assert "Current contents of this file:" in user_turn
    assert "Fix this file so the following is resolved:" in user_turn
    assert "Implement this feature:" not in user_turn


@pytest.mark.asyncio
async def test_first_pass_uses_the_implement_framing(generator) -> None:
    gen, rec = generator
    await gen.generate("f-1", "Add a jump.")

    user_turn = _user_text(rec)
    assert "Implement this feature:" in user_turn
    assert "Current contents of this file:" not in user_turn


@pytest.mark.asyncio
async def test_repair_instructs_preserving_untouched_lines(generator) -> None:
    gen, rec = generator
    await gen.generate("f-1", "fix it", previous_source="public class F1 { }")

    system = _system_text(rec)
    assert "smallest change" in system
    assert "COMPLETE corrected file" in system
