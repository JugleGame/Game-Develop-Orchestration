"""Bounded JSON Schemas used as the contract between fresh Codex phases."""

from __future__ import annotations

from typing import Any


VISUAL_DIMENSIONS = ("2D", "3D", "hybrid")
QA_PHASES = ("unity_implementation", "unity_integration")
QA_STATUSES = ("PASS", "INCOMPLETE", "INFRA_ERROR", "TEST_FAIL", "FLAKY", "PRODUCT_FAIL")
MAX_RESULT_BYTES = 32 * 1024
MAX_CONTEXT_BYTES = 64 * 1024


def phase_result_schema(phase: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        # Codex structured outputs require every property schema to declare its
        # JSON type, even when ``const`` or ``enum`` already implies it.
        "phase": {"type": "string", "const": phase},
        "status": {"type": "string", "const": "completed"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "handoff": {"type": "string", "maxLength": 8000},
        "artifactPaths": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    }
    required = ["phase", "status", "summary", "handoff", "artifactPaths"]
    if phase == "planning":
        properties.update(
            {
                "visualDimension": {
                    "type": "string",
                    "enum": list(VISUAL_DIMENSIONS),
                },
                "assetsRequired": {"type": "boolean"},
            }
        )
        required.extend(["visualDimension", "assetsRequired"])
    if phase in QA_PHASES:
        properties["qaStatus"] = {"type": "string", "enum": list(QA_STATUSES)}
        required.append("qaStatus")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def codex_phase_result_schema(phase: str) -> dict[str, Any]:
    """Return the strict-output subset accepted by Codex.

    Length and item-count bounds remain in :func:`phase_result_schema` and are
    enforced after execution. Structured Outputs accepts only a JSON Schema
    subset, so transport must not include those validation-only keywords.
    """

    schema = phase_result_schema(phase)

    def compatible(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: compatible(item)
                for key, item in value.items()
                if key not in {"minLength", "maxLength", "maxItems"}
            }
        if isinstance(value, list):
            return [compatible(item) for item in value]
        return value

    return compatible(schema)
