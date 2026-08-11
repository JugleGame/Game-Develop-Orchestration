"""Tests for QaMcpServer.

Driven through a real MCP session (SDK in-memory transport), so the §03
contract — tool names, argument casing, structuredContent, error codes — is
exercised exactly as the orchestrator will exercise it. The Anthropic call
itself is monkeypatched at ``qa.judge._Judge.ask`` so these tests never hit
the network or require ``ANTHROPIC_API_KEY``.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession
from mcp.server.mcpserver.exceptions import ToolError
from mcp import Client

from qa import judge
from qa.server import mcp

_GAME_DESIGN = {
    "game_id": "g1",
    "genre": "platformer",
    "core_mechanics": ["jump", "run"],
    "art_style": "pixel art",
    "target_platform": "PC",
    "structure_overview": "one level",
    "created_at": "2026-07-15T00:00:00Z",
}


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with Client(mcp) as client:
        yield client


_STUB_USAGE = {
    "model": "claude-sonnet-5",
    "input_tokens": 120,
    "output_tokens": 45,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


def _stub_llm(monkeypatch: pytest.MonkeyPatch, reply: dict) -> list[dict]:
    """Replace the model call with a canned reply; return the payloads it was
    called with, for call-shape assertions.

    Patched at ``_Judge.ask`` rather than at the SDK, so the validators and the
    usage plumbing below it still run exactly as in production.
    """

    calls: list[dict] = []

    async def fake(
        self,
        instructions: str,
        schema: dict,
        payload: dict,
        *,
        shared_context: dict | None = None,
    ):
        # ``shared_context`` carries the game design document, which now rides
        # in the cached system prefix instead of the per-call payload. It is
        # recorded alongside ``payload`` so call-shape assertions can see the
        # whole input regardless of which side of the cache breakpoint it sits on.
        calls.append(
            {
                "instructions": instructions,
                "schema": schema,
                "payload": payload,
                "shared_context": shared_context,
            }
        )
        return reply, dict(_STUB_USAGE)

    monkeypatch.setattr(judge._Judge, "ask", fake)
    return calls


# --------------------------------------------------------------------------
# §03 contract
# --------------------------------------------------------------------------


async def test_exposes_every_contract_tool():
    async with session() as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert {
        "establish_qa_policy",
        "verify_prototype_structure",
        "compare_structure",
        "run_functional_verification",
        "generate_error_report",
    } <= names


async def test_contract_tools_use_camel_case_argument_names():
    async with session() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert {"gameDesign"} <= set(tools["establish_qa_policy"].input_schema["properties"])
    assert {"gameDesign", "build"} <= set(
        tools["verify_prototype_structure"].input_schema["properties"]
    )
    assert {"gameDesign", "build"} <= set(tools["compare_structure"].input_schema["properties"])
    assert {"gameDesign", "build", "qaPolicy"} <= set(
        tools["run_functional_verification"].input_schema["properties"]
    )
    assert {"gameDesign", "build", "logs"} <= set(
        tools["generate_error_report"].input_schema["properties"]
    )


async def test_validation_failure_carries_error_code_1000():
    async with session() as client:
        result = await client.call_tool("establish_qa_policy", {"gameDesign": {}})

    assert result.is_error is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 1000' in text


# --------------------------------------------------------------------------
# establish_qa_policy
# --------------------------------------------------------------------------


async def test_establish_qa_policy_returns_structured_qa_policy(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "test_cases": [
                {"case_id": "t-1", "description": "Player can jump", "expected_result": "jumps"}
            ],
            "acceptance_criteria": ["Core movement works"],
        },
    )

    async with session() as client:
        result = await client.call_tool("establish_qa_policy", {"gameDesign": _GAME_DESIGN})

    assert result.is_error is False
    assert result.structured_content is not None
    policy = result.structured_content["qaPolicy"]
    assert policy["test_cases"][0]["case_id"] == "t-1"
    assert policy["acceptance_criteria"] == ["Core movement works"]


async def test_malformed_qa_policy_reply_is_reported_as_unknown_error(monkeypatch):
    _stub_llm(monkeypatch, {"test_cases": "not-a-list", "acceptance_criteria": []})

    async with session() as client:
        result = await client.call_tool("establish_qa_policy", {"gameDesign": _GAME_DESIGN})

    assert result.is_error is True
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert '"errorCode": 5000' in text


# --------------------------------------------------------------------------
# verify_prototype_structure / compare_structure
# --------------------------------------------------------------------------


async def test_verify_prototype_structure_reports_missing_features(monkeypatch):
    calls = _stub_llm(monkeypatch, {"match": False, "missing": ["run"], "structuralDefects": []})

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure",
            {"gameDesign": _GAME_DESIGN, "build": '{"buildId": "b1"}'},
        )

    assert result.structured_content["match"] is False
    assert result.structured_content["missing"] == ["run"]
    assert calls[0]["payload"]["build"] == '{"buildId": "b1"}'


async def test_compare_structure_is_an_alias_of_verify_prototype_structure(monkeypatch):
    _stub_llm(monkeypatch, {"match": True, "missing": [], "structuralDefects": []})

    async with session() as client:
        result = await client.call_tool(
            "compare_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.structured_content["match"] is True
    assert result.structured_content["missing"] == []


async def test_structure_check_reports_wiring_defects_apart_from_missing_features(monkeypatch):
    """코드가 없는 것과 코드는 있는데 안 붙은 것은 다르게 보고돼야 한다.

    둘을 한 목록에 섞으면 개발 AI 가 이미 있는 기능을 다시 만들려 든다 — 재시도
    한 회차를 통째로 낭비하는 오독이다.
    """

    _stub_llm(
        monkeypatch,
        {
            "match": False,
            "missing": [],
            "structuralDefects": ["Spec001 이 어떤 씬·프리팹에도 붙어 있지 않다"],
        },
    )

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.structured_content["match"] is False
    assert result.structured_content["missing"] == []
    assert result.structured_content["structuralDefects"] == [
        "Spec001 이 어떤 씬·프리팹에도 붙어 있지 않다"
    ]


async def test_structure_check_rejects_pass_that_lists_defects(monkeypatch):
    """결함을 나열하면서 match=true 를 내는 것은 판정이 아니라 모순이다.

    그대로 통과시키면 "QA PASS" 기록만 남고 결함은 아무도 보지 않는다 — 이
    저장소에서 실제로 일어난 일이다 (06 문서 §2.1).
    """

    _stub_llm(
        monkeypatch,
        {"match": True, "missing": [], "structuralDefects": ["붙지 않은 MonoBehaviour 5개"]},
    )

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.is_error is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "structuralDefects" in text


async def test_structure_check_requires_build_argument():
    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": ""}
        )

    assert result.is_error is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# run_functional_verification
# --------------------------------------------------------------------------


async def test_functional_verification_pass(monkeypatch):
    _stub_llm(monkeypatch, {"result": "PASS"})

    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
            },
        )

    assert result.structured_content["result"] == "PASS"


async def test_functional_verification_fail_carries_error_report(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "result": "FAIL",
            "errorReport": {
                "error_type": "runtime",
                "message": "NullReferenceException in PlayerController",
                "file": "Assets/Scripts/PlayerController.cs",
                "line": 42,
                "suggested_fix": "Null-check the input reference before use.",
                "related_feature_id": "f-1",
            },
        },
    )

    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
            },
        )

    assert result.structured_content["result"] == "FAIL"
    assert result.structured_content["errorReport"]["error_type"] == "runtime"
    assert result.structured_content["errorReport"]["line"] == 42


async def test_functional_fail_without_error_report_is_rejected(monkeypatch):
    """A FAIL verdict must always carry an errorReport; a bare FAIL is a
    malformed LLM reply, not a valid QA verdict."""

    _stub_llm(monkeypatch, {"result": "FAIL"})

    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
            },
        )

    assert result.is_error is True
    assert '"errorCode": 5000' in "".join(getattr(b, "text", "") for b in result.content)


async def test_invalid_error_type_is_rejected(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "result": "FAIL",
            "errorReport": {
                "error_type": "not-a-real-type",
                "message": "x",
                "suggested_fix": "x",
                "related_feature_id": "",
            },
        },
    )

    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
            },
        )

    assert result.is_error is True
    assert '"errorCode": 5000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# generate_error_report
# --------------------------------------------------------------------------


async def test_generate_error_report_returns_structured_report(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "errorReport": {
                "error_type": "compile",
                "message": "CS0103: name 'foo' does not exist",
                "file": "Assets/Scripts/Foo.cs",
                "line": 10,
                "suggested_fix": "Declare 'foo' before use.",
                "related_feature_id": "f-2",
            }
        },
    )

    async with session() as client:
        result = await client.call_tool(
            "generate_error_report",
            {"gameDesign": _GAME_DESIGN, "build": "{}", "logs": "CS0103 at line 10"},
        )

    assert result.structured_content["errorReport"]["error_type"] == "compile"
    assert result.structured_content["errorReport"]["file"] == "Assets/Scripts/Foo.cs"


async def test_generate_error_report_requires_game_design():
    async with session() as client:
        result = await client.call_tool(
            "generate_error_report", {"gameDesign": {}, "build": "{}", "logs": "x"}
        )

    assert result.is_error is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# LLM output parsing robustness
#
# output_config pins the shape at the API, so these guard the decoder itself
# rather than a shape the model is still free to produce.
# --------------------------------------------------------------------------


def test_markdown_fenced_reply_is_still_parsed():
    """A transport that decorates the reply must not crash the decoder."""

    fenced = '```json\n{"match": true, "missing": []}\n```'
    assert judge._parse_json(fenced) == {"match": True, "missing": []}


def test_non_json_reply_is_reported_as_unknown_error():
    with pytest.raises(ToolError) as excinfo:
        judge._parse_json("I'm sorry, I cannot determine this.")

    assert json.loads(str(excinfo.value))["errorCode"] == 5000


def test_non_object_json_is_rejected():
    with pytest.raises(ToolError) as excinfo:
        judge._parse_json("[1, 2, 3]")

    assert json.loads(str(excinfo.value))["errorCode"] == 5000


# --------------------------------------------------------------------------
# Field-type validation
#
# Each payload below is one the orchestrator's Pydantic models reject with an
# unhandled ValidationError *inside its QA node*, bypassing the §03 error-code
# channel and so the retry/escalation policy. They must fail here instead,
# with a code the orchestrator can map.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "bad_report"),
    [
        ("line as a word", {"line": "unknown"}),
        ("line as a decimal", {"line": 3.7}),
        ("line as a bool", {"line": True}),
        ("file as an int", {"file": 123}),
        ("message as an int", {"message": 7}),
        ("related_feature_id as null", {"related_feature_id": None}),
        ("suggested_fix as a list", {"suggested_fix": ["a"]}),
    ],
)
async def test_bad_error_report_field_types_fail_here_not_in_the_orchestrator(
    monkeypatch, field, bad_report
):
    report = {
        "error_type": "runtime",
        "message": "m",
        "file": None,
        "line": None,
        "suggested_fix": "s",
        "related_feature_id": "f-1",
    }
    report.update(bad_report)
    _stub_llm(monkeypatch, {"result": "FAIL", "errorReport": report})

    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
            },
        )

    assert result.is_error is True, field
    assert '"errorCode": 5000' in "".join(getattr(b, "text", "") for b in result.content), field


async def test_numeric_string_line_is_normalized_to_an_int(monkeypatch):
    """The orchestrator would coerce "42" itself; normalizing here keeps the
    payload's declared type honest."""

    _stub_llm(
        monkeypatch,
        {
            "result": "FAIL",
            "errorReport": {
                "error_type": "compile",
                "message": "m",
                "file": "A.cs",
                "line": "42",
                "suggested_fix": "s",
                "related_feature_id": "f-1",
            },
        },
    )

    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
            },
        )

    assert result.structured_content["errorReport"]["line"] == 42


@pytest.mark.parametrize(
    ("label", "policy"),
    [
        (
            "case_id as an int",
            {
                "test_cases": [{"case_id": 1, "description": "d", "expected_result": "e"}],
                "acceptance_criteria": [],
            },
        ),
        (
            "description as null",
            {
                "test_cases": [{"case_id": "t", "description": None, "expected_result": "e"}],
                "acceptance_criteria": [],
            },
        ),
        ("acceptance_criteria of ints", {"test_cases": [], "acceptance_criteria": [1, 2]}),
        ("test case not an object", {"test_cases": ["t-1"], "acceptance_criteria": []}),
    ],
)
async def test_bad_qa_policy_field_types_fail_here_not_in_the_orchestrator(
    monkeypatch, label, policy
):
    _stub_llm(monkeypatch, policy)

    async with session() as client:
        result = await client.call_tool("establish_qa_policy", {"gameDesign": _GAME_DESIGN})

    assert result.is_error is True, label
    assert '"errorCode": 5000' in "".join(getattr(b, "text", "") for b in result.content), label


async def test_non_string_missing_entry_is_rejected(monkeypatch):
    _stub_llm(monkeypatch, {"match": False, "missing": ["run", 7]})

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.is_error is True
    assert '"errorCode": 5000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# The real model boundary
#
# Every test above stubs _Judge.ask, which skips the request construction and
# the stop_reason guards. These stub the SDK client one level lower so that
# code actually runs.
# --------------------------------------------------------------------------


class _FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeUsage:
    input_tokens = 111
    output_tokens = 22
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason
        self.stop_details = None
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def _stub_client(monkeypatch, text: str, stop_reason: str = "end_turn") -> _FakeClient:
    """Give the judge a fake SDK client.

    Patching ``_ensure_client`` rather than the environment keeps the test from
    depending on whether ANTHROPIC_API_KEY happens to be set on the machine.
    """

    fake = _FakeClient(_FakeResponse(text, stop_reason))
    monkeypatch.setattr(judge._Judge, "_ensure_client", lambda self: fake)
    return fake


async def test_request_is_built_with_a_json_schema_and_adaptive_thinking(monkeypatch):
    """The shape is pinned by the API, not asked for in prose — that is what
    makes the validators a second line of defence rather than the first."""

    fake = _stub_client(monkeypatch, '{"match": true, "missing": []}')

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.is_error is False, result.content
    assert result.structured_content["match"] is True

    sent = fake.messages.kwargs
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"] == judge._STRUCTURE_SCHEMA
    assert sent["thinking"] == {"type": "adaptive"}
    # No assistant prefill any more: the schema does that job.
    assert [m["role"] for m in sent["messages"]] == ["user"]


async def test_usage_from_the_model_is_reported_to_the_orchestrator(monkeypatch):
    """app/utils/usage.py harvests a ``usage`` key from any tool result; without
    it every QA token is invisible in game_jobs.cost_usd."""

    _stub_client(monkeypatch, '{"match": true, "missing": []}')

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    usage = result.structured_content["usage"]
    assert usage["model"] == judge.MODEL
    assert usage["input_tokens"] == 111
    assert usage["output_tokens"] == 22


async def test_reply_truncated_at_max_tokens_is_reported_clearly(monkeypatch):
    _stub_client(monkeypatch, '{"match": true, "missi', stop_reason="max_tokens")

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.is_error is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert '"errorCode": 5000' in text
    assert "max_tokens" in text


async def test_a_refusal_is_reported_rather_than_parsed(monkeypatch):
    _stub_client(monkeypatch, "", stop_reason="refusal")

    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.is_error is True
    assert '"errorCode": 5000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# Path B — Claude Code has no API key by design (CLAUDE.md)
#
# CLAUDE.md requires both execution paths to use the same MCP servers, and
# gives path B no key. Accepting an already-made judgement is what reconciles
# those two facts; it mirrors create_script(contents=...) on the Unity side.
# --------------------------------------------------------------------------


@pytest.fixture
def no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # A client cached by an earlier test would mask the missing key.
    monkeypatch.setattr(judge._judge, "_client", None)


async def test_missing_key_names_the_keyless_alternative(no_api_key):
    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure", {"gameDesign": _GAME_DESIGN, "build": "{}"}
        )

    assert result.is_error is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert '"errorCode": 3000' in text
    assert "ANTHROPIC_API_KEY" in text
    assert "result" in text  # names the argument that avoids needing a key


async def test_supplied_structure_result_needs_no_key(no_api_key):
    async with session() as client:
        result = await client.call_tool(
            "verify_prototype_structure",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "result": {"match": False, "missing": ["run"]},
            },
        )

    assert result.is_error is False, result.content
    # ``structuralDefects`` is optional on the way in — a caller written before
    # the field existed still works — but always present on the way out, so the
    # orchestrator never has to branch on whether the key is there.
    assert result.structured_content == {
        "match": False,
        "missing": ["run"],
        "structuralDefects": [],
    }
    # Nothing was spent, so nothing is reported.
    assert "usage" not in result.structured_content


async def test_supplied_qa_policy_needs_no_key(no_api_key):
    async with session() as client:
        result = await client.call_tool(
            "establish_qa_policy",
            {
                "gameDesign": _GAME_DESIGN,
                "qaPolicy": {
                    "test_cases": [{"case_id": "t-1", "description": "d", "expected_result": "e"}],
                    "acceptance_criteria": ["a"],
                },
            },
        )

    assert result.is_error is False, result.content
    assert result.structured_content["qaPolicy"]["test_cases"][0]["case_id"] == "t-1"


async def test_supplied_verdict_needs_no_key(no_api_key):
    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
                "verdict": {"result": "PASS"},
            },
        )

    assert result.structured_content == {"result": "PASS"}


async def test_supplied_error_report_needs_no_key(no_api_key):
    async with session() as client:
        result = await client.call_tool(
            "generate_error_report",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "logs": "CS0103",
                "errorReport": {
                    "error_type": "compile",
                    "message": "CS0103",
                    "file": "A.cs",
                    "line": 10,
                    "suggested_fix": "declare it",
                    "related_feature_id": "f-1",
                },
            },
        )

    assert result.structured_content["errorReport"]["line"] == 10


async def test_a_supplied_judgement_is_validated_like_a_generated_one(no_api_key):
    """The keyless path must not become a hole in the schema guarantee."""

    async with session() as client:
        result = await client.call_tool(
            "run_functional_verification",
            {
                "gameDesign": _GAME_DESIGN,
                "build": "{}",
                "qaPolicy": {"test_cases": [], "acceptance_criteria": []},
                "verdict": {"result": "FAIL"},  # FAIL with no errorReport
            },
        )

    assert result.is_error is True
    assert '"errorCode": 5000' in "".join(getattr(b, "text", "") for b in result.content)


def test_request_timeout_stays_under_the_orchestrator_qa_budget():
    """§03 §5 gives QA 180s. If this server's own LLM timeout were higher, the
    orchestrator would give up first while this server kept burning tokens."""

    assert judge._timeout_seconds() < 180.0
