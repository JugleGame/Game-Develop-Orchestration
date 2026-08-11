"""QaMcpServer — LLM-backed structural and functional QA for Unity prototypes.

Implements the five §03 contract tools. Verdicts come from an Anthropic model
prompted per the QA AI role in ``04_Prompt_Specification.md`` (QA Engineer;
schema-exact JSON) — see ``judge.py`` for the prompts, the output schemas, the
request timeout, and the validation that keeps malformed output from reaching
the orchestrator as an unhandled error.

**Two execution paths** (``CLAUDE.md``). Path A is the LangGraph orchestrator
with ``ANTHROPIC_API_KEY`` set; the model produces the verdict here. Path B is
Claude Code, which has no key by design — it judges with the ``qa-review``
skill and hands the finished verdict to the same tool through the optional
argument on each one (``qaPolicy`` / ``result`` / ``verdict`` / ``errorReport``).
Either way the payload goes through the same validation, so both paths emit the
same artifacts. This mirrors ``create_script(contents=...)`` on the Unity side.

Those optional arguments sit *beside* the §03 signature and are never
required, so a caller written to the bare contract is unaffected and
``verify_contract.py`` still passes.

Return type is ``dict[str, Any]``: a bare ``dict`` leaves ``structuredContent``
empty; see ``common/server.py``.

Logging: each tool logs entry and failure with the game id and the §03 error
code (CLAUDE.md requires logging on every MCP path; §7.4 requires it per job).
Prompt bodies and credentials are never logged — see ``judge.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error
from common.server import build as build_server
from common.server import expects_dict_return, serve

from . import judge
from .judge import JudgementError

mcp = build_server("QaMcpServer", 9103)

logger = logging.getLogger("QaMcpServer")


def _require_dict(value: dict[str, Any] | None, field: str) -> dict[str, Any]:
    if not value:
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value


def _require_str(value: str, field: str) -> str:
    if not value or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value


def _log_failure(tool: str, game_design: dict[str, Any] | None, exc: ToolError) -> None:
    """Record a tool failure together with the §03 code the caller will see.

    ``tool_error`` encodes the code as a JSON body in the message, so it is
    parsed back out here rather than logged as an opaque string.
    """

    code: Any = None
    try:
        code = json.loads(str(exc)).get("errorCode")
    except ValueError:
        pass
    logger.error(
        "QA tool failed",
        extra={"tool": tool, "game_id": (game_design or {}).get("game_id"), "error_code": code},
    )


def _result(payload: dict[str, Any], usage: dict[str, Any] | None) -> dict[str, Any]:
    """Attach token usage so the orchestrator can attribute the spend.

    ``app/utils/usage.py`` harvests a ``usage`` key from any tool result. The
    keyless path spends nothing and reports nothing, which that side accounts
    as zero rather than as missing data.
    """

    if usage is not None:
        return {**payload, "usage": usage}
    return payload


@mcp.tool(
    description=(
        "Derive a QA policy (test cases + acceptance criteria) from the game design "
        "document. Pass qaPolicy to record an already-written policy without calling the model."
    )
)
@expects_dict_return
async def establish_qa_policy(
    gameDesign: dict[str, Any], qaPolicy: dict[str, Any] | None = None
) -> dict[str, Any]:
    logger.info("establish_qa_policy called", extra={"game_id": (gameDesign or {}).get("game_id")})
    try:
        game_design = _require_dict(gameDesign, "gameDesign")
        payload, usage = await judge.establish_qa_policy(game_design, qaPolicy)
        return _result(payload, usage)
    except JudgementError as exc:
        error = tool_error(MCP_ERROR, str(exc))
        _log_failure("establish_qa_policy", gameDesign, error)
        raise error from exc
    except ToolError as exc:
        _log_failure("establish_qa_policy", gameDesign, exc)
        raise


@mcp.tool(
    description=(
        "Check whether the built prototype's structure covers every feature in the game "
        "design. Pass result to record an already-made comparison without calling the model."
    )
)
@expects_dict_return
async def verify_prototype_structure(
    gameDesign: dict[str, Any], build: str, result: dict[str, Any] | None = None
) -> dict[str, Any]:
    logger.info(
        "verify_prototype_structure called", extra={"game_id": (gameDesign or {}).get("game_id")}
    )
    try:
        game_design = _require_dict(gameDesign, "gameDesign")
        build_ref = _require_str(build, "build")
        payload, usage = await judge.verify_prototype_structure(game_design, build_ref, result)
        return _result(payload, usage)
    except JudgementError as exc:
        error = tool_error(MCP_ERROR, str(exc))
        _log_failure("verify_prototype_structure", gameDesign, error)
        raise error from exc
    except ToolError as exc:
        _log_failure("verify_prototype_structure", gameDesign, exc)
        raise


@mcp.tool(
    description=(
        "Diff the prototype structure against the design document "
        "(alias of verify_prototype_structure)."
    )
)
@expects_dict_return
async def compare_structure(
    gameDesign: dict[str, Any], build: str, result: dict[str, Any] | None = None
) -> dict[str, Any]:
    logger.info("compare_structure called", extra={"game_id": (gameDesign or {}).get("game_id")})
    try:
        game_design = _require_dict(gameDesign, "gameDesign")
        build_ref = _require_str(build, "build")
        payload, usage = await judge.verify_prototype_structure(game_design, build_ref, result)
        return _result(payload, usage)
    except JudgementError as exc:
        error = tool_error(MCP_ERROR, str(exc))
        _log_failure("compare_structure", gameDesign, error)
        raise error from exc
    except ToolError as exc:
        _log_failure("compare_structure", gameDesign, exc)
        raise


@mcp.tool(
    description=(
        "Run the QA policy's test cases against the build and return a PASS/FAIL verdict. "
        "Pass verdict to record an already-made judgement without calling the model."
    )
)
@expects_dict_return
async def run_functional_verification(
    gameDesign: dict[str, Any],
    build: str,
    qaPolicy: dict[str, Any],
    verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logger.info(
        "run_functional_verification called", extra={"game_id": (gameDesign or {}).get("game_id")}
    )
    try:
        game_design = _require_dict(gameDesign, "gameDesign")
        build_ref = _require_str(build, "build")
        policy = _require_dict(qaPolicy, "qaPolicy")
        payload, usage = await judge.run_functional_verification(
            game_design, build_ref, policy, verdict
        )
        return _result(payload, usage)
    except JudgementError as exc:
        error = tool_error(MCP_ERROR, str(exc))
        _log_failure("run_functional_verification", gameDesign, error)
        raise error from exc
    except ToolError as exc:
        _log_failure("run_functional_verification", gameDesign, exc)
        raise


@mcp.tool(
    description=(
        "Turn raw execution logs into a structured ExecutionErrorReport. Pass errorReport "
        "to record an already-written report without calling the model."
    )
)
@expects_dict_return
async def generate_error_report(
    gameDesign: dict[str, Any],
    build: str,
    logs: str,
    errorReport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logger.info(
        "generate_error_report called", extra={"game_id": (gameDesign or {}).get("game_id")}
    )
    try:
        game_design = _require_dict(gameDesign, "gameDesign")
        build_ref = _require_str(build, "build")
        payload, usage = await judge.generate_error_report(
            game_design, build_ref, logs or "", errorReport
        )
        return _result(payload, usage)
    except JudgementError as exc:
        error = tool_error(MCP_ERROR, str(exc))
        _log_failure("generate_error_report", gameDesign, error)
        raise error from exc
    except ToolError as exc:
        _log_failure("generate_error_report", gameDesign, exc)
        raise


if __name__ == "__main__":
    serve(mcp)
