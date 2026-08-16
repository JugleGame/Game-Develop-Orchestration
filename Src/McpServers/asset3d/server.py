"""Provider-neutral 3D prompt validation and request handoff."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageChops

from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error
from common.server import build, expects_dict_return, serve

from . import meshy_client


mcp = build("Asset3DGenMcpServer")
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_UNITY_PROJECT = os.getenv("UNITY_PROJECT_PATH", "").strip()
_DEFAULT_ROOT = Path(
    os.getenv(
        "ASSET3D_RUN_ROOT",
        str(Path(_UNITY_PROJECT) / ".asset3d-staging") if _UNITY_PROJECT else "./var/assets",
    )
).resolve()
ROOT = _DEFAULT_ROOT

ASSET_TYPES = frozenset(
    {"character", "slime", "monster", "prop", "environment", "building", "interactive"}
)
GENERATION_METHODS = frozenset(
    {"image_to_3d", "manual_blender", "procedural", "existing_asset"}
)
MODEL_FORMATS = frozenset({"fbx", "glb", "gltf"})
REFERENCE_VIEWS = ("front", "side", "back")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
MESHY_FORMATS = frozenset({"glb", "fbx"})
MESHY_PROMPT_LIMIT = 600
RETEXTURE_FORMAT = "fbx"
WEBGL_TRIANGLE_CEILING = 15_000
#: Providers return an "unused" map as near-black rather than exactly black.
EMPTY_MAP_THRESHOLD = 2
BLENDER_SCRIPT = Path(__file__).with_name("blender_cleanup.py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _claim_submission(path: Path, request_id: str) -> dict[str, Any] | None:
    """Atomically reserve one paid-provider submission key.

    A plain ``is_file`` check leaves two MCP processes free to pay for the same
    request.  The reservation is deliberately retained after an interrupted
    provider call: without a provider idempotency key it is safer to require
    recovery than to risk charging for a second task.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "submission_state_unreadable"}
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(
            {"requestId": request_id, "status": "SUBMITTING", "createdAt": _now()},
            stream,
            ensure_ascii=False,
        )
        stream.flush()
        os.fsync(stream.fileno())
    return None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _require_external_runtime_root() -> Path:
    root = ROOT
    # Tests replace the module root with an isolated temporary directory. The
    # production value remains _DEFAULT_ROOT and is always subject to the
    # repository/Unity-workspace boundary checks below.
    if root != _DEFAULT_ROOT:
        return root
    configured = os.getenv("ASSET3D_RUN_ROOT", "").strip()
    if root == _DEFAULT_ROOT and configured and not Path(configured).is_absolute():
        raise tool_error(VALIDATION_ERROR, "ASSET3D_RUN_ROOT must be an absolute path")
    if _is_within(root, REPOSITORY_ROOT):
        raise tool_error(
            VALIDATION_ERROR,
            "3D runtime root must be outside the orchestration repository",
        )
    unity_value = os.getenv("UNITY_PROJECT_PATH", "").strip()
    if unity_value:
        unity_root = Path(unity_value)
        if not unity_root.is_absolute():
            raise tool_error(VALIDATION_ERROR, "UNITY_PROJECT_PATH must be an absolute path")
        assets_root = unity_root.resolve() / "Assets"
        if not _is_within(root, unity_root) or _is_within(root, assets_root):
            raise tool_error(
                VALIDATION_ERROR,
                "3D runtime root must be inside the external Unity workspace and outside Assets",
            )
    return root


async def _await_result(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


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


def _webgl_triangle_ceiling() -> int:
    """Per-asset triangle ceiling the WebGL build target accepts."""
    configured = os.getenv("ASSET3D_WEBGL_MAX_TRIANGLES", "").strip()
    if not configured:
        return WEBGL_TRIANGLE_CEILING
    if not configured.isdigit() or int(configured) < 100:
        raise tool_error(
            VALIDATION_ERROR,
            "ASSET3D_WEBGL_MAX_TRIANGLES must be an integer of at least 100",
        )
    return int(configured)


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
    form = _require_object(design.get("form"), "assetSpec.design.form")
    silhouette = _require_text(form.get("silhouette"), "assetSpec.design.form.silhouette")
    primary_volumes = _require_text_list(
        form.get("primaryVolumes"), "assetSpec.design.form.primaryVolumes"
    )
    part_relationships = _require_text_list(
        form.get("partRelationships"), "assetSpec.design.form.partRelationships"
    )
    surface_features = _require_text_list(
        form.get("surfaceFeatures"), "assetSpec.design.form.surfaceFeatures"
    )
    bevel_policy = _require_text_list(
        form.get("bevelPolicy"), "assetSpec.design.form.bevelPolicy"
    )
    materials = _optional_text_list(design, "materials")
    preserve = _optional_text_list(design, "preserve")
    exclude = _optional_text_list(design, "exclude")

    geometry = _require_object(asset_spec.get("geometry"), "assetSpec.geometry")
    max_triangles = geometry.get("maxTriangles")
    if not isinstance(max_triangles, int) or isinstance(max_triangles, bool) or max_triangles < 1:
        raise tool_error(VALIDATION_ERROR, "assetSpec.geometry.maxTriangles must be a positive integer")
    ceiling = _webgl_triangle_ceiling()
    if max_triangles > ceiling:
        raise tool_error(
            VALIDATION_ERROR,
            f"assetSpec.geometry.maxTriangles must not exceed the WebGL budget {ceiling}",
        )
    separate_meshes = _require_text_list(
        geometry.get("separateMeshes"), "assetSpec.geometry.separateMeshes", allow_empty=True
    )
    shading = _require_object(geometry.get("shading"), "assetSpec.geometry.shading")
    normal_policy = _require_text(
        shading.get("normalPolicy"), "assetSpec.geometry.shading.normalPolicy"
    )
    if normal_policy not in {"explicit_hard", "explicit_smooth", "mixed"}:
        raise tool_error(
            VALIDATION_ERROR,
            "assetSpec.geometry.shading.normalPolicy must be one of ['explicit_hard', 'explicit_smooth', 'mixed']",
        )
    face_orientation = _require_text(
        shading.get("faceOrientation"), "assetSpec.geometry.shading.faceOrientation"
    )
    if face_orientation != "outward":
        raise tool_error(
            VALIDATION_ERROR, "assetSpec.geometry.shading.faceOrientation must be outward"
        )
    smoothing_rules = _require_text_list(
        shading.get("smoothingRules"), "assetSpec.geometry.shading.smoothingRules"
    )
    integrity = _require_object(geometry.get("integrity"), "assetSpec.geometry.integrity")
    integrity_flags = {}
    for field in (
        "forbidDegenerateFaces",
        "forbidDuplicateFaces",
        "forbidCoplanarOverlaps",
        "preserveHardEdgeSplits",
    ):
        value = integrity.get(field)
        if value is not True:
            raise tool_error(
                VALIDATION_ERROR, f"assetSpec.geometry.integrity.{field} must be true"
            )
        integrity_flags[field] = value

    output = _require_object(asset_spec.get("output"), "assetSpec.output")
    model_format = _require_text(output.get("format"), "assetSpec.output.format").lower()
    if model_format not in MODEL_FORMATS:
        raise tool_error(VALIDATION_ERROR, f"assetSpec.output.format must be one of {sorted(MODEL_FORMATS)}")
    scale = output.get("scale")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        raise tool_error(VALIDATION_ERROR, "assetSpec.output.scale must be a positive number")
    pivot = _require_text(output.get("pivot"), "assetSpec.output.pivot")
    pivot_policy = _require_text(output.get("pivotPolicy"), "assetSpec.output.pivotPolicy")
    if pivot_policy not in {"ground_center", "center", "root", "custom"}:
        raise tool_error(
            VALIDATION_ERROR,
            "assetSpec.output.pivotPolicy must be one of ['center', 'custom', 'ground_center', 'root']",
        )
    collider = _require_text(output.get("collider"), "assetSpec.output.collider")

    texture = _require_object(asset_spec.get("texture"), "assetSpec.texture")
    texture_required = texture.get("required")
    if not isinstance(texture_required, bool):
        raise tool_error(VALIDATION_ERROR, "assetSpec.texture.required must be a boolean")
    texture_description = _require_text(texture.get("description"), "assetSpec.texture.description")
    texture_maps = _require_text_list(
        texture.get("maps"), "assetSpec.texture.maps", allow_empty=True
    )
    surface_details = _require_text_list(
        texture.get("surfaceDetails", []),
        "assetSpec.texture.surfaceDetails",
        allow_empty=True,
    )
    material = texture.get("material")
    material_values = None
    if material is not None:
        material = _require_object(material, "assetSpec.texture.material")
        base_color = _require_text(
            material.get("baseColor"), "assetSpec.texture.material.baseColor"
        )
        if not _HEX_COLOR.fullmatch(base_color):
            raise tool_error(
                VALIDATION_ERROR,
                "assetSpec.texture.material.baseColor must be #RRGGBB",
            )
        material_values = {"baseColor": base_color}
        for field in ("metallic", "roughness"):
            value = material.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                raise tool_error(
                    VALIDATION_ERROR,
                    f"assetSpec.texture.material.{field} must be between 0 and 1",
                )
            material_values[field] = float(value)
    if texture_required and not texture_maps and material_values is None:
        raise tool_error(
            VALIDATION_ERROR,
            "assetSpec.texture.maps must be non-empty unless texture.material is provided",
        )

    animation = _require_object(asset_spec.get("animation"), "assetSpec.animation")
    animation_required = animation.get("required")
    if not isinstance(animation_required, bool):
        raise tool_error(VALIDATION_ERROR, "assetSpec.animation.required must be a boolean")
    if animation_required:
        raise tool_error(
            VALIDATION_ERROR,
            "assetSpec.animation.required=true is unsupported: the current 3D provider path does not create or verify rigs and animation clips",
        )
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
        "silhouette": silhouette,
        "primaryVolumes": primary_volumes,
        "partRelationships": part_relationships,
        "surfaceFeatures": surface_features,
        "bevelPolicy": bevel_policy,
        "materials": materials,
        "preserve": preserve,
        "exclude": exclude,
        "maxTriangles": max_triangles,
        "separateMeshes": separate_meshes,
        "normalPolicy": normal_policy,
        "faceOrientation": face_orientation,
        "smoothingRules": smoothing_rules,
        "integrity": integrity_flags,
        "modelFormat": model_format,
        "scale": scale,
        "pivot": pivot,
        "pivotPolicy": pivot_policy,
        "collider": collider,
        "textureRequired": texture_required,
        "textureDescription": texture_description,
        "textureMaps": texture_maps,
        "textureSurfaceDetails": surface_details,
        "textureMaterial": material_values,
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
        f"Silhouette: {spec['silhouette']}.",
        f"Primary volumes: {', '.join(spec['primaryVolumes'])}.",
        f"Part relationships: {', '.join(spec['partRelationships'])}.",
        f"Surface features: {', '.join(spec['surfaceFeatures'])}.",
        f"Bevel only: {', '.join(spec['bevelPolicy'])}.",
        f"Use {spec['style']} style with {spec['proportions']} proportions.",
        f"Colors: {', '.join(spec['colors'])}.",
    ]
    if spec["materials"]:
        clauses.append(f"Materials: {', '.join(spec['materials'])}.")
    if spec["textureRequired"]:
        texture_clause = f"Texture: {spec['textureDescription']}"
        if spec["textureMaps"]:
            texture_clause += f"; maps {', '.join(spec['textureMaps'])}"
        if spec["textureMaterial"]:
            texture_clause += f"; material {spec['textureMaterial']}"
        clauses.append(texture_clause + ".")
    if spec["assetType"] in {"character", "slime", "monster"} or spec["animationRequired"]:
        clauses.append("Use a neutral pose with unobstructed parts and animation-ready deformation.")
    clauses.append(
        f"Keep the mesh at or below {spec['maxTriangles']} triangles and export {spec['modelFormat']}."
    )
    clauses.append(
        f"Normals {spec['normalPolicy']}; faces {spec['faceOrientation']}; smoothing {', '.join(spec['smoothingRules'])}."
    )
    clauses.append(
        "Topology integrity: no degenerate or duplicate faces, no coplanar overlaps, "
        "and preserve hard-edge vertex/normal splits through triangulation and export."
    )
    if spec["separateMeshes"]:
        clauses.append(f"Separate meshes: {', '.join(spec['separateMeshes'])}.")
    clauses.append(
        f"Use scale {spec['scale']}, pivot {spec['pivot']} ({spec['pivotPolicy']}); preserve that origin after export, and use collider plan {spec['collider']}."
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
    reference_constraints = []
    if spec["preserve"]:
        reference_constraints.append(f"preserve {', '.join(spec['preserve'])}")
    if spec["exclude"]:
        reference_constraints.append(f"do not include {', '.join(spec['exclude'])}")
    reference_base = (
        f"{spec['assetName']}, {spec['description']} {spec['style']} style, "
        f"{spec['proportions']} proportions, colors {', '.join(spec['colors'])}. "
        f"Design for at most {spec['maxTriangles']} triangles: model only the silhouette and "
        "primary volumes; represent repeated or tiny non-silhouette details as flat color or "
        "normal-map information instead of raised geometry"
    )
    if reference_constraints:
        reference_base += ". " + "; ".join(reference_constraints)
    return {
        "textureStrategy": _texture_strategy(spec),
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


def _request_package(
    feature_id: str, game_id: str, asset_spec: dict[str, Any]
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    prompts = _compose_prompts(asset_spec)
    provenance = _validate(
        asset_spec, prompts["generationPrompt"], prompts["referenceSearchPrompt"]
    )
    request_id = f"{provenance['assetId']}__{provenance['sourceSpecSha256'][:12]}"
    request_path = ROOT / "3d" / "requests" / game_id / f"{request_id}.json"
    spec = _validate_asset_spec(asset_spec)
    visual_review_checklist = [
        "The asset is the same object across all three views and its silhouette matches the specification.",
        f"Only the declared palette is present: {', '.join(spec['colors'])}.",
        "No undeclared patterns, text, logos, symbols, or accessories are present.",
    ]
    if spec["preserve"]:
        visual_review_checklist.append(f"Preserved: {', '.join(spec['preserve'])}.")
    if spec["exclude"]:
        visual_review_checklist.append(f"Absent: {', '.join(spec['exclude'])}.")
    return request_path, {
        "requestId": request_id,
        "gameId": game_id,
        "featureId": feature_id,
        "createdAt": _now(),
        "assetSpec": asset_spec,
        **prompts,
        "referenceGenerationPlan": {
            "provider": "gpt_image_api",
            "requiredViews": list(REFERENCE_VIEWS),
            "approvalRequired": True,
            "captureMode": "single_generation_contact_sheet",
            "visualReviewChecklist": visual_review_checklist,
            "nextStep": "Generate one left-to-right front, side, and back contact sheet of the same object in one GPT image generation; do not stitch independently generated images. Obtain human approval before Multi-Image-to-3D submission.",
        },
        "provenance": provenance,
    }, provenance


def _meshy_format(provenance: dict[str, str]) -> str:
    model_format = provenance["modelFormat"]
    if model_format not in MESHY_FORMATS:
        raise tool_error(
            VALIDATION_ERROR,
            f"Meshy supports only {sorted(MESHY_FORMATS)}; requested {model_format}",
        )
    return model_format


def _meshy_triangle_limit(asset_spec: dict[str, Any], method: str) -> int:
    max_triangles = _validate_asset_spec(asset_spec)["maxTriangles"]
    maximum = 15_000 if method == "image_to_3d" else 300_000
    if not 100 <= max_triangles <= maximum:
        raise tool_error(
            VALIDATION_ERROR,
            f"Meshy {method} requires maxTriangles between 100 and {maximum}",
        )
    return max_triangles


def _meshy_texture_prompt(spec: dict[str, Any]) -> str:
    clauses = [
        spec["textureDescription"],
        f"use only these colors: {', '.join(spec['colors'])}",
        f"materials: {', '.join(spec['materials'])}",
        "keep color regions clean and flat; do not invent extra colors, patterns, text, logos, symbols, or accessories",
    ]
    if spec["preserve"]:
        clauses.append(f"preserve: {', '.join(spec['preserve'])}")
    if spec["exclude"]:
        clauses.append(f"exclude: {', '.join(spec['exclude'])}")
    texture = "; ".join(clauses)
    if len(texture) > MESHY_PROMPT_LIMIT:
        raise tool_error(
            VALIDATION_ERROR,
            f"Meshy texture prompt exceeds {MESHY_PROMPT_LIMIT} characters ({len(texture)})",
        )
    return texture


def _require_reference_image(value: str, field: str) -> str:
    url = _require_text(value, field)
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc:
        return url
    prefix, separator, encoded = url.partition(",")
    if prefix not in {"data:image/png;base64", "data:image/jpeg;base64"} or not separator:
        raise tool_error(VALIDATION_ERROR, f"{field} must be an https URL or PNG/JPEG data URI")
    try:
        base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise tool_error(VALIDATION_ERROR, f"{field} contains invalid base64 data") from exc
    return url


def _split_reference_contact_sheet(value: str) -> list[str]:
    """Turn an approved left-to-right front/side/back sheet into Meshy inputs.

    Meshy accepts one image, but it does not infer that three panels represent
    three viewpoints.  Splitting keeps the review surface compact while still
    giving Multi-Image-to-3D its required three independent images.
    """
    reference = _require_reference_image(value, "referenceContactSheetUrl")
    prefix, _, encoded = reference.partition(",")
    if not prefix.startswith("data:image/"):
        raise tool_error(
            VALIDATION_ERROR,
            "referenceContactSheetUrl must be a PNG/JPEG data URI so it can be split locally",
        )
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(raw)) as image:
            image.load()
            if image.width < 3:
                raise tool_error(
                    VALIDATION_ERROR,
                    "referenceContactSheetUrl must have a three-column layout (front, side, back)",
                )
            # GPT image outputs are not guaranteed to have a width divisible by three.
            # Keep all pixels and distribute a one- or two-pixel remainder across
            # the panels rather than rejecting an otherwise valid single generation.
            boundaries = [round(index * image.width / 3) for index in range(4)]
            panels = [
                image.crop((boundaries[index], 0, boundaries[index + 1], image.height))
                .convert("RGBA")
                for index in range(3)
            ]
    except (OSError, ValueError) as exc:
        raise tool_error(VALIDATION_ERROR, "referenceContactSheetUrl is not a readable image") from exc
    references: list[str] = []
    for panel in panels:
        buffer = BytesIO()
        panel.save(buffer, format="PNG")
        references.append(f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}")
    return references


def _reference_inputs(
    reference_image_url: str,
    reference_image_urls: list[str] | None,
    reference_contact_sheet_url: str,
    reference_provenance: dict[str, Any] | None,
) -> tuple[list[str], dict[str, Any], str]:
    values = list(reference_image_urls or [])
    supplied = sum(bool(value) for value in (reference_image_url, reference_contact_sheet_url)) + bool(values)
    if supplied > 1:
        raise tool_error(
            VALIDATION_ERROR,
            "use referenceImageUrls, legacy referenceImageUrl, or referenceContactSheetUrl, not more than one",
        )
    input_mode = "individual_images"
    if reference_contact_sheet_url:
        values = _split_reference_contact_sheet(reference_contact_sheet_url)
        input_mode = "three_view_contact_sheet"
    elif reference_image_url:
        values = [reference_image_url]
    if not 1 <= len(values) <= 4:
        raise tool_error(VALIDATION_ERROR, "referenceImageUrls must contain 1 to 4 images")
    references = [
        _require_reference_image(value, f"referenceImageUrls[{index}]")
        for index, value in enumerate(values)
    ]
    if len(set(references)) != len(references):
        raise tool_error(VALIDATION_ERROR, "referenceImageUrls must not contain duplicates")
    evidence = reference_provenance or {}
    source = evidence.get("source")
    if not isinstance(source, str) or not source.strip():
        raise tool_error(VALIDATION_ERROR, "referenceProvenance.source is required")
    if evidence.get("humanApproved") is not True:
        raise tool_error(
            VALIDATION_ERROR,
            "referenceProvenance.humanApproved must be true before paid submission",
        )
    if input_mode == "three_view_contact_sheet" and evidence.get("captureMode") != "single_generation_contact_sheet":
        raise tool_error(
            VALIDATION_ERROR,
            "referenceProvenance.captureMode must be single_generation_contact_sheet; stitched independent images are not valid multi-view references",
        )
    prompt_hash = evidence.get("sourcePromptSha256")
    if prompt_hash is not None and (
        not isinstance(prompt_hash, str) or re.fullmatch(r"[0-9a-f]{64}", prompt_hash) is None
    ):
        raise tool_error(
            VALIDATION_ERROR,
            "referenceProvenance.sourcePromptSha256 must be a lowercase SHA-256 digest",
        )
    normalized_evidence = {
        "source": source.strip(),
        "humanApproved": True,
        **({"sourcePromptSha256": prompt_hash} if prompt_hash else {}),
    }
    if input_mode == "three_view_contact_sheet":
        normalized_evidence["captureMode"] = "single_generation_contact_sheet"
    return references, normalized_evidence, input_mode


def _cleanup_with_blender(
    model_path: Path,
    max_triangles: int,
    material: dict[str, Any] | None = None,
    output_format: str | None = None,
    normal_policy: str = "mixed",
    preserve_uv: bool = False,
) -> Path:
    configured = os.getenv("BLENDER_PATH")
    executable = configured if configured and Path(configured).is_file() else shutil.which("blender")
    if not executable:
        raise tool_error(MCP_ERROR, "Blender not found; set BLENDER_PATH", provider="blender")
    stem = model_path.stem.removesuffix("-clean")
    suffix = f".{output_format}" if output_format else model_path.suffix
    output_path = model_path.with_name(f"{stem}-clean{suffix}")
    try:
        command = [
            executable,
            "--background",
            "--python",
            str(BLENDER_SCRIPT),
            "--",
            str(model_path),
            str(output_path),
            str(max_triangles),
        ]
        command.extend(
            [
                json.dumps(material, separators=(",", ":")),
                normal_policy,
                "preserve_uv" if preserve_uv else "rebuild",
            ]
        )
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise tool_error(MCP_ERROR, f"Blender cleanup failed: {exc}", provider="blender") from exc
    if process.returncode or not output_path.is_file():
        message = (process.stderr or process.stdout)[-800:]
        raise tool_error(MCP_ERROR, f"Blender cleanup failed: {message}", provider="blender")
    return output_path


def _blender_inspection(model_path: Path) -> dict[str, Any] | None:
    report_path = Path(f"{model_path}.report.json")
    if not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return report if isinstance(report, dict) else None


def _require_meshy_model_url(value: Any) -> str:
    if not isinstance(value, str):
        raise tool_error(MCP_ERROR, "Meshy returned an invalid model URL", provider="meshy")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "assets.meshy.ai":
        raise tool_error(MCP_ERROR, "Meshy returned an unexpected model URL", provider="meshy")
    return value


def _inspect_model(
    model: bytes,
    model_format: str,
    animation_required: bool,
    texture_required: bool = True,
    max_triangles: int | None = None,
) -> dict[str, int | bool]:
    if model_format == "glb":
        if len(model) < 20:
            raise tool_error(MCP_ERROR, "Meshy returned a truncated GLB model", provider="meshy")
        magic, version, declared_size = struct.unpack_from("<III", model)
        chunk_size, chunk_type = struct.unpack_from("<II", model, 12)
        if magic != 0x46546C67 or version != 2 or declared_size != len(model) or chunk_type != 0x4E4F534A:
            raise tool_error(MCP_ERROR, "Meshy returned an invalid GLB model", provider="meshy")
        try:
            document = json.loads(model[20 : 20 + chunk_size].decode("utf-8").rstrip(" \t\r\n\0"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise tool_error(MCP_ERROR, "Meshy returned an unreadable GLB document", provider="meshy") from exc
        meshes = len(document.get("meshes") or [])
        textures = len(document.get("textures") or [])
        rigs = len(document.get("skins") or [])
        accessors = document.get("accessors") or []
        vertices = triangles = 0
        for mesh in document.get("meshes") or []:
            for primitive in mesh.get("primitives") or []:
                position = (primitive.get("attributes") or {}).get("POSITION")
                indices = primitive.get("indices")
                if isinstance(position, int) and 0 <= position < len(accessors):
                    vertices += accessors[position].get("count", 0)
                if isinstance(indices, int) and 0 <= indices < len(accessors):
                    triangles += accessors[indices].get("count", 0) // 3
    else:
        if not (model.startswith(b"Kaydara FBX Binary") or model.startswith(b"; FBX")):
            raise tool_error(MCP_ERROR, "Meshy returned an invalid FBX model", provider="meshy")
        meshes = model.count(b"Geometry::")
        textures = model.count(b"Texture::")
        rigs = model.count(b"Deformer::")
        vertices = triangles = 0
    if not meshes or (texture_required and not textures) or (animation_required and not rigs):
        raise tool_error(
            MCP_ERROR,
            "Meshy model failed mesh, texture, or required rig validation",
            provider="meshy",
        )
    inspection: dict[str, int | bool] = {
        "meshes": meshes,
        "textures": textures,
        "rigs": rigs,
        "vertices": vertices,
        "triangles": triangles,
    }
    if max_triangles is not None:
        inspection["triangleBudgetPassed"] = triangles <= max_triangles
    return inspection


def _texture_strategy(spec: dict[str, Any]) -> dict[str, str]:
    if not spec["textureRequired"]:
        return {"mode": "none", "reason": "assetSpec.texture.required is false"}
    uniform_palette = len(spec["colors"]) <= 1 and len(spec["materials"]) <= 1
    if spec["textureMaterial"] and not spec["textureSurfaceDetails"] and uniform_palette:
        return {
            "mode": "material_only",
            "reason": "one color and material are declared with no unique surface details",
        }
    return {
        "mode": "generated_texture",
        "reason": "multiple appearance regions, unique details, or no numeric material are declared",
    }


def _task_path(task_id: str) -> Path:
    return ROOT / "3d" / "tasks" / f"{task_id}.json"


def _read_task(task_id: str) -> dict[str, Any]:
    path = _task_path(task_id)
    try:
        task = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise tool_error(VALIDATION_ERROR, f"3D task not found: {task_id}") from exc
    if task.get("provider") != "meshy":
        raise tool_error(VALIDATION_ERROR, f"unsupported 3D provider task: {task_id}")
    return task


def _meshy_error(exc: meshy_client.MeshyUnavailable) -> None:
    raise tool_error(
        MCP_ERROR,
        str(exc),
        provider="meshy",
        status=getattr(exc, "status", "provider_failed"),
        httpStatus=getattr(exc, "http_status", None),
    ) from exc


def _actual_credits(
    current: dict[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> int | float | None:
    reported = current.get("consumed_credits")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return reported
    before_value = (before or {}).get("balance")
    after_value = (after or {}).get("balance")
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (before_value, after_value)):
        return max(0, before_value - after_value)
    return None


def _unity_import_path(task: dict[str, Any], model_path: Path, sha256: str) -> Path:
    unity_value = os.getenv("UNITY_PROJECT_PATH", "").strip()
    if not unity_value:
        raise tool_error(VALIDATION_ERROR, "UNITY_PROJECT_PATH must be set for Unity import")
    unity_root = Path(unity_value)
    if not unity_root.is_absolute() or _is_within(unity_root, REPOSITORY_ROOT):
        raise tool_error(
            VALIDATION_ERROR,
            "UNITY_PROJECT_PATH must be an absolute workspace outside the orchestration repository",
        )
    assets = unity_root.resolve() / "Assets"
    if not assets.is_dir():
        raise tool_error(VALIDATION_ERROR, f"Unity Assets directory not found: {assets}")
    # One folder per asset: the Unity side treats everything beside the model as its own,
    # which the sidecar texture folder relies on.
    stem = f"{task['assetId']}-{sha256[:12]}"
    target = assets / "Generated3D" / task["featureId"] / stem / f"{stem}{model_path.suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    # The FBX references its maps relative to its own folder, so the sidecar keeps its name.
    textures = model_path.with_name(f"{model_path.stem}.fbm")
    requires_generated_textures = task.get("textureStrategy", {}).get("mode") == "generated_texture"
    if requires_generated_textures and not any(
        candidate.is_file()
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.tga")
        for candidate in textures.glob(pattern)
    ):
        raise tool_error(
            VALIDATION_ERROR,
            "Generated-texture model is missing its FBX .fbm texture sidecar",
        )
    shutil.copy2(model_path, target)
    if textures.is_dir():
        shutil.copytree(textures, target.parent / textures.name, dirs_exist_ok=True)
    return target


def _pack_metallic_smoothness(model_path: Path) -> Path | None:
    """URP reads metallic from RGB and smoothness from alpha of a single map.

    Providers deliver metallic and roughness as separate greyscale files, which Unity
    cannot bind to `_MetallicGlossMap` as they are. The two inputs are consumed: leaving
    them behind ships two more 1K textures that nothing samples.
    """
    sidecar = model_path.with_name(f"{model_path.stem}.fbm")
    if not sidecar.is_dir():
        return None
    metallic = next(iter(sorted(sidecar.glob("*_metallic.png"))), None)
    roughness = next(iter(sorted(sidecar.glob("*_roughness.png"))), None)
    if metallic is None or roughness is None:
        return None
    with Image.open(metallic) as metallic_image, Image.open(roughness) as roughness_image:
        metal = metallic_image.convert("L")
        smoothness = ImageChops.invert(roughness_image.convert("L").resize(metal.size))
        packed = Image.merge("RGBA", (metal, metal, metal, smoothness))
    target = sidecar / f"{metallic.stem.removesuffix('_metallic')}_metallicSmoothness.png"
    packed.save(target)
    metallic.unlink()
    roughness.unlink()
    return target


def _drop_empty_maps(model_path: Path) -> list[str]:
    """Remove maps that carry no information.

    Providers emit a full-resolution emission map even when nothing emits light. Unity
    imports it, binds it, and pays for it in memory and download size for nothing.
    """
    sidecar = model_path.with_name(f"{model_path.stem}.fbm")
    if not sidecar.is_dir():
        return []
    dropped = []
    for candidate in sorted(sidecar.glob("*_emission.png")):
        with Image.open(candidate) as image:
            brightest = image.convert("L").getextrema()[1]
        if brightest <= EMPTY_MAP_THRESHOLD:
            candidate.unlink()
            dropped.append(candidate.name)
    return dropped


def _game_ready_result(
    task: dict[str, Any],
    source_path: Path,
    *,
    provider: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    material = task.get("textureMaterial") if task["textureStrategy"]["mode"] == "material_only" else None
    output_format = task.get("outputFormat", task["modelFormat"])
    # Geometry was already rebuilt during refine; this pass must not disturb the textured UVs.
    clean_path = _cleanup_with_blender(
        source_path,
        task["maxTriangles"],
        material,
        output_format if source_path.suffix[1:].lower() != output_format else None,
        task["normalPolicy"],
        preserve_uv=True,
    )
    inspection = _blender_inspection(clean_path)
    if not inspection or not inspection.get("triangleBudgetPassed"):
        raise tool_error(MCP_ERROR, "Blender output exceeds triangle budget", provider="blender", status="quality_rejected")
    if not inspection.get("gameReadyPassed"):
        raise tool_error(MCP_ERROR, "Blender output failed the GameReady quality gate", provider="blender", status="quality_rejected")
    packed_map = _pack_metallic_smoothness(clean_path)
    dropped_maps = _drop_empty_maps(clean_path)
    content_hash = hashlib.sha256(clean_path.read_bytes()).hexdigest()
    unity_path = _unity_import_path(task, clean_path, content_hash)
    completed = {
        **task,
        "status": "SUCCEEDED",
        "provider": provider,
        "assetPath": str(clean_path),
        "unityAssetPath": str(unity_path),
        "inspection": inspection,
        "provenance": {**provenance, "gameReadySha256": content_hash},
        "completedAt": _now(),
    }
    _write_json(_task_path(task["taskId"]), completed)
    return {
        "taskId": task["taskId"],
        "status": "SUCCEEDED",
        "provider": provider,
        "assetPath": str(clean_path),
        "unityAssetPath": str(unity_path),
        "modelFormat": output_format,
        "inspection": inspection,
        "metallicSmoothnessMap": str(packed_map) if packed_map else "",
        "droppedMaps": dropped_maps,
        "provenance": completed["provenance"],
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
        "Compose, validate, and save a provider-neutral 3D request package in the external "
        "Unity workspace staging area."
    )
)
@expects_dict_return
def prepare_3d_asset_request(
    featureId: str,
    assetSpec: dict[str, Any],
    gameId: str | None = None,
) -> dict[str, Any]:
    _require_external_runtime_root()
    feature_id = _require_id(featureId, "featureId")
    game_id = _require_id(gameId or os.getenv("ASSET_DEFAULT_GAME_ID", "default"), "gameId")
    request_path, package, provenance = _request_package(feature_id, game_id, assetSpec)
    package.update(
        {
            "status": "prepared",
            "provider": {
            "configured": meshy_client.is_configured(),
            "message": "Meshy is the configured 3D generation provider.",
        },
        }
    )
    _write_json(request_path, package)
    return {
        "requestId": package["requestId"],
        "requestPath": str(request_path),
        "assetId": provenance["assetId"],
        "assetType": provenance["assetType"],
        "modelFormat": provenance["modelFormat"],
        "textureStrategy": package["textureStrategy"],
        "visualReviewChecklist": package["referenceGenerationPlan"]["visualReviewChecklist"],
        "referenceGenerationPlan": package["referenceGenerationPlan"],
        "status": package["status"],
        "providerConfigured": meshy_client.is_configured(),
    }


@mcp.tool(description="Submit approved reference images to Meshy for 3D generation.")
@expects_dict_return
async def submit_3d_asset_generation(
    featureId: str,
    assetSpec: dict[str, Any],
    referenceImageUrl: str = "",
    referenceImageUrls: list[str] | None = None,
    referenceContactSheetUrl: str = "",
    referenceProvenance: dict[str, Any] | None = None,
    gameId: str | None = None,
) -> dict[str, Any]:
    _require_external_runtime_root()
    feature_id = _require_id(featureId, "featureId")
    game_id = _require_id(gameId or os.getenv("ASSET_DEFAULT_GAME_ID", "default"), "gameId")
    request_path, package, provenance = _request_package(feature_id, game_id, assetSpec)
    method = package["generationPrompt"]["method"]
    if method != "image_to_3d":
        raise tool_error(
            VALIDATION_ERROR,
            "Meshy submission requires generation.method=image_to_3d; text_to_3d is disabled",
        )
    output_format = _meshy_format(provenance)
    model_format = "glb"
    max_triangles = _meshy_triangle_limit(assetSpec, method)
    spec = _validate_asset_spec(assetSpec)
    references: list[str] = []
    reference_evidence: dict[str, Any] = {}
    task = {
        "provider": "meshy",
        "taskId": package["requestId"],
        "phase": "generation",
        "method": method,
        "modelFormat": model_format,
        "outputFormat": output_format,
        "gameId": game_id,
        "featureId": feature_id,
        "assetId": provenance["assetId"],
        "requestPath": str(request_path),
        "sourceSpecSha256": provenance["sourceSpecSha256"],
        "animationRequired": spec["animationRequired"],
        "textureRequired": spec["textureRequired"],
        "texturePrompt": _meshy_texture_prompt(spec),
        "textureStrategy": package["textureStrategy"],
        "textureMaterial": spec["textureMaterial"],
        "normalPolicy": spec["normalPolicy"],
        "maxTriangles": max_triangles,
        "referenceProvenance": reference_evidence,
        "referenceImageCount": len(references),
        "createdAt": _now(),
    }
    references, reference_evidence, reference_input_mode = _reference_inputs(
        referenceImageUrl, referenceImageUrls, referenceContactSheetUrl, referenceProvenance
    )
    if len(references) != 3:
        raise tool_error(
            VALIDATION_ERROR,
            "3D assets require exactly three approved front, side, and back references generated by GPT; single-image submission is disabled",
        )
    reference_set_sha256 = _digest(
        {"images": references, "provenance": reference_evidence}
    )
    task.update(
        {
            "referenceProvenance": reference_evidence,
            "referenceImageCount": len(references),
            "referenceInputMode": reference_input_mode,
            "referenceSetSha256": reference_set_sha256,
        }
    )
    if not meshy_client.is_configured():
        raise tool_error(
            MCP_ERROR,
            "MESHY_API_KEY is not set",
            provider="meshy",
            status="provider_unconfigured",
        )
    submission_key = package["requestId"]
    submission_key = f"{submission_key}__{task['referenceSetSha256'][:16]}"
    submission_path = (
        _require_external_runtime_root()
        / "3d"
        / "submissions"
        / f"{submission_key}.json"
    )
    existing = _claim_submission(submission_path, package["requestId"])
    if existing is not None:
        existing_id = existing.get("taskId")
        if isinstance(existing_id, str) and existing_id:
            return {
                "requestId": package["requestId"],
                "taskId": existing_id,
                "status": "duplicate_blocked",
                "provider": "meshy",
            }
        return {
            "requestId": package["requestId"],
            "status": "submission_in_progress",
            "provider": "meshy",
            "recoveryRequired": True,
        }
    texture_prompt = task["texturePrompt"]
    try:
        balance_before = await _await_result(meshy_client.get_balance())
        credit_estimate = meshy_client.estimate_credits("multi_image_meshy_6_untextured")
        provider_method = "multi_image_to_3d"
        task_id = await _await_result(
            meshy_client.create_multi_image_task(references, model_format, max_triangles)
        )
        phase = "generation"
    except meshy_client.MeshyUnavailable as exc:
        _write_json(
            submission_path,
            {
                "requestId": package["requestId"],
                "status": "SUBMISSION_UNCERTAIN",
                "createdAt": _now(),
                "providerStatus": exc.status,
            },
        )
        _meshy_error(exc)

    task = {
        **task,
        "provider": "meshy",
        "taskId": task_id,
        "phase": phase,
        "method": method,
        "providerMethod": provider_method,
        "modelFormat": model_format,
        "outputFormat": output_format,
        "texturePrompt": texture_prompt,
        "topology": "triangle",
        "estimatedCredits": credit_estimate["credits"],
        "creditEstimate": credit_estimate,
        "balanceBefore": balance_before,
        "referenceProvenance": reference_evidence,
        "referenceImageCount": len(references),
        "createdAt": _now(),
    }
    package.update(
        {
            "status": "submitted",
            "provider": {"name": "meshy", "taskId": task_id, "phase": phase},
        }
    )
    _write_json(request_path, package)
    _write_json(_task_path(task_id), task)
    _write_json(submission_path, {"requestId": package["requestId"], "taskId": task_id, "createdAt": _now()})
    return {
        "requestId": package["requestId"],
        "requestPath": str(request_path),
        "taskId": task_id,
        "phase": phase,
        "provider": "meshy",
        "providerConfigured": True,
        "textureStrategy": package["textureStrategy"],
        "status": "submitted",
        "estimatedCredits": task["estimatedCredits"],
        "creditEstimate": task["creditEstimate"],
        "balanceBefore": balance_before,
        "referenceImageCount": len(references),
        "referenceProvenance": reference_evidence,
        "referenceSetSha256": task.get("referenceSetSha256", ""),
    }


@mcp.tool(
    description=(
        "After human geometry approval, refine or retexture a completed Meshy preview."
    )
)
@expects_dict_return
async def refine_3d_asset_generation(
    taskId: str,
    geometryReviewApproved: bool = False,
    geometryReviewNote: str = "",
) -> dict[str, Any]:
    task_id = _require_id(taskId, "taskId")
    task = _read_task(task_id)
    if geometryReviewApproved is not True:
        raise tool_error(
            VALIDATION_ERROR,
            "geometryReviewApproved must be true after human preview review",
        )
    task["geometryReview"] = {
        "humanApproved": True,
        "note": geometryReviewNote.strip(),
        "reviewedAt": _now(),
    }
    if (task["method"], task["phase"]) != ("image_to_3d", "generation"):
        raise tool_error(VALIDATION_ERROR, "only completed Meshy geometry can be refined")
    try:
        current = await _await_result(
            meshy_client.get_task(task.get("providerMethod", task["method"]), task_id)
        )
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    if current.get("status") != "SUCCEEDED":
        return {
            "taskId": task_id,
            "phase": task["phase"],
            "status": current.get("status", "UNKNOWN"),
        }
    model_url = _require_meshy_model_url(
        (current.get("model_urls") or {}).get(task["modelFormat"])
    )
    try:
        output = await _await_result(meshy_client.download_model(model_url))
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    raw_path = ROOT / "3d" / "models" / task["gameId"] / f"{task_id}.{task['modelFormat']}"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(output)
    # Keep the reviewed geometry preview available while the separate texture
    # task runs.  Otherwise callers have to unpack provider-specific evidence
    # (or lose the preview altogether after a process restart).
    task.update(
        {
            "thumbnailUrl": current.get("thumbnail_url", ""),
            "thumbnailUrls": current.get("thumbnail_urls", {}),
            "providerEvidence": current,
        }
    )
    _write_json(_task_path(task_id), task)
    material = (
        task["textureMaterial"]
        if task["textureStrategy"]["mode"] == "material_only"
        else None
    )
    clean_path = _cleanup_with_blender(
        raw_path,
        task["maxTriangles"],
        material,
        None,
        task["normalPolicy"],
    )
    clean_model = clean_path.read_bytes()
    inspection = _blender_inspection(clean_path) or _inspect_model(
        clean_model, task["modelFormat"], task["animationRequired"], False, task["maxTriangles"]
    )
    if not inspection.get("triangleBudgetPassed"):
        raise tool_error(MCP_ERROR, "Blender output exceeds triangle budget", provider="blender")
    if inspection.get("gameReadyPassed") is False:
        raise tool_error(MCP_ERROR, "Blender output failed the GameReady quality gate", provider="blender")
    if task["textureStrategy"]["mode"] != "generated_texture":
        final_path = clean_path
        if task.get("outputFormat", task["modelFormat"]) != task["modelFormat"]:
            final_path = _cleanup_with_blender(
                clean_path,
                task["maxTriangles"],
                material,
                task["outputFormat"],
                task["normalPolicy"],
                preserve_uv=True,
            )
            inspection = _blender_inspection(final_path) or inspection
        final_hash = hashlib.sha256(final_path.read_bytes()).hexdigest()
        try:
            balance_after = await _await_result(meshy_client.get_balance())
        except meshy_client.MeshyUnavailable as exc:
            balance_after = {"error": str(exc), "status": exc.status}
        consumed_credits = _actual_credits(current, task.get("balanceBefore"), balance_after)
        provenance = {
            "license": "generated",
            "provider": "meshy",
            "taskId": task_id,
            "estimatedCredits": task.get("estimatedCredits"),
            "consumedCredits": consumed_credits,
            "balanceBefore": task.get("balanceBefore"),
            "balanceAfter": balance_after,
            "downloadedAt": _now(),
            "sha256": hashlib.sha256(output).hexdigest(),
            "gameReadySha256": final_hash,
        }
        task.update(
            {
                "status": "AWAITING_FINAL_REVIEW",
                "phase": "material_review",
                "assetPath": str(final_path),
                "inspection": inspection,
                "provenance": provenance,
            }
        )
        _write_json(_task_path(task_id), task)
        return {
            "taskId": task_id,
            "phase": "material_review",
            "provider": "blender",
            "status": "AWAITING_FINAL_REVIEW",
            "assetPath": str(final_path),
            "modelFormat": task.get("outputFormat", task["modelFormat"]),
            "inspection": inspection,
            "textureStrategy": task["textureStrategy"],
            "provenance": provenance,
            "nextStep": "Inspect the staged asset, then call finalize_3d_asset_generation.",
        }
    # Meshy generation always returns GLB, but retexture runs on FBX so the
    # textured result imports into Unity with its material slots intact.
    retexture_path = (
        clean_path
        if clean_path.suffix.lower() == f".{RETEXTURE_FORMAT}"
        else _cleanup_with_blender(
            clean_path,
            task["maxTriangles"],
            material,
            RETEXTURE_FORMAT,
            task["normalPolicy"],
            preserve_uv=True,
        )
    )
    try:
        balance_before = await _await_result(meshy_client.get_balance())
        refine_task_id = await _await_result(meshy_client.create_retexture_task(
            retexture_path.read_bytes(),
            RETEXTURE_FORMAT,
            task["texturePrompt"],
        ))
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    refined = {
        **task,
        "taskId": refine_task_id,
        "method": "retexture",
        "providerMethod": "retexture",
        "modelFormat": RETEXTURE_FORMAT,
        "phase": "retexture",
        "sourceTaskId": task_id,
        "postprocessedPath": str(retexture_path),
        "postprocessInspection": inspection,
        "balanceBefore": balance_before,
        "estimatedCredits": meshy_client.estimate_credits("retexture_2k")["credits"],
        "creditEstimate": meshy_client.estimate_credits("retexture_2k"),
        "createdAt": _now(),
    }
    _write_json(_task_path(refine_task_id), refined)
    return {
        "taskId": refine_task_id,
        "phase": refined["phase"],
        "provider": "meshy",
        "status": "submitted",
        "estimatedCredits": refined.get("estimatedCredits"),
        "balanceBefore": refined.get("balanceBefore"),
    }


@mcp.tool(description="Get a Meshy 3D generation status and download its completed model.")
@expects_dict_return
async def get_3d_asset_generation(taskId: str) -> dict[str, Any]:
    task_id = _require_id(taskId, "taskId")
    task = _read_task(task_id)
    if task.get("status") == "AWAITING_FINAL_REVIEW" and task.get("assetPath"):
        return {
            "taskId": task_id,
            "phase": task["phase"],
            "provider": task.get("provider", "meshy"),
            "status": "AWAITING_FINAL_REVIEW",
            "assetPath": task["assetPath"],
            "inspection": task.get("inspection"),
            "thumbnailUrl": task.get("thumbnailUrl", ""),
            "thumbnailUrls": task.get("thumbnailUrls", {}),
            "visualReviewChecklist": task.get("visualReviewChecklist", []),
            "nextStep": "Inspect the staged asset, then call finalize_3d_asset_generation.",
        }
    try:
        current = await _await_result(
            meshy_client.get_task(task.get("providerMethod", task["method"]), task_id)
        )
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    status = current.get("status", "UNKNOWN")
    result = {
        "taskId": task_id,
        "phase": task["phase"],
        "provider": "meshy",
        "status": status,
        "progress": current.get("progress"),
        "consumedCredits": current.get("consumed_credits"),
        "thumbnailUrl": current.get("thumbnail_url", ""),
        "thumbnailUrls": current.get("thumbnail_urls", {}),
    }
    if status != "SUCCEEDED":
        result["providerError"] = (current.get("task_error") or {}).get("message", "")
        return result
    model_url = _require_meshy_model_url((current.get("model_urls") or {}).get(task["modelFormat"]))
    output_path = ROOT / "3d" / "models" / task["gameId"] / f"{task_id}.{task['modelFormat']}"
    try:
        output = await _await_result(meshy_client.download_model(model_url))
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output)
    if task["phase"] == "retexture":
        try:
            balance_after = await _await_result(meshy_client.get_balance())
        except meshy_client.MeshyUnavailable as exc:
            balance_after = {"error": str(exc), "status": exc.status}
        consumed_credits = _actual_credits(current, task.get("balanceBefore"), balance_after)
        provenance = {
                "license": "generated",
                "provider": "meshy",
                "taskId": task_id,
                "sourceSpecSha256": task["sourceSpecSha256"],
                "estimatedCredits": task.get("estimatedCredits"),
                "consumedCredits": consumed_credits,
                "balanceBefore": task.get("balanceBefore"),
                "balanceAfter": balance_after,
                "downloadedAt": _now(),
                "sha256": hashlib.sha256(output).hexdigest(),
            }
        task.update(
            {
                "status": "AWAITING_FINAL_REVIEW",
                "assetPath": str(output_path),
                "thumbnailUrl": current.get("thumbnail_url", ""),
                "thumbnailUrls": current.get("thumbnail_urls", {}),
                "textureUrls": current.get("texture_urls", []),
                "providerEvidence": current,
                "provenance": provenance,
                "consumedCredits": consumed_credits,
            }
        )
        _write_json(_task_path(task_id), task)
        return {
            **result,
            "status": "AWAITING_FINAL_REVIEW",
            "assetPath": str(output_path),
            "modelFormat": task["modelFormat"],
            "provenance": provenance,
            "consumedCredits": consumed_credits,
            "visualReviewChecklist": task.get("visualReviewChecklist", []),
            "nextStep": "Inspect the staged asset, then call finalize_3d_asset_generation.",
        }
    result.update(
        {
            "assetPath": str(output_path),
            "modelFormat": task["modelFormat"],
            "inspection": _inspect_model(
                output,
                task["modelFormat"],
                False,
                False,
                task.get("maxTriangles"),
            ),
        }
    )
    try:
        result["balanceAfter"] = await _await_result(meshy_client.get_balance())
    except meshy_client.MeshyUnavailable as exc:
        result["balanceEvidenceError"] = {"status": exc.status, "message": str(exc)}
    result["consumedCredits"] = _actual_credits(
        current, task.get("balanceBefore"), result.get("balanceAfter")
    )
    task.update(
        {
            "status": status,
            "consumedCredits": result.get("consumedCredits"),
            "balanceAfter": result.get("balanceAfter"),
            "thumbnailUrl": current.get("thumbnail_url", ""),
            "thumbnailUrls": current.get("thumbnail_urls", {}),
            "providerEvidence": current,
        }
    )
    _write_json(_task_path(task_id), task)
    return result


@mcp.tool(
    description=(
        "Run Blender GameReady and Unity import only after a human approves the final visual review."
    )
)
@expects_dict_return
def finalize_3d_asset_generation(
    taskId: str,
    finalVisualReviewApproved: bool = False,
    finalVisualReviewNote: str = "",
) -> dict[str, Any]:
    task_id = _require_id(taskId, "taskId")
    task = _read_task(task_id)
    if finalVisualReviewApproved is not True:
        raise tool_error(
            VALIDATION_ERROR,
            "finalVisualReviewApproved must be true after human visual review",
        )
    if task.get("status") != "AWAITING_FINAL_REVIEW" or not task.get("assetPath"):
        raise tool_error(
            VALIDATION_ERROR,
            "the task must be downloaded and awaiting final review before finalization",
        )
    source_path = Path(task["assetPath"])
    if not source_path.is_file():
        raise tool_error(VALIDATION_ERROR, f"staged 3D asset not found: {source_path}")
    task["finalVisualReview"] = {
        "humanApproved": True,
        "note": finalVisualReviewNote.strip(),
        "reviewedAt": _now(),
    }
    return _game_ready_result(
        task,
        source_path,
        provider="meshy",
        provenance=task.get("provenance", {}),
    )


@mcp.tool(description="Cancel a resumable Meshy task by provider task id.")
@expects_dict_return
async def cancel_3d_asset_generation(taskId: str) -> dict[str, Any]:
    task_id = _require_id(taskId, "taskId")
    task = _read_task(task_id)
    try:
        evidence = await _await_result(
            meshy_client.cancel_task(task.get("providerMethod", task["method"]), task_id)
        )
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    task.update({"status": "CANCELED", "canceledAt": _now(), "cancelEvidence": evidence})
    _write_json(_task_path(task_id), task)
    return {"taskId": task_id, "status": "CANCELED", "provider": "meshy"}


if __name__ == "__main__":
    serve(mcp)
