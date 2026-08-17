"""Bounded contracts shared by fresh Issue Work Runner phases."""

from __future__ import annotations

from typing import Any

MAX_RESULT_BYTES = 48 * 1024
MAX_CONTEXT_BYTES = 96 * 1024
MAX_ISSUE_BODY_BYTES = 64 * 1024


def _string(max_length: int = 4000) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": max_length}


def _items(max_items: int = 64) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": max_items,
        "items": _string(),
    }


def _evidence(*, include_passed: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "id": _string(100),
        "evidence": _string(4000),
        "artifactPaths": {
            "type": "array",
            "maxItems": 16,
            "items": _string(500),
        },
    }
    required = ["id", "evidence", "artifactPaths"]
    if include_passed:
        properties["passed"] = {"type": "boolean"}
        required.append("passed")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def phase_result_schema(phase: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "phase": {"const": phase},
        "status": {"const": "completed"},
        "summary": _string(2000),
        "artifactPaths": {
            "type": "array",
            "maxItems": 32,
            "items": _string(500),
        },
    }
    required = ["phase", "status", "summary", "artifactPaths"]

    if phase == "analysis":
        common.update(
            {
                "objective": _string(8000),
                "scope": _items(),
                "outOfScope": _items(),
                "acceptanceCriteria": _items(),
                "tests": _items(),
                "implementationPlan": _items(),
            }
        )
        required.extend(
            [
                "objective",
                "scope",
                "outOfScope",
                "acceptanceCriteria",
                "tests",
                "implementationPlan",
            ]
        )
    elif phase == "implementation":
        common.update(
            {
                "changedFiles": {
                    "type": "array",
                    "maxItems": 256,
                    "items": _string(500),
                },
                "implementationNotes": _items(),
            }
        )
        required.extend(["changedFiles", "implementationNotes"])
    elif phase == "verification":
        common.update(
            {
                "testEvidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": _evidence(include_passed=True),
                },
                "allTestsPassed": {"type": "boolean"},
            }
        )
        required.extend(["testEvidence", "allTestsPassed"])
    elif phase == "review":
        common.update(
            {
                "verdict": {"enum": ["PASS", "FAIL"]},
                "changedFiles": {
                    "type": "array",
                    "maxItems": 256,
                    "items": _string(500),
                },
                "scopeViolations": {
                    "type": "array",
                    "maxItems": 64,
                    "items": _string(4000),
                },
                "acceptanceCriteriaEvidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": _evidence(include_passed=True),
                },
                "testEvidence": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 64,
                    "items": _evidence(include_passed=True),
                },
                "findings": {
                    "type": "array",
                    "maxItems": 64,
                    "items": _string(4000),
                },
            }
        )
        required.extend(
            [
                "verdict",
                "changedFiles",
                "scopeViolations",
                "acceptanceCriteriaEvidence",
                "testEvidence",
                "findings",
            ]
        )
    else:
        raise ValueError(f"unknown issue phase: {phase}")

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": common,
        "required": required,
        "additionalProperties": False,
    }
