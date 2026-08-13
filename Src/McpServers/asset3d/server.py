"""Asset3DGenMcpServer: validate and persist provider-neutral 3D requests.

This server deliberately does not select or emulate a 3D provider. Until one
is approved and configured, it returns a request package rather than a 2D
placeholder or a claimed 3D asset.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.errors import VALIDATION_ERROR, tool_error
from common.server import build, expects_dict_return, serve


mcp = build("Asset3DGenMcpServer")
ROOT = Path(os.getenv("ASSET_ROOT", "./var/assets")).resolve()

ASSET_TYPES = frozenset({"character", "slime", "prop", "environment", "building", "interactive"})
GENERATION_METHODS = frozenset(
    {"image_to_3d", "text_to_3d", "manual_blender", "procedural", "existing_asset"}
)
MODEL_FORMATS = frozenset({"fbx", "glb", "gltf"})
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must be a non-empty string")
    return value.strip()


def _require_id(value: Any, field: str) -> str:
    value = _require_text(value, field)
    if not _ID.fullmatch(value):
        raise tool_error(
            VALIDATION_ERROR,
            f"{field} must use lowercase letters, digits, hyphens, or underscores",
        )
    return value


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise tool_error(VALIDATION_ERROR, f"{field} must be an object")
    return value


def _require_text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise tool_error(VALIDATION_ERROR, f"{field} must be a non-empty list")
    return [_require_text(item, field) for item in value]


def _validate_asset_spec(asset_spec: dict[str, Any]) -> dict[str, Any]:
    asset_id = _require_id(asset_spec.get("assetId"), "assetSpec.assetId")
    asset_type = _require_text(asset_spec.get("assetType"), "assetSpec.assetType")
    if asset_type not in ASSET_TYPES:
        raise tool_error(VALIDATION_ERROR, f"assetSpec.assetType must be one of {sorted(ASSET_TYPES)}")

    design = _require_object(asset_spec.get("design"), "assetSpec.design")
    _require_text(design.get("description"), "assetSpec.design.description")
    _require_text(design.get("style"), "assetSpec.design.style")

    output = _require_object(asset_spec.get("output"), "assetSpec.output")
    model_format = _require_text(output.get("format"), "assetSpec.output.format").lower()
    if model_format not in MODEL_FORMATS:
        raise tool_error(VALIDATION_ERROR, f"assetSpec.output.format must be one of {sorted(MODEL_FORMATS)}")
    max_triangles = output.get("maxTriangles")
    if not isinstance(max_triangles, int) or isinstance(max_triangles, bool) or max_triangles < 1:
        raise tool_error(VALIDATION_ERROR, "assetSpec.output.maxTriangles must be a positive integer")
    return {"assetId": asset_id, "assetType": asset_type, "modelFormat": model_format}


def _validate_generation_prompt(prompt: dict[str, Any], asset_id: str) -> None:
    if _require_id(prompt.get("assetId"), "generationPrompt.assetId") != asset_id:
        raise tool_error(VALIDATION_ERROR, "generationPrompt.assetId must match assetSpec.assetId")
    method = _require_text(prompt.get("method"), "generationPrompt.method")
    if method not in GENERATION_METHODS:
        raise tool_error(
            VALIDATION_ERROR,
            f"generationPrompt.method must be one of {sorted(GENERATION_METHODS)}",
        )
    _require_text(prompt.get("prompt"), "generationPrompt.prompt")


def _validate_search_prompt(prompt: dict[str, Any], asset_id: str) -> None:
    if _require_id(prompt.get("assetId"), "referenceSearchPrompt.assetId") != asset_id:
        raise tool_error(VALIDATION_ERROR, "referenceSearchPrompt.assetId must match assetSpec.assetId")
    _require_text_list(prompt.get("queries"), "referenceSearchPrompt.queries")
    _require_text_list(prompt.get("requiredViews"), "referenceSearchPrompt.requiredViews")


def _validate(
    asset_spec: dict[str, Any], generation_prompt: dict[str, Any], reference_search_prompt: dict[str, Any]
) -> dict[str, str]:
    summary = _validate_asset_spec(asset_spec)
    _validate_generation_prompt(generation_prompt, summary["assetId"])
    _validate_search_prompt(reference_search_prompt, summary["assetId"])
    return {
        **summary,
        "sourceSpecSha256": _digest(asset_spec),
        "generationPromptSha256": _digest(generation_prompt),
        "referenceSearchPromptSha256": _digest(reference_search_prompt),
    }


@mcp.tool(description="Validate a provider-neutral 3D asset specification and its two derived prompts.")
@expects_dict_return
def validate_3d_asset_prompts(
    assetSpec: dict[str, Any], generationPrompt: dict[str, Any], referenceSearchPrompt: dict[str, Any]
) -> dict[str, Any]:
    return _validate(assetSpec, generationPrompt, referenceSearchPrompt)


@mcp.tool(
    description=(
        "Validate and save a provider-neutral 3D request package. No provider is configured, so this "
        "never returns a generated model or a 2D placeholder."
    )
)
@expects_dict_return
def prepare_3d_asset_request(
    featureId: str,
    assetSpec: dict[str, Any],
    generationPrompt: dict[str, Any],
    referenceSearchPrompt: dict[str, Any],
    gameId: str | None = None,
) -> dict[str, Any]:
    feature_id = _require_id(featureId, "featureId")
    game_id = _require_id(gameId or os.getenv("ASSET_DEFAULT_GAME_ID", "default"), "gameId")
    provenance = _validate(assetSpec, generationPrompt, referenceSearchPrompt)
    request_id = f"{provenance['assetId']}__{provenance['sourceSpecSha256'][:12]}"
    request_path = ROOT / "3d" / "requests" / game_id / f"{request_id}.json"
    package = {
        "requestId": request_id,
        "gameId": game_id,
        "featureId": feature_id,
        "status": "provider_unconfigured",
        "createdAt": _now(),
        "assetSpec": assetSpec,
        "generationPrompt": generationPrompt,
        "referenceSearchPrompt": referenceSearchPrompt,
        "provenance": provenance,
        "provider": {
            "configured": False,
            "message": "No 3D provider is configured; use this package for approved external generation.",
        },
    }
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "requestId": request_id,
        "requestPath": str(request_path),
        "assetId": provenance["assetId"],
        "assetType": provenance["assetType"],
        "modelFormat": provenance["modelFormat"],
        "status": package["status"],
        "providerConfigured": False,
    }


if __name__ == "__main__":
    serve(mcp)
