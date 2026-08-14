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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error
from common.server import build, expects_dict_return, serve

from . import cc0_client, meshy_client


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
    {"image_to_3d", "text_to_3d", "manual_blender", "procedural", "existing_asset"}
)
MODEL_FORMATS = frozenset({"fbx", "glb", "gltf"})
REFERENCE_VIEWS = ("front", "side", "back")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
MESHY_FORMATS = frozenset({"glb", "fbx"})
MESHY_PROMPT_LIMIT = 600
BLENDER_SCRIPT = Path(__file__).with_name("blender_cleanup.py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


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
    reference_base = (
        f"{spec['assetName']}, {spec['description']} {spec['style']} style, "
        f"{spec['proportions']} proportions, colors {', '.join(spec['colors'])}. "
        f"Design for at most {spec['maxTriangles']} triangles: model only the silhouette and "
        "primary volumes; represent repeated or tiny non-silhouette details as flat color or "
        "normal-map information instead of raised geometry"
    )
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
    return request_path, {
        "requestId": request_id,
        "gameId": game_id,
        "featureId": feature_id,
        "createdAt": _now(),
        "assetSpec": asset_spec,
        **prompts,
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
    texture = "; ".join(
        [
            spec["textureDescription"],
            f"colors: {', '.join(spec['colors'])}",
            f"materials: {', '.join(spec['materials'])}",
        ]
    )
    if len(texture) > MESHY_PROMPT_LIMIT:
        raise tool_error(
            VALIDATION_ERROR,
            f"Meshy texture prompt exceeds {MESHY_PROMPT_LIMIT} characters ({len(texture)})",
        )
    return texture


def _meshy_prompts(asset_spec: dict[str, Any]) -> tuple[str, str]:
    spec = _validate_asset_spec(asset_spec)
    required = [
        f"{spec['assetName']}, {spec['assetType']}",
        f"preserve: {', '.join(spec['preserve'])}",
        f"silhouette: {spec['silhouette']}",
        f"volumes: {', '.join(spec['primaryVolumes'])}",
        f"parts: {', '.join(spec['partRelationships'])}",
        f"exclude: {', '.join(spec['exclude'])}",
    ]
    geometry = "; ".join(required)
    if len(geometry) > MESHY_PROMPT_LIMIT:
        raise tool_error(
            VALIDATION_ERROR,
            f"Meshy geometry prompt exceeds {MESHY_PROMPT_LIMIT} characters ({len(geometry)})",
        )
    for detail in (
        f"features: {', '.join(spec['surfaceFeatures'])}",
        f"bevels: {', '.join(spec['bevelPolicy'])}",
        f"style: {spec['style']}, {spec['proportions']}",
    ):
        candidate = f"{geometry}; {detail}"
        if len(candidate) <= MESHY_PROMPT_LIMIT:
            geometry = candidate
    return geometry, _meshy_texture_prompt(spec)


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


def _cleanup_with_blender(
    model_path: Path,
    max_triangles: int,
    material: dict[str, Any] | None = None,
    output_format: str | None = None,
    normal_policy: str = "mixed",
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
            [json.dumps(material, separators=(",", ":")), normal_policy]
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
    target = assets / "Generated3D" / task["featureId"] / (
        f"{task['assetId']}-{sha256[:12]}{model_path.suffix.lower()}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model_path, target)
    return target


def _game_ready_result(
    task: dict[str, Any],
    source_path: Path,
    *,
    provider: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    material = task.get("textureMaterial") if task["textureStrategy"]["mode"] == "material_only" else None
    output_format = task.get("outputFormat", task["modelFormat"])
    clean_path = _cleanup_with_blender(
        source_path,
        task["maxTriangles"],
        material,
        output_format if source_path.suffix[1:].lower() != output_format else None,
        task["normalPolicy"],
    )
    inspection = _blender_inspection(clean_path)
    if not inspection or not inspection.get("triangleBudgetPassed"):
        raise tool_error(MCP_ERROR, "Blender output exceeds triangle budget", provider="blender", status="quality_rejected")
    if not inspection.get("gameReadyPassed"):
        raise tool_error(MCP_ERROR, "Blender output failed the GameReady quality gate", provider="blender", status="quality_rejected")
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
        "provenance": completed["provenance"],
    }


async def _try_cc0(package: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    acquisition = await cc0_client.acquire(package["assetSpec"])
    status = acquisition.get("status", "provider_failed")
    if status != "found":
        return acquisition
    content = acquisition.pop("content")
    source_format = acquisition["format"]
    source_path = (
        _require_external_runtime_root()
        / "3d"
        / "downloads"
        / task["gameId"]
        / f"{acquisition['assetId']}.{source_format}"
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(content)
    task.update(
        {
            "provider": acquisition["provider"],
            "phase": "cc0_validation",
            "modelFormat": source_format,
            "taskId": f"cc0-{package['requestId']}",
        }
    )
    return _game_ready_result(
        task,
        source_path,
        provider=acquisition["provider"],
        provenance=acquisition["provenance"],
    )


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
            "message": "CC0 discovery is always attempted before any configured Meshy fallback.",
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
        "status": package["status"],
        "providerConfigured": meshy_client.is_configured(),
    }


@mcp.tool(description="Acquire CC0 first and submit to Meshy only after a verified not_found result.")
@expects_dict_return
async def submit_3d_asset_generation(
    featureId: str,
    assetSpec: dict[str, Any],
    referenceImageUrl: str = "",
    gameId: str | None = None,
) -> dict[str, Any]:
    _require_external_runtime_root()
    feature_id = _require_id(featureId, "featureId")
    game_id = _require_id(gameId or os.getenv("ASSET_DEFAULT_GAME_ID", "default"), "gameId")
    request_path, package, provenance = _request_package(feature_id, game_id, assetSpec)
    method = package["generationPrompt"]["method"]
    if method not in {"text_to_3d", "image_to_3d"}:
        raise tool_error(VALIDATION_ERROR, f"Meshy does not submit generation.method={method}")
    output_format = _meshy_format(provenance)
    model_format = "glb" if method == "image_to_3d" else output_format
    max_triangles = _meshy_triangle_limit(assetSpec, method)
    spec = _validate_asset_spec(assetSpec)
    task = {
        "provider": "cc0",
        "taskId": package["requestId"],
        "phase": "cc0_search",
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
        "createdAt": _now(),
    }
    cc0_result = await _try_cc0(package, task)
    cc0_status = cc0_result.get("status")
    if cc0_status == "SUCCEEDED":
        package.update({"status": "found", "provider": cc0_result["provider"], "provenance": cc0_result["provenance"]})
        _write_json(request_path, package)
        return {"requestId": package["requestId"], "cc0Status": "found", **cc0_result}
    if cc0_status != "not_found":
        package.update({"status": cc0_status, "cc0": cc0_result})
        _write_json(request_path, package)
        return {
            "requestId": package["requestId"],
            "status": cc0_status,
            "cc0Status": cc0_status,
            "provider": cc0_result.get("provider"),
            "reason": cc0_result.get("reason", ""),
        }
    if not meshy_client.is_configured():
        raise tool_error(
            MCP_ERROR,
            "CC0 search returned not_found and MESHY_API_KEY is not set",
            provider="meshy",
            status="provider_unconfigured",
            cc0Status="not_found",
        )
    submission_path = _require_external_runtime_root() / "3d" / "submissions" / f"{package['requestId']}.json"
    if submission_path.is_file():
        try:
            existing = json.loads(submission_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        existing_id = existing.get("taskId")
        if isinstance(existing_id, str) and existing_id:
            return {
                "requestId": package["requestId"],
                "taskId": existing_id,
                "status": "duplicate_blocked",
                "cc0Status": "not_found",
                "provider": "meshy",
            }
    meshy_prompt = ""
    texture_prompt = task["texturePrompt"]
    try:
        balance_before = await _await_result(meshy_client.get_balance())
        if method == "image_to_3d":
            credit_estimate = meshy_client.estimate_credits("image_smart_topology_untextured")
            task_id = await _await_result(meshy_client.create_image_task(
                _require_reference_image(referenceImageUrl, "referenceImageUrl"),
                model_format,
                max_triangles,
            ))
            phase = "generation"
        else:
            credit_estimate = meshy_client.estimate_credits("text_preview_meshy_6")
            meshy_prompt, texture_prompt = _meshy_prompts(assetSpec)
            task_id = await _await_result(meshy_client.create_text_preview(
                meshy_prompt, model_format, max_triangles
            ))
            phase = "preview"
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)

    task = {
        **task,
        "provider": "meshy",
        "taskId": task_id,
        "phase": phase,
        "method": method,
        "modelFormat": model_format,
        "outputFormat": output_format,
        "texturePrompt": texture_prompt,
        "providerPrompt": meshy_prompt,
        "topology": "triangle" if method == "text_to_3d" else "",
        "cc0Status": "not_found",
        "estimatedCredits": credit_estimate["credits"],
        "creditEstimate": credit_estimate,
        "balanceBefore": balance_before,
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
        "cc0Status": "not_found",
        "estimatedCredits": task["estimatedCredits"],
        "creditEstimate": task["creditEstimate"],
        "balanceBefore": balance_before,
    }


@mcp.tool(description="Refine a completed Meshy text-to-3D preview into a textured model.")
@expects_dict_return
async def refine_3d_asset_generation(taskId: str) -> dict[str, Any]:
    task_id = _require_id(taskId, "taskId")
    task = _read_task(task_id)
    if (task["method"], task["phase"]) not in {
        ("text_to_3d", "preview"),
        ("image_to_3d", "generation"),
    }:
        raise tool_error(VALIDATION_ERROR, "only completed Meshy geometry can be refined")
    try:
        current = await _await_result(meshy_client.get_task(task["method"], task_id))
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    if current.get("status") != "SUCCEEDED":
        return {
            "taskId": task_id,
            "phase": task["phase"],
            "status": current.get("status", "UNKNOWN"),
        }
    if (
        task["method"] == "text_to_3d"
        and task["textureStrategy"]["mode"] == "generated_texture"
    ):
        try:
            balance_before = await _await_result(meshy_client.get_balance())
            refine_task_id = await _await_result(meshy_client.create_text_refine(
                task_id, task["modelFormat"], task["texturePrompt"]
            ))
        except meshy_client.MeshyUnavailable as exc:
            _meshy_error(exc)
        estimate = meshy_client.estimate_credits("text_refine_2k")
        refined = {
            **task,
            "taskId": refine_task_id,
            "phase": "refine",
            "balanceBefore": balance_before,
            "estimatedCredits": estimate["credits"],
            "creditEstimate": estimate,
            "createdAt": _now(),
        }
    else:
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
                )
                inspection = _blender_inspection(final_path) or inspection
            final_hash = hashlib.sha256(final_path.read_bytes()).hexdigest()
            unity_path = _unity_import_path(task, final_path, final_hash)
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
            task.update({"status": "SUCCEEDED", "unityAssetPath": str(unity_path), "provenance": provenance})
            _write_json(_task_path(task_id), task)
            return {
                "taskId": task_id,
                "phase": (
                    "material"
                    if task["textureStrategy"]["mode"] == "material_only"
                    else "geometry"
                ),
                "provider": "blender",
                "status": "SUCCEEDED",
                "assetPath": str(final_path),
                "unityAssetPath": str(unity_path),
                "modelFormat": task.get("outputFormat", task["modelFormat"]),
                "inspection": inspection,
                "textureStrategy": task["textureStrategy"],
                "provenance": provenance,
            }
        try:
            balance_before = await _await_result(meshy_client.get_balance())
            refine_task_id = await _await_result(meshy_client.create_retexture_task(
                clean_model,
                task.get("outputFormat", task["modelFormat"]),
                task["texturePrompt"],
            ))
        except meshy_client.MeshyUnavailable as exc:
            _meshy_error(exc)
        refined = {
            **task,
            "taskId": refine_task_id,
            "method": "retexture",
            "modelFormat": task.get("outputFormat", task["modelFormat"]),
            "phase": "retexture",
            "sourceTaskId": task_id,
            "postprocessedPath": str(clean_path),
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
    try:
        current = await _await_result(meshy_client.get_task(task["method"], task_id))
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
    if task["phase"] in {"refine", "retexture"}:
        try:
            balance_after = await _await_result(meshy_client.get_balance())
        except meshy_client.MeshyUnavailable as exc:
            balance_after = {"error": str(exc), "status": exc.status}
        consumed_credits = _actual_credits(current, task.get("balanceBefore"), balance_after)
        finalized = _game_ready_result(
            task,
            output_path,
            provider="meshy",
            provenance={
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
            },
        )
        finalized.update(
            {
                "phase": task["phase"],
                "progress": current.get("progress"),
                "consumedCredits": consumed_credits,
            }
        )
        return finalized
    result.update(
        {
            "assetPath": str(output_path),
            "modelFormat": task["modelFormat"],
            "inspection": _inspect_model(
                output,
                task["modelFormat"],
                task["animationRequired"],
                task["phase"] in {"refine", "retexture"} and task["textureRequired"],
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
            "providerEvidence": current,
        }
    )
    _write_json(_task_path(task_id), task)
    return result


@mcp.tool(description="Cancel a resumable Meshy task by provider task id.")
@expects_dict_return
async def cancel_3d_asset_generation(taskId: str) -> dict[str, Any]:
    task_id = _require_id(taskId, "taskId")
    task = _read_task(task_id)
    try:
        evidence = await _await_result(meshy_client.cancel_task(task["method"], task_id))
    except meshy_client.MeshyUnavailable as exc:
        _meshy_error(exc)
    task.update({"status": "CANCELED", "canceledAt": _now(), "cancelEvidence": evidence})
    _write_json(_task_path(task_id), task)
    return {"taskId": task_id, "status": "CANCELED", "provider": "meshy"}


if __name__ == "__main__":
    serve(mcp)
