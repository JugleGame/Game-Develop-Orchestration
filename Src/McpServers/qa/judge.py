"""LLM-backed QA judgment engine for QaMcpServer.

Implements the QA AI role from ``Doc/설계/04_Prompt_Specification.md``: a QA
Engineer that compares a GameDesignDocument against a built prototype, flags
missing features, and analyzes compile/runtime errors — always as
schema-exact JSON, never prose or markdown.

Structured to match ``strategic/planner.py`` and ``unity/codegen.py``, so all
three LLM-using servers behave the same way under the repository's two
execution paths (``CLAUDE.md``):

* **Lazy client, explicit failure.** ``_ensure_client`` builds the client on
  first use and, when ``ANTHROPIC_API_KEY`` is absent, fails with a message
  naming the keyless alternative rather than a bare auth error. The key is
  read from the environment by the SDK itself, so the credential never enters
  this call frame, is never a parameter, and is never logged.

* **Schema enforced by the API, not by asking.** Every call passes a JSON
  Schema through ``output_config``, so the model cannot return a shape the
  orchestrator's Pydantic models would reject. The validators below stay as a
  second line of defence — they are what turns a bad payload into a §03
  ``errorCode`` instead of an unhandled ``ValidationError`` inside the
  orchestrator's QA node.

* **Token cost is reported.** Each judgment returns a ``usage`` object built
  by ``common.usage.usage_of``; ``server.py`` puts it in the tool result and
  ``app/utils/usage.py`` folds it into ``game_jobs.cost_usd``. A server that
  does LLM work and reports nothing makes that spend invisible.

* **A keyless path exists.** Path B (Claude Code) has no API key by design,
  yet ``CLAUDE.md`` requires both paths to use the same MCP servers. Every
  judgment function therefore accepts an already-formed result and, given one,
  validates and returns it without calling the model — the same escape hatch
  ``create_script(contents=...)`` provides on the Unity side. Validation still
  runs, so both paths emit byte-identical artifacts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from common.errors import MCP_ERROR, TIMEOUT, UNKNOWN, tool_error
from common.usage import usage_of

logger = logging.getLogger("QaMcpServer.judge")

# §02 §7.5 puts QA on a "balanced" model, between Strategic's high-order
# reasoning and Unity's code specialisation. Priced in app/utils/usage.py, so
# QA spend is attributable rather than silently zero.
MODEL = os.getenv("QA_MODEL", "claude-sonnet-5")
DEFAULT_MAX_TOKENS = 4096

# Must stay under the orchestrator's 180 s QA timeout (§03 §5) so this server
# is the side that fails, with a §03 error code the orchestrator can map.
DEFAULT_TIMEOUT_SECONDS = 150.0

_ERROR_TYPES = ("compile", "runtime", "logic", "structure_mismatch")

_BASE_SYSTEM_PROMPT = """You are the QA AI in an AI game generation pipeline \
(role defined in Doc/설계/04_Prompt_Specification.md).

Role: QA Engineer.
Task: compare the game design document against the built prototype, detect \
missing features, and analyze compile/runtime errors.

Judgement rules:
- Never guess facts not present in the input. If the evidence is insufficient \
to confirm a feature works, treat it as missing or failing rather than \
assuming success.
- The `build` reference is opaque build metadata produced by UnityMcpServer. \
It may carry a build id, a file list, compile errors, or feature markers. \
Absence of evidence in it is not evidence of success.
- When `build` carries a `runtimeCheck` object, it is a real playmode pass: \
`runtimeCheck.errors` is the actual console error list from running the game, \
not an inference. A non-empty `runtimeCheck.errors` is positive evidence of a \
runtime failure, not merely inconclusive evidence."""

# ---------------------------------------------------------------------------
# Output schemas — enforced by the API, mirrored by the validators below.
# ---------------------------------------------------------------------------

_ERROR_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "error_type": {"type": "string", "enum": list(_ERROR_TYPES)},
        "message": {"type": "string", "description": "무엇이 잘못됐는지 한 문장"},
        "file": {"type": ["string", "null"], "description": "파일 경로 또는 null"},
        "line": {"type": ["integer", "null"], "description": "정수 행 번호 또는 null"},
        "suggested_fix": {"type": "string", "description": "구체적인 수정 방법"},
        "related_feature_id": {"type": "string", "description": "원인이 된 feature id"},
    },
    "required": ["error_type", "message", "file", "line", "suggested_fix", "related_feature_id"],
    "additionalProperties": False,
}

_POLICY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "description": "core_mechanics 항목마다 최소 한 개.",
            "items": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "description": {"type": "string"},
                    "expected_result": {"type": "string"},
                },
                "required": ["case_id", "description", "expected_result"],
                "additionalProperties": False,
            },
        },
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["test_cases", "acceptance_criteria"],
    "additionalProperties": False,
}

_STRUCTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "match": {
            "type": "boolean",
            "description": "기획의 모든 기능이 확인되고 구조 결함도 없으면 true",
        },
        "missing": {
            "type": "array",
            "items": {"type": "string"},
            "description": "build 에서 근거를 찾지 못한 core_mechanics 항목",
        },
        # 기능 커버리지와 별도로 담는다. 둘을 한 리스트에 섞으면 "기능이 없다"와
        # "기능은 있는데 씬에 안 붙었다"가 구분되지 않아, 개발 AI 가 이미 있는
        # 코드를 다시 만들려 든다 — 재시도 한 회차를 통째로 낭비하는 오독이다.
        "structuralDefects": {
            "type": "array",
            "items": {"type": "string"},
            "description": "코드는 있으나 씬·프리팹 구조가 잘못된 지점",
        },
    },
    "required": ["match", "missing", "structuralDefects"],
    "additionalProperties": False,
}

_FUNCTIONAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "enum": ["PASS", "FAIL"]},
        "errorReport": {
            "anyOf": [_ERROR_REPORT_SCHEMA, {"type": "null"}],
            "description": "FAIL 이면 반드시 채운다. PASS 면 null.",
        },
    },
    "required": ["result", "errorReport"],
    "additionalProperties": False,
}

_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"errorReport": _ERROR_REPORT_SCHEMA},
    "required": ["errorReport"],
    "additionalProperties": False,
}

_POLICY_INSTRUCTIONS = """Produce a QAPolicy for this game design.

Derive at least one test case per entry in core_mechanics, so every mechanic \
in the design has a corresponding test case. Acceptance criteria must be \
observable facts, not subjective adjectives."""

_STRUCTURE_INSTRUCTIONS = """Judge the prototype on two separate axes.

1. COVERAGE — does the build show evidence of every entry in core_mechanics?
   `missing` lists the design elements the build reference gives no evidence of.

2. STRUCTURE — is the code actually wired into the game? A Unity script that
   exists as a file but is attached to no GameObject never runs, so a mechanic
   can be "implemented" and still be absent at runtime. Put each such defect in
   `structuralDefects`, naming the script or object involved:
   - a MonoBehaviour that no scene or prefab references
   - a scene object the design calls for that the build has no sign of
   - a script that builds its own GameObjects at runtime (new GameObject +
     AddComponent) instead of being placed in a scene or prefab
   - a generated sprite that nothing references

Set match=true only when BOTH lists are empty. Absence of evidence is not \
evidence of correctness: if the build reference does not let you confirm \
something, report it rather than assuming it works."""

_FUNCTIONAL_INSTRUCTIONS = """Judge whether the prototype passes the QA policy.

Fail the build if the build reference shows compile errors, if a test case's \
expected_result has no supporting evidence, or if an acceptance criterion is \
unmet. On FAIL, fill errorReport with the most specific error_type that \
explains the failure. On PASS, set errorReport to null."""

_REPORT_INSTRUCTIONS = """Produce one ExecutionErrorReport describing the most \
significant problem found in the logs.

If the logs show no error, use error_type="runtime", message="No errors \
detected.", file=null, line=null, suggested_fix="None required.", \
related_feature_id=""."""


class JudgementError(RuntimeError):
    """QA 판정 실패 — 서버 계층에서 §03 코드로 변환된다."""


# ---------------------------------------------------------------------------
# Model access
# ---------------------------------------------------------------------------


def _timeout_seconds() -> float:
    raw = os.getenv("QA_LLM_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "QA_LLM_TIMEOUT_SECONDS is not a number; using the default",
            extra={"fallback_seconds": DEFAULT_TIMEOUT_SECONDS},
        )
        return DEFAULT_TIMEOUT_SECONDS


def _max_tokens() -> int:
    raw = os.getenv("QA_LLM_MAX_TOKENS")
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "QA_LLM_MAX_TOKENS is not an integer; using the default",
            extra={"fallback_max_tokens": DEFAULT_MAX_TOKENS},
        )
        return DEFAULT_MAX_TOKENS


class _Judge:
    """Holds the Anthropic client so it is built once, and only if used."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model or MODEL
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise JudgementError(
                    "ANTHROPIC_API_KEY 가 없어 QA 판정을 생성할 수 없습니다. "
                    "키를 설정하거나, 도구 호출 시 이미 만들어 둔 판정을 직접 "
                    "넘기세요 (establish_qa_policy 는 qaPolicy, 구조 비교는 "
                    "result, 기능 검증은 verdict, 오류 보고는 errorReport)."
                )
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(timeout=_timeout_seconds())
        return self._client

    async def ask(
        self,
        instructions: str,
        schema: dict[str, Any],
        payload: dict[str, Any],
        *,
        shared_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one judgment and return ``(decoded_json, usage)``.

        ``shared_context`` is the part of the input that is byte-identical
        across the four judgments of one QA pass — in practice the game design
        document. It goes into the cached system prefix; ``payload`` carries
        only what differs per judgment and stays uncached after the breakpoint.

        Splitting them is what makes the cache do anything at all.
        ``_BASE_SYSTEM_PROMPT`` is ~165 tokens, and the minimum cacheable
        prefix on claude-sonnet-5 is 1024 — a breakpoint on it alone is
        silently ignored (no error, ``cache_creation_input_tokens: 0``).
        Folding the design document in clears the minimum *and* moves the
        expensive repeated bytes behind the breakpoint, which is where the
        saving actually was.
        """

        client = self._ensure_client()
        body = json.dumps(payload, ensure_ascii=False)
        started = time.monotonic()

        # Caching is a prefix match on exact bytes, so this prefix must
        # serialize identically on every call: sort_keys removes any dependence
        # on dict insertion order.
        system: list[dict[str, Any]] = [{"type": "text", "text": _BASE_SYSTEM_PROMPT}]
        if shared_context:
            system.append(
                {
                    "type": "text",
                    "text": "[Game design document — shared by every judgment in this pass]\n"
                    + json.dumps(shared_context, ensure_ascii=False, sort_keys=True),
                }
            )
        # Breakpoint on the last stable block: it caches everything before it.
        system[-1]["cache_control"] = {"type": "ephemeral"}

        try:
            response = await client.messages.create(
                model=self._model,
                max_tokens=_max_tokens(),
                system=system,
                messages=[{"role": "user", "content": f"{instructions}\n\n{body}"}],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "high",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except Exception as exc:  # noqa: BLE001 — mapped to §03 codes below
            elapsed = round(time.monotonic() - started, 2)
            if _is_timeout(exc):
                logger.error("QA LLM timed out", extra={"model": self._model, "elapsed_s": elapsed})
                raise tool_error(TIMEOUT, f"QA LLM timed out: {exc}") from exc
            logger.error(
                "QA LLM request failed", extra={"model": self._model, "elapsed_s": elapsed}
            )
            raise tool_error(MCP_ERROR, f"QA LLM request failed: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            logger.error("QA LLM refused", extra={"model": self._model})
            raise tool_error(
                UNKNOWN, f"QA 판정이 거부되었습니다: {getattr(response, 'stop_details', None)}"
            )
        if getattr(response, "stop_reason", None) == "max_tokens":
            logger.error("QA LLM reply hit max_tokens", extra={"model": self._model})
            raise tool_error(
                UNKNOWN, "QA LLM reply was truncated at max_tokens; raise QA_LLM_MAX_TOKENS"
            )

        text = "".join(block.text for block in response.content if block.type == "text")
        logger.info(
            "QA LLM call succeeded",
            extra={
                "model": self._model,
                "elapsed_s": round(time.monotonic() - started, 2),
                "request_chars": len(body),
                "reply_chars": len(text),
            },
        )
        return _parse_json(text), usage_of(response, self._model)


def _is_timeout(exc: BaseException) -> bool:
    """Recognise a timeout without importing anthropic at module import time.

    The SDK is imported lazily so the server starts without it configured;
    matching on the class name keeps that property.
    """

    if isinstance(exc, TimeoutError):
        return True
    return any(base.__name__ == "APITimeoutError" for base in type(exc).__mro__)


_judge = _Judge()


def _parse_json(text: str) -> dict[str, Any]:
    """Decode the model's reply, tolerating a stray markdown fence.

    ``output_config`` already constrains the shape, so this is a guard against
    a transport that hands back decorated text rather than a real failure mode.
    """

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            head, rest = stripped.split("\n", 1)
            if head.strip().lower() in {"json", ""}:
                stripped = rest
    try:
        decoded = json.loads(stripped)
    except ValueError as exc:
        raise tool_error(UNKNOWN, f"QA LLM returned non-JSON output: {exc}") from exc
    if not isinstance(decoded, dict):
        raise tool_error(UNKNOWN, "QA LLM must return a JSON object")
    return decoded


# ---------------------------------------------------------------------------
# Response validation
#
# Each helper rejects exactly the shapes that would otherwise raise a
# ValidationError inside the orchestrator (verified against its Pydantic
# models). ``bool`` is excluded from integer fields on purpose: it is an
# ``int`` subclass in Python, so ``line: true`` would otherwise pass as 1.
#
# These run on model output *and* on results supplied by a keyless caller, so
# both execution paths emit the same shape.
# ---------------------------------------------------------------------------


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise tool_error(
            UNKNOWN, f"QA 결과의 {field!r} 는 문자열이어야 합니다 (받은 값: {type(value).__name__})"
        )
    return value


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise tool_error(
            UNKNOWN,
            f"QA 결과의 {field!r} 는 문자열이거나 null이어야 합니다 "
            f"(받은 값: {type(value).__name__})",
        )
    return value


def _optional_line(value: Any) -> int | None:
    """Normalize ``line`` to an int or None, rejecting anything ambiguous."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise tool_error(UNKNOWN, "QA 결과의 'line' 은 정수이거나 null이어야 합니다 (bool 불가)")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise tool_error(
        UNKNOWN, f"QA 결과의 'line' 은 정수이거나 null이어야 합니다 (받은 값: {value!r})"
    )


def validate_qa_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise tool_error(UNKNOWN, "qaPolicy 는 JSON 객체여야 합니다")
    test_cases = policy.get("test_cases")
    acceptance = policy.get("acceptance_criteria")
    if not isinstance(test_cases, list) or not isinstance(acceptance, list):
        raise tool_error(UNKNOWN, "QA 정책의 형태가 올바르지 않습니다")

    validated_cases: list[dict[str, str]] = []
    for index, case in enumerate(test_cases):
        if not isinstance(case, dict):
            raise tool_error(UNKNOWN, f"test_cases[{index}] 가 JSON 객체가 아닙니다")
        validated_cases.append(
            {
                "case_id": _require_str(case.get("case_id"), f"test_cases[{index}].case_id"),
                "description": _require_str(
                    case.get("description"), f"test_cases[{index}].description"
                ),
                "expected_result": _require_str(
                    case.get("expected_result"), f"test_cases[{index}].expected_result"
                ),
            }
        )

    validated_criteria = [
        _require_str(item, f"acceptance_criteria[{index}]") for index, item in enumerate(acceptance)
    ]
    return {"test_cases": validated_cases, "acceptance_criteria": validated_criteria}


def validate_structure_result(result: Any) -> dict[str, Any]:
    """구조 비교 결과를 검증한다. 모델이 낸 것이든 사람이 넘긴 것이든 같은 규칙이다.

    ``structuralDefects`` 는 **선택**이다 — 이 필드가 생기기 전에 저장된 결과와
    아직 갱신되지 않은 호출자를 받아 주기 위해서다. 빠지면 빈 목록으로 읽는다.

    반면 ``match`` 와 두 목록의 **일관성은 강제**한다. 결함을 나열하면서
    ``match=true`` 를 내는 것은 판정이 아니라 모순이고, 그대로 통과시키면
    "QA PASS" 라는 기록만 남고 결함은 아무도 보지 않는다 — 이 저장소에서 실제로
    일어난 일이다 (06 문서 §2.1).
    """

    if not isinstance(result, dict):
        raise tool_error(UNKNOWN, "구조 비교 결과는 JSON 객체여야 합니다")
    match = result.get("match")
    missing = result.get("missing")
    defects = result.get("structuralDefects", [])
    if not isinstance(match, bool) or not isinstance(missing, list) or not isinstance(defects, list):
        raise tool_error(UNKNOWN, "구조 비교 결과의 형태가 올바르지 않습니다")

    validated_missing = [_require_str(item, f"missing[{i}]") for i, item in enumerate(missing)]
    validated_defects = [
        _require_str(item, f"structuralDefects[{i}]") for i, item in enumerate(defects)
    ]

    if match and (validated_missing or validated_defects):
        raise tool_error(
            UNKNOWN,
            "match=true 인데 missing 또는 structuralDefects 가 비어 있지 않습니다 — "
            f"missing={validated_missing}, structuralDefects={validated_defects}",
        )

    return {
        "match": match,
        "missing": validated_missing,
        "structuralDefects": validated_defects,
    }


def validate_error_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise tool_error(UNKNOWN, "errorReport 는 JSON 객체여야 합니다")
    if report.get("error_type") not in _ERROR_TYPES:
        raise tool_error(
            UNKNOWN, f"errorReport 의 error_type 이 올바르지 않습니다: {report.get('error_type')!r}"
        )
    return {
        "error_type": report["error_type"],
        "message": _require_str(report.get("message"), "errorReport.message"),
        "file": _optional_str(report.get("file"), "errorReport.file"),
        "line": _optional_line(report.get("line")),
        "suggested_fix": _require_str(report.get("suggested_fix"), "errorReport.suggested_fix"),
        "related_feature_id": _require_str(
            report.get("related_feature_id"), "errorReport.related_feature_id"
        ),
    }


def validate_functional_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise tool_error(UNKNOWN, "기능 검증 결과는 JSON 객체여야 합니다")
    verdict = result.get("result")
    if verdict not in {"PASS", "FAIL"}:
        raise tool_error(UNKNOWN, f"기능 검증 결과가 올바르지 않습니다: {verdict!r}")
    if verdict == "PASS":
        return {"result": "PASS"}
    # A FAIL with no report gives the orchestrator nothing to feed back into
    # CodeGen, so it is rejected rather than passed along half-formed.
    return {"result": "FAIL", "errorReport": validate_error_report(result.get("errorReport"))}


# ---------------------------------------------------------------------------
# Judgment entry points
#
# Each returns ``(payload, usage)``. ``usage`` is None on the keyless path,
# which spends nothing.
# ---------------------------------------------------------------------------


async def establish_qa_policy(
    game_design: dict[str, Any], supplied: Any = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if supplied is not None:
        return {"qaPolicy": validate_qa_policy(supplied)}, None

    raw, usage = await _judge.ask(
        _POLICY_INSTRUCTIONS, _POLICY_SCHEMA, {}, shared_context=game_design
    )
    policy = validate_qa_policy(raw)
    logger.info(
        "QA policy established",
        extra={
            "game_id": game_design.get("game_id"),
            "test_cases": len(policy["test_cases"]),
            "acceptance_criteria": len(policy["acceptance_criteria"]),
        },
    )
    return {"qaPolicy": policy}, usage


async def verify_prototype_structure(
    game_design: dict[str, Any], build: str, supplied: Any = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if supplied is not None:
        return validate_structure_result(supplied), None

    raw, usage = await _judge.ask(
        _STRUCTURE_INSTRUCTIONS, _STRUCTURE_SCHEMA, {"build": build}, shared_context=game_design
    )
    validated = validate_structure_result(raw)
    logger.info(
        "Structure check complete",
        extra={
            "game_id": game_design.get("game_id"),
            "match": validated["match"],
            "missing_count": len(validated["missing"]),
            "structural_defect_count": len(validated["structuralDefects"]),
        },
    )
    return validated, usage


async def run_functional_verification(
    game_design: dict[str, Any], build: str, qa_policy: dict[str, Any], supplied: Any = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if supplied is not None:
        return validate_functional_result(supplied), None

    raw, usage = await _judge.ask(
        _FUNCTIONAL_INSTRUCTIONS,
        _FUNCTIONAL_SCHEMA,
        {"build": build, "qaPolicy": qa_policy},
        shared_context=game_design,
    )
    validated = validate_functional_result(raw)
    logger.info(
        "Functional verification complete",
        extra={"game_id": game_design.get("game_id"), "result": validated["result"]},
    )
    return validated, usage


async def generate_error_report(
    game_design: dict[str, Any], build: str, logs: str, supplied: Any = None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if supplied is not None:
        return {"errorReport": validate_error_report(supplied)}, None

    raw, usage = await _judge.ask(
        _REPORT_INSTRUCTIONS,
        _REPORT_SCHEMA,
        {"build": build, "logs": logs},
        shared_context=game_design,
    )
    if "errorReport" not in raw:
        raise tool_error(UNKNOWN, "QA LLM 응답에 'errorReport' 가 없습니다")
    report = validate_error_report(raw["errorReport"])
    logger.info(
        "Error report generated",
        extra={
            "game_id": game_design.get("game_id"),
            "error_type": report["error_type"],
            "log_chars": len(logs),
        },
    )
    return {"errorReport": report}, usage
