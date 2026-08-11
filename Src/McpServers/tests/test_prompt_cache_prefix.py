"""The QA cache breakpoint must sit behind something big enough to cache.

Prompt caching fails *silently* when the prefix is under the model's minimum
(1024 tokens on claude-sonnet-5): no error, no warning, just
``cache_creation_input_tokens: 0``. That makes it the kind of regression that
survives code review, so these tests assert the request shape directly instead
of trusting the comment above it.

They also pin the two properties the prefix match depends on: the cached bytes
must be identical across the four judgments of one pass, and they must not
carry anything that varies per judgment.
"""

import json
from typing import Any

import pytest

from qa import judge

GAME_DESIGN: dict[str, Any] = {
    "game_id": "g-1",
    "genre": "2D open world",
    "core_mechanics": ["exploration", "crafting", "day-night cycle"],
    "art_style": "pixel",
    "target_platform": "PC",
    "structure_overview": "overworld with three biomes",
}


_MINIMAL_ERROR_REPORT = {
    "error_type": "runtime",
    "message": "No errors detected.",
    "file": None,
    "line": None,
    "suggested_fix": "None required.",
    "related_feature_id": "",
}


def _minimal_valid_reply(schema: dict[str, Any]) -> str:
    """Smallest response satisfying ``schema``.

    Each judgment enforces a different schema and validates the reply against
    it, so a single canned answer cannot drive all four. Keying off the
    requested schema keeps the stub honest without duplicating the shapes.
    """

    properties = set(schema.get("properties", {}))
    if "test_cases" in properties:
        return json.dumps({"test_cases": [], "acceptance_criteria": []})
    if "match" in properties:
        return json.dumps({"match": True, "missing": []})
    if "result" in properties:
        return json.dumps({"result": "PASS", "errorReport": None})
    return json.dumps({"errorReport": _MINIMAL_ERROR_REPORT})


class _Recorder:
    """Captures the kwargs of every ``messages.create`` call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        schema = kwargs["output_config"]["format"]["schema"]
        return _Reply(_minimal_valid_reply(schema))


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
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    class _Client:
        messages = rec

    monkeypatch.setattr(judge._judge, "_client", _Client())
    return rec


def _system_blocks(rec: _Recorder) -> list[dict[str, Any]]:
    return rec.calls[-1]["system"]


@pytest.mark.asyncio
async def test_breakpoint_is_on_the_last_block_not_the_tiny_prompt(recorder: _Recorder) -> None:
    """A breakpoint on ``_BASE_SYSTEM_PROMPT`` alone would never cache."""

    await judge.verify_prototype_structure(GAME_DESIGN, "build-ref-1")

    blocks = _system_blocks(recorder)
    assert len(blocks) == 2, "the design document must form its own system block"
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_design_document_is_in_the_cached_prefix(recorder: _Recorder) -> None:
    await judge.verify_prototype_structure(GAME_DESIGN, "build-ref-1")

    cached = "".join(block["text"] for block in _system_blocks(recorder))
    assert "day-night cycle" in cached, "design doc belongs before the breakpoint"


@pytest.mark.asyncio
async def test_per_judgment_input_stays_out_of_the_cached_prefix(recorder: _Recorder) -> None:
    """Anything that varies per call must sit after the breakpoint."""

    await judge.verify_prototype_structure(GAME_DESIGN, "build-ref-1")

    cached = "".join(block["text"] for block in _system_blocks(recorder))
    user_turn = recorder.calls[-1]["messages"][0]["content"]
    assert "build-ref-1" not in cached
    assert "build-ref-1" in user_turn


@pytest.mark.asyncio
async def test_prefix_is_byte_identical_across_the_four_judgments(recorder: _Recorder) -> None:
    """The whole saving depends on this: same bytes, or no cache hit."""

    policy = {"test_cases": [], "acceptance_criteria": []}
    await judge.establish_qa_policy(GAME_DESIGN)
    await judge.verify_prototype_structure(GAME_DESIGN, "build-ref-1")
    await judge.run_functional_verification(GAME_DESIGN, "build-ref-1", policy)
    await judge.generate_error_report(GAME_DESIGN, "build-ref-1", "some logs")

    prefixes = {
        "".join(block["text"] for block in call["system"]) for call in recorder.calls
    }
    assert len(recorder.calls) == 4
    assert len(prefixes) == 1, "cached prefix differs between judgments; cache will never hit"


@pytest.mark.asyncio
async def test_prefix_survives_dict_reordering(recorder: _Recorder) -> None:
    """Key order must not change the bytes — hence ``sort_keys``."""

    reordered = dict(reversed(list(GAME_DESIGN.items())))
    await judge.verify_prototype_structure(GAME_DESIGN, "b1")
    await judge.verify_prototype_structure(reordered, "b1")

    first, second = (
        "".join(block["text"] for block in call["system"]) for call in recorder.calls
    )
    assert first == second
