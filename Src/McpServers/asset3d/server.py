"""Provider-neutral 3D prompt validation and request handoff."""

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

ASSET_TYPES = frozenset(
    {"character", "slime", "monster", "prop", "environment", "building", "interactive"}
)
GENERATION_METHODS = frozenset(
    {"image_to_3d", "text_to_3d", "manual_blender", "procedural", "existing_asset"}
)
MODEL_FORMATS = frozenset({"fbx", "glb", "gltf"})
REFERENCE_VIEWS = ("front", "side", "back")
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


def _require_text_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise tool_error(VALIDATION_ERROR, f"{field} must be {qualifier}")
    return [_require_text(item, field) for item in value]


def _optional_text_list(parent: dict[str, Any], field: str) -> list[str]:
    value = parent.get(field, [])
    return _require_text_list(value, f"assetSpec.design.{field}", allow_empty=True)


def _validate_asset_spec(asset_spec: dict[str, Any]) -> dict[str, Any]:
    asset_id = _require_id(asset_spec.get("assetId"), "assetSpec.assetId")
    asset_name = _require_text(asset_spec.get("assetName"), "assetSpec.assetName")
    asset_type = _require_text(asset_spec.get("assetType"), "assetSpec.assetType")
    if asset_type not in ASSET_TYPES:
        raise tool_error(VALIDATION_ERROR, f"assetSpec.assetType must be one of {sorted(ASSET_TYPES)}")
    gameplay_role = _require_text(asset_spec.get("gameplayRole"), "assetSpec.gameplayRole")

    design = _require_object(asset_spec.get("design"), "assetSpec.design")
    description = _require_text(design.get("description"), "assetSpec.design.description")
    style = _require_text(design.get("style"), "assetSpec.design.style")
    proportions = _require_text(design.get("proportions"), "assetSpec.design.proportions")
    colors = _require_text_list(design.get("colors"), "assetSpec.design.colors")
    materials = _optional_text_list(design, "materials")
    preserve = _optional_text_list(design, "preserve")
    exclude = _optional_text_list(design, "exclude")

    geometry = _require_object(asset_spec.get("geometry"), "assetSpec.geometry")
    max_triangles = geometry.get("maxTriangles")
    if not isinstance(max_triangles, int) or isinstance(max_triangles, bool) or max_triangles < 1:
        raise tool_error(VALIDATION_ERROR, "assetSpec.geometry.maxTriangles must be a positive integer")
    separate_meshes = _require_text_list(
        geometry.get("separateMeshes"), "assetSpec.geometry.separateMeshes", allow_empty=True
    )

    output = _require_object(asset_spec.get("output"), "assetSpec.output")
    model_format = _require_text(output.get("format"), "assetSpec.output.format").lower()
    if model_format not in MODEL_FORMATS:
        raise tool_error(VALIDATION_ERROR, f"assetSpec.output.format must be one of {sorted(MODEL_FORMATS)}")
    scale = output.get("scale")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise tool_error(VALIDATION_ERROR, "assetSpec.output.scale must be a positive number")
    pivot = _require_text(output.get("pivot"), "assetSpec.output.pivot")
    collider = _require_text(output.get("collider"), "assetSpec.output.collider")

    animation = _require_object(asset_spec.get("animation"), "assetSpec.animation")
    animation_required = animation.get("required")
    if not isinstance(animation_required, bool):
        raise tool_error(VALIDATION_ERROR, "assetSpec.animation.required must be a boolean")
    rig_type = ""
    clips: list[str] = []
    if animation_required:
        rig_type = _require_text(animation.get("rigType"), "assetSpec.animation.rigType")
        clips = _require_text_list(animation.get("clips"), "assetSpec.animation.clips")

    generation = _require_object(asset_spec.get("generation"), "assetSpec.generation")
    method = _require_text(generation.get("method"), "assetSpec.generation.method")
    if method not in GENERATION_METHODS:
        raise tool_error(
            VALIDATION_ERROR,
            f"assetSpec.generation.method must be one of {sorted(GENERATION_METHODS)}",
        )

    validation = _require_object(asset_spec.get("validation"), "assetSpec.validation")
    requirements = _require_text_list(
        validation.get("requirements"), "assetSpec.validation.requirements"
    )
    return {
        "assetId": asset_id,
        "assetName": asset_name,
        "assetType": asset_type,
        "gameplayRole": gameplay_role,
        "description": description,
        "style": style,
        "proportions": proportions,
        "colors": colors,
        "materials": materials,
        "preserve": preserve,
        "exclude": exclude,
        "maxTriangles": max_triangles,
        "separateMeshes": separate_meshes,
        "modelFormat": model_format,
        "scale": scale,
        "pivot": pivot,
        "collider": collider,
        "animationRequired": animation_required,
        "rigType": rig_type,
        "clips": clips,
        "method": method,
        "validationRequirements": requirements,
    }


def _compose_prompts(asset_spec: dict[str, Any]) -> dict[str, Any]:
    spec = _validate_asset_spec(asset_spec)
    clauses = [
        f"Create {spec['assetName']}, a {spec['assetType']} used for {spec['gameplayRole']}.",
        spec["description"],
        f"Use {spec['style']} style with {spec['proportions']} proportions.",
        f"Colors: {', '.join(spec['colors'])}.",
    ]
    if spec["materials"]:
        clauses.append(f"Materials: {', '.join(spec['materials'])}.")
    if spec["assetType"] in {"character", "slime", "monster"} or spec["animationRequired"]:
        clauses.append("Use a neutral pose with unobstructed parts and animation-ready deformation.")
    clauses.append(
        f"Keep the mesh at or below {spec['maxTriangles']} triangles and export {spec['modelFormat']}."
    )
    if spec["separateMeshes"]:
        clauses.append(f"Separate meshes: {', '.join(spec['separateMeshes'])}.")
    clauses.append(
        f"Use scale {spec['scale']}, pivot {spec['pivot']}, and collider plan {spec['collider']}."
    )
    if spec["animationRequired"]:
        clauses.append(
            f"Prepare a {spec['rigType']} rig for clips: {', '.join(spec['clips'])}."
        )
    if spec["preserve"]:
        clauses.append(f"Preserve: {', '.join(spec['preserve'])}.")
    if spec["exclude"]:
        clauses.append(f"Exclude: {', '.join(spec['exclude'])}.")

    spec_digest = _digest(asset_spec)
    references_required = spec["method"] == "image_to_3d" or spec["assetType"] == "character"
    views = list(REFERENCE_VIEWS) if references_required else []
    reference_base = (
        f"{spec['assetName']}, {spec['description']} {spec['style']} style, "
        f"{spec['proportions']} proportions, colors {', '.join(spec['colors'])}"
    )
    return {
        "generationPrompt": {
            "assetId": spec["assetId"],
            "method": spec["method"],
            "sourceSpecSha256": spec_digest,
            "prompt": " ".join(clauses),
            "validationRequirements": spec["validationRequirements"],
        },
        "referenceSearchPrompt": {
            "assetId": spec["assetId"],
            "sourceSpecSha256": spec_digest,
            "required": references_required,
            "queries": (
                [
                    f"{spec['assetName']} {spec['style']} 3D turnaround reference",
                    f"{spec['assetName']} {spec['proportions']} proportions material reference",
                ]
                if references_required
                else []
            ),
            "requiredViews": views,
            "viewPrompts": {
                view: (
                    f"{reference_base}, {view} orthographic view, neutral pose, unobstructed parts, "
                    "plain background, consistent lighting, minimal perspective distortion"
                )
                for view in views
            },
        },
    }


def _validate(
    asset_spec: dict[str, Any], generation_prompt: dict[str, Any], reference_search_prompt: dict[str, Any]
) -> dict[str, str]:
    expected = _compose_prompts(asset_spec)
    if generation_prompt != expected["generationPrompt"]:
        raise tool_error(
            VALIDATION_ERROR,
            "generationPrompt does not match the prompt derived from assetSpec",
        )
    if reference_search_prompt != expected["referenceSearchPrompt"]:
        raise tool_error(
            VALIDATION_ERROR,
            "referenceSearchPrompt does not match the prompt derived from assetSpec",
        )
    spec = _validate_asset_spec(asset_spec)
    return {
        "assetId": spec["assetId"],
        "assetType": spec["assetType"],
        "modelFormat": spec["modelFormat"],
        "sourceSpecSha256": _digest(asset_spec),
        "generationPromptSha256": _digest(generation_prompt),
        "referenceSearchPromptSha256": _digest(reference_search_prompt),
    }


@mcp.tool(description="Compose provider-neutral generation and reference prompts from an asset spec.")
@expects_dict_return
def compose_3d_asset_prompts(assetSpec: dict[str, Any]) -> dict[str, Any]:
    return _compose_prompts(assetSpec)


@mcp.tool(description="Validate a 3D asset specification and its deterministically derived prompts.")
@expects_dict_return
def validate_3d_asset_prompts(
    assetSpec: dict[str, Any], generationPrompt: dict[str, Any], referenceSearchPrompt: dict[str, Any]
) -> dict[str, Any]:
    return _validate(assetSpec, generationPrompt, referenceSearchPrompt)


@mcp.tool(
    description=(
        "Compose, validate, and save a provider-neutral 3D request package. No provider is "
        "configured, so this never returns a generated model or a 2D placeholder."
    )
)
@expects_dict_return
def prepare_3d_asset_request(
    featureId: str,
    assetSpec: dict[str, Any],
    gameId: str | None = None,
) -> dict[str, Any]:
    feature_id = _require_id(featureId, "featureId")
    game_id = _require_id(gameId or os.getenv("ASSET_DEFAULT_GAME_ID", "default"), "gameId")
    prompts = _compose_prompts(assetSpec)
    provenance = _validate(
        assetSpec, prompts["generationPrompt"], prompts["referenceSearchPrompt"]
    )
    request_id = f"{provenance['assetId']}__{provenance['sourceSpecSha256'][:12]}"
    request_path = ROOT / "3d" / "requests" / game_id / f"{request_id}.json"
    package = {
        "requestId": request_id,
        "gameId": game_id,
        "featureId": feature_id,
        "status": "provider_unconfigured",
        "createdAt": _now(),
        "assetSpec": assetSpec,
        **prompts,
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
