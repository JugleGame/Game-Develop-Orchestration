"""Bounded JSON Schemas used as the contract between fresh Codex phases."""

from __future__ import annotations

from typing import Any


VISUAL_DIMENSIONS = ("2D", "3D", "hybrid")
MAX_RESULT_BYTES = 32 * 1024
MAX_CONTEXT_BYTES = 64 * 1024


def phase_result_schema(phase: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "phase": {"const": phase},
        "status": {"const": "completed"},
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
                "visualDimension": {"enum": list(VISUAL_DIMENSIONS)},
                "assetsRequired": {"type": "boolean"},
            }
        )
        required.extend(["visualDimension", "assetsRequired"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
