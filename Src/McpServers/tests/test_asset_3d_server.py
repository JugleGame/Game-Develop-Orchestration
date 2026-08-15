"""Contract tests for the isolated, provider-neutral 3D asset boundary."""

import json
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client, ClientSession

from asset3d import cc0_client, meshy_client, server

REAL_CC0_ACQUIRE = cc0_client.acquire


@pytest.fixture(autouse=True)
def _isolate_external_providers(monkeypatch):
    async def no_cc0_match(_asset_spec):
        return {"status": "not_found", "provider": "poly_haven"}

    monkeypatch.setattr(cc0_client, "acquire", no_cc0_match)
    monkeypatch.setattr(meshy_client, "get_balance", lambda: {"balance": 1000})
    monkeypatch.setattr(server, "_unity_import_path", lambda _task, path, _sha: path)


def _glb(*, textured: bool = True) -> bytes:
    document = json.dumps(
        {
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
            "accessors": [{"count": 100}, {"count": 300}],
            "textures": [{}] if textured else [],
        }
    ).encode()
    document += b" " * (-len(document) % 4)
    return struct.pack("<IIIII", 0x46546C67, 2, 20 + len(document), len(document), 0x4E4F534A) + document


def _fbx(source: bytes) -> bytes:
    return b"Kaydara FBX Binary  \x00" + source


def _fake_cleanup(
    path, _target, _material=None, output_format=None, _normal_policy="mixed", preserve_uv=False
):
    """Stand in for Blender, including its GLB-to-FBX conversion step."""
    if not output_format or path.suffix.lower() == f".{output_format}":
        return path
    converted = path.with_name(f"{path.stem.removesuffix('-clean')}-clean.{output_format}")
    converted.write_bytes(_fbx(path.read_bytes()))
    return converted


def _asset_spec(asset_type: str = "slime", method: str = "image_to_3d") -> dict:
    animated = asset_type in {"character", "slime", "monster"}
    return {
        "assetId": f"{asset_type}-green",
        "assetName": f"Green {asset_type}",
        "assetType": asset_type,
        "gameplayRole": "a readable ranch encounter",
        "design": {
            "description": f"A rounded {asset_type} with a clear silhouette",
            "style": "cute stylized 3D",
            "proportions": "compact and broad",
            "colors": ["leaf green", "cream"],
            "form": {
                "silhouette": "a compact readable outline",
                "primaryVolumes": ["one rounded body"],
                "partRelationships": ["eyes remain attached to the front of the body"],
                "surfaceFeatures": ["preserve intentional recesses and protrusions"],
                "bevelPolicy": ["bevel only silhouette-defining hard edges"],
            },
            "materials": ["soft matte body"],
            "preserve": ["round silhouette"],
            "exclude": ["text", "weapons"],
        },
        "geometry": {
            "maxTriangles": 2500,
            "separateMeshes": ["body", "eyes"] if asset_type == "interactive" else [],
            "shading": {
                "normalPolicy": "explicit_hard" if asset_type in {"prop", "building"} else "mixed",
                "faceOrientation": "outward",
                "smoothingRules": ["split normals across silhouette-defining hard edges"],
            },
            "integrity": {
                "forbidDegenerateFaces": True,
                "forbidDuplicateFaces": True,
                "forbidCoplanarOverlaps": True,
                "preserveHardEdgeSplits": True,
            },
        },
        "output": {
            "format": "glb",
            "scale": 1.0,
            "pivot": "ground center",
            "pivotPolicy": "ground_center",
            "collider": "single capsule",
        },
        "texture": {
            "required": True,
            "description": "matte stylized surface with readable color separation",
            "maps": ["base color"],
        },
        "animation": {
            "required": animated,
            "rigType": "simple deform rig" if animated else "",
            "clips": ["idle", "move"] if animated else [],
        },
        "generation": {"method": method},
        "validation": {
            "requirements": ["silhouette matches the specification", "triangle budget passes"]
        },
    }


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with Client(server.mcp) as client:
        yield client


async def _compose(asset_spec: dict) -> dict:
    async with session() as client:
        result = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
    assert result.is_error is False
    return result.structured_content


async def test_exposes_all_3d_tools_with_camel_case_inputs():
    async with session() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert {
        "compose_3d_asset_prompts",
        "validate_3d_asset_prompts",
        "prepare_3d_asset_request",
        "submit_3d_asset_generation",
        "refine_3d_asset_generation",
        "get_3d_asset_generation",
        "finalize_3d_asset_generation",
    } <= set(tools)
    assert {"featureId", "assetSpec"} <= set(
        tools["prepare_3d_asset_request"].input_schema["properties"]
    )
    assert {"featureId", "assetSpec"} <= set(
        tools["submit_3d_asset_generation"].input_schema["properties"]
    )
    assert {"taskId"} <= set(tools["get_3d_asset_generation"].input_schema["properties"])
    assert {"referenceImageUrls", "referenceProvenance"} <= set(
        tools["submit_3d_asset_generation"].input_schema["properties"]
    )


@pytest.mark.parametrize(
    "asset_type",
    ["character", "slime", "monster", "prop", "environment", "building", "interactive"],
)
async def test_every_documented_asset_type_composes(asset_type):
    prompts = await _compose(_asset_spec(asset_type, "image_to_3d"))

    assert prompts["generationPrompt"]["assetId"] == f"{asset_type}-green"
    assert asset_type in prompts["generationPrompt"]["prompt"]


async def test_generation_prompt_follows_the_documented_order_and_content():
    prompt = (await _compose(_asset_spec()))["generationPrompt"]["prompt"]

    ordered = [
        "a readable ranch encounter",
        "clear silhouette",
        "cute stylized 3D",
        "leaf green",
        "soft matte body",
        "neutral pose",
        "2500 triangles",
        "export glb",
        "simple deform rig",
        "Preserve: round silhouette",
        "Exclude: text, weapons",
    ]
    positions = [prompt.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    assert "no degenerate or duplicate faces" in prompt
    assert "no coplanar overlaps" in prompt
    assert "preserve hard-edge vertex/normal splits" in prompt


async def test_image_to_3d_composes_consistent_front_side_and_back_views():
    reference = (await _compose(_asset_spec()))["referenceSearchPrompt"]

    assert reference["required"] is True
    assert reference["requiredViews"] == ["front", "side", "back"]
    assert set(reference["viewPrompts"]) == {"front", "side", "back"}
    for view, prompt in reference["viewPrompts"].items():
        assert view in prompt
        assert "Design for at most 2500 triangles" in prompt
        assert "tiny non-silhouette details as flat color or normal-map information" in prompt
        assert "neutral pose" in prompt
        assert "consistent lighting" in prompt
        assert "minimal perspective distortion" in prompt


async def test_text_to_3d_is_rejected_by_the_asset_contract():
    async with session() as client:
        result = await client.call_tool(
            "compose_3d_asset_prompts",
            {"assetSpec": _asset_spec("prop", "text_to_3d")},
        )

    assert result.is_error is True
    assert "generation.method" in "".join(
        getattr(block, "text", "") for block in result.content
    )


async def test_triangle_budget_stays_within_the_webgl_ceiling(monkeypatch):
    over_budget = _asset_spec("prop", "manual_blender")
    over_budget["geometry"]["maxTriangles"] = server.WEBGL_TRIANGLE_CEILING + 1

    async with session() as client:
        rejected = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": over_budget})
        monkeypatch.setenv("ASSET3D_WEBGL_MAX_TRIANGLES", "800")
        tightened = await client.call_tool(
            "compose_3d_asset_prompts", {"assetSpec": _asset_spec("prop", "manual_blender")}
        )

    assert rejected.is_error is True
    assert "WebGL budget" in "".join(getattr(block, "text", "") for block in rejected.content)
    assert tightened.is_error is True


async def test_character_requires_reference_views_even_for_manual_modeling():
    reference = (await _compose(_asset_spec("character", "manual_blender")))[
        "referenceSearchPrompt"
    ]
    assert reference["requiredViews"] == ["front", "side", "back"]


async def test_animation_fields_are_required_only_for_animated_assets():
    animated = _asset_spec()
    animated["animation"]["rigType"] = ""
    static = _asset_spec("prop", "image_to_3d")
    static["animation"] = {"required": False}

    async with session() as client:
        invalid = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": animated})
        valid = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": static})

    assert invalid.is_error is True
    assert '"errorCode": 1000' in "".join(
        getattr(block, "text", "") for block in invalid.content
    )
    assert valid.is_error is False


async def test_requires_concrete_form_texture_and_pivot_policy():
    asset_spec = _asset_spec("slime", "image_to_3d")
    del asset_spec["design"]["form"]
    async with session() as client:
        missing_form = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
    assert missing_form.is_error is True

    asset_spec = _asset_spec("prop", "image_to_3d")
    asset_spec["output"]["pivotPolicy"] = "guess"
    async with session() as client:
        bad_pivot = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
    assert bad_pivot.is_error is True

    asset_spec = _asset_spec("prop", "image_to_3d")
    asset_spec["geometry"]["shading"]["faceOrientation"] = "mixed"
    async with session() as client:
        bad_winding = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
    assert bad_winding.is_error is True


async def test_prompt_preserves_shape_surface_texture_and_origin_requirements():
    prompt = (await _compose(_asset_spec("prop", "image_to_3d")))["generationPrompt"]["prompt"]
    assert "Primary volumes: one rounded body" in prompt
    assert "Part relationships: eyes remain attached" in prompt
    assert "Surface features: preserve intentional recesses and protrusions" in prompt
    assert "Bevel only: bevel only silhouette-defining hard edges" in prompt
    assert "Texture: matte stylized surface" in prompt
    assert "Normals explicit_hard; faces outward" in prompt
    assert "pivot ground center (ground_center); preserve that origin after export" in prompt


def test_geometry_preview_accepts_an_untextured_unrigged_mesh():
    inspection = server._inspect_model(
        _glb(textured=False),
        "glb",
        animation_required=False,
        texture_required=False,
        max_triangles=2500,
    )

    assert inspection["meshes"] == 1
    assert inspection["textures"] == 0
    assert inspection["rigs"] == 0
    assert inspection["triangleBudgetPassed"] is True


async def test_validation_rejects_a_prompt_that_drifted_from_the_specification():
    asset_spec = _asset_spec()
    prompts = await _compose(asset_spec)
    prompts["generationPrompt"]["prompt"] += " Add a sword."

    async with session() as client:
        result = await client.call_tool(
            "validate_3d_asset_prompts",
            {
                "assetSpec": asset_spec,
                "generationPrompt": prompts["generationPrompt"],
                "referenceSearchPrompt": prompts["referenceSearchPrompt"],
            },
        )

    assert result.is_error is True
    assert '"errorCode": 1000' in "".join(
        getattr(block, "text", "") for block in result.content
    )


async def test_request_composes_and_preserves_prompts_without_a_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    asset_spec = _asset_spec()
    async with session() as client:
        result = await client.call_tool(
            "prepare_3d_asset_request",
            {"featureId": "slime-art", "gameId": "slime-ranch", "assetSpec": asset_spec},
        )

    assert result.is_error is False
    body = result.structured_content
    assert body["status"] == "prepared"
    assert body["providerConfigured"] is meshy_client.is_configured()
    assert "assetPath" not in body
    package = json.loads(Path(body["requestPath"]).read_text(encoding="utf-8"))
    assert package["assetSpec"] == asset_spec
    assert package["generationPrompt"]["sourceSpecSha256"] == package["provenance"][
        "sourceSpecSha256"
    ]
    assert package["referenceSearchPrompt"]["sourceSpecSha256"] == package["provenance"][
        "sourceSpecSha256"
    ]
    assert len(package["provenance"]["sourceSpecSha256"]) == 64


async def test_submit_requires_a_configured_meshy_key(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.delenv("MESHY_API_KEY", raising=False)

    async with session() as client:
        result = await client.call_tool(
            "submit_3d_asset_generation",
            {
                "featureId": "slime-art",
                "gameId": "slime-ranch",
                "assetSpec": _asset_spec("prop", "image_to_3d"),
                "referenceImageUrls": [
                    "data:image/png;base64,ZnJvbnQ=",
                    "data:image/png;base64,c2lkZQ==",
                    "data:image/png;base64,YmFjaw==",
                ],
                "referenceProvenance": {
                    "source": "gpt_image_api",
                    "humanApproved": True,
                },
            },
        )

    assert result.is_error is True
    assert '"errorCode": 3000' in "".join(getattr(block, "text", "") for block in result.content)


def test_meshy_multi_image_uses_official_endpoint(monkeypatch):
    created = []
    monkeypatch.setattr(
        meshy_client,
        "_create",
        lambda path, payload: created.append((path, payload)) or "task-multi-image",
    )

    meshy_client.create_multi_image_task(["front", "side", "back"], "glb", 5000)

    assert created == [
        (
            "/openapi/v1/multi-image-to-3d",
            {
                "image_urls": ["front", "side", "back"],
                "ai_model": "meshy-6",
                "should_texture": False,
                "should_remesh": True,
                "topology": "triangle",
                "target_polycount": 5000,
                "target_formats": ["glb"],
            },
        )
    ]


async def test_image_generation_accepts_data_uri_and_refines_with_blender(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    submitted = []
    retextured = []
    monkeypatch.setattr(
        meshy_client,
        "create_multi_image_task",
        lambda *args: submitted.append(args) or "task-image",
    )
    monkeypatch.setattr(
        meshy_client,
        "get_task",
        lambda *_: {
            "status": "SUCCEEDED",
            "model_urls": {"glb": "https://assets.meshy.ai/model.glb"},
        },
    )
    monkeypatch.setattr(meshy_client, "download_model", lambda _: _glb(textured=False))
    monkeypatch.setattr(server, "_cleanup_with_blender", _fake_cleanup)
    monkeypatch.setattr(
        meshy_client,
        "create_retexture_task",
        lambda *args: retextured.append(args) or "task-retexture",
    )

    async with session() as client:
        result = await client.call_tool(
            "submit_3d_asset_generation",
            {
                "featureId": "slime-art",
                "assetSpec": _asset_spec("prop", "image_to_3d"),
                "referenceImageUrls": ["data:image/png;base64,ZnJvbnQ=", "data:image/png;base64,c2lkZQ==", "data:image/png;base64,YmFjaw=="],
                "referenceProvenance": {
                    "source": "gpt_image_api",
                    "humanApproved": True,
                },
            },
        )
        refined = await client.call_tool(
            "refine_3d_asset_generation",
            {
                "taskId": result.structured_content["taskId"],
                "geometryReviewApproved": True,
            },
        )

    assert result.is_error is False
    assert submitted == [(
        ["data:image/png;base64,ZnJvbnQ=", "data:image/png;base64,c2lkZQ==", "data:image/png;base64,YmFjaw=="],
        "glb",
        2500,
    )]
    assert refined.structured_content["phase"] == "retexture"
    assert retextured[0][0] == _fbx(_glb(textured=False))
    assert retextured[0][1:] == (
        "fbx",
        "matte stylized surface with readable color separation; colors: leaf green, cream; materials: soft matte body",
    )


async def test_image_generation_requires_approved_reference_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    monkeypatch.setattr(
        meshy_client,
        "create_image_task",
        lambda *_: pytest.fail("unapproved references must not reach Meshy"),
    )

    async with session() as client:
        result = await client.call_tool(
            "submit_3d_asset_generation",
            {
                "featureId": "slime-art",
                "assetSpec": _asset_spec("prop", "image_to_3d"),
                "referenceImageUrl": "data:image/png;base64,aW1hZ2U=",
                "referenceProvenance": {
                    "source": "gpt_image_api",
                    "humanApproved": False,
                },
            },
        )

    assert result.is_error is True
    assert "humanApproved" in "".join(getattr(block, "text", "") for block in result.content)


async def test_static_image_generation_requires_three_distinct_references(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    monkeypatch.setattr(
        meshy_client,
        "create_image_task",
        lambda *_: pytest.fail("single static reference must not reach Meshy"),
    )

    async with session() as client:
        result = await client.call_tool(
            "submit_3d_asset_generation",
            {
                "featureId": "prop-art",
                "assetSpec": _asset_spec("prop", "image_to_3d"),
                "referenceImageUrl": "data:image/png;base64,aW1hZ2U=",
                "referenceProvenance": {
                    "source": "gpt_image_api",
                    "humanApproved": True,
                },
            },
        )

    assert result.is_error is True
    assert "exactly three approved front, side, and back references" in "".join(
        getattr(block, "text", "") for block in result.content
    )


async def test_multiple_approved_references_use_multi_image(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    submitted = []
    monkeypatch.setattr(
        meshy_client,
        "create_multi_image_task",
        lambda *args: submitted.append(args) or "task-multi-image",
    )
    references = [
        "data:image/png;base64,ZnJvbnQ=",
        "data:image/png;base64,c2lkZQ==",
        "data:image/png;base64,YmFjaw==",
    ]
    provenance = {
        "source": "gpt_image_api",
        "humanApproved": True,
        "sourcePromptSha256": "a" * 64,
    }

    async with session() as client:
        result = await client.call_tool(
            "submit_3d_asset_generation",
            {
                "featureId": "slime-art",
                "assetSpec": _asset_spec("prop", "image_to_3d"),
                "referenceImageUrls": references,
                "referenceProvenance": provenance,
            },
        )

    assert result.is_error is False
    assert submitted == [(references, "glb", 2500)]
    task = server._read_task("task-multi-image")
    assert task["providerMethod"] == "multi_image_to_3d"
    assert task["referenceImageCount"] == 3
    assert task["referenceProvenance"] == provenance
    assert task["estimatedCredits"] == 20


async def test_geometry_review_gate_blocks_refine(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    server._write_json(
        server._task_path("task-image"),
        {
            "provider": "meshy",
            "taskId": "task-image",
            "method": "image_to_3d",
            "phase": "generation",
        },
    )
    monkeypatch.setattr(
        meshy_client,
        "get_task",
        lambda *_: pytest.fail("review rejection must happen before provider access"),
    )

    async with session() as client:
        result = await client.call_tool(
            "refine_3d_asset_generation", {"taskId": "task-image"}
        )

    assert result.is_error is True
    assert "geometryReviewApproved" in "".join(
        getattr(block, "text", "") for block in result.content
    )


async def test_material_only_strategy_skips_meshy_retexture(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    asset_spec = _asset_spec("prop", "image_to_3d")
    asset_spec["design"]["colors"] = ["matte black"]
    asset_spec["design"]["materials"] = ["matte plastic"]
    asset_spec["output"]["format"] = "fbx"
    asset_spec["texture"].update(
        {
            "surfaceDetails": [],
            "material": {"baseColor": "#111111", "metallic": 0.15, "roughness": 0.65},
        }
    )
    monkeypatch.setattr(meshy_client, "create_multi_image_task", lambda *_: "task-image")
    monkeypatch.setattr(
        meshy_client,
        "get_task",
        lambda *_: {
            "status": "SUCCEEDED",
            "model_urls": {"glb": "https://assets.meshy.ai/model.glb"},
        },
    )
    monkeypatch.setattr(meshy_client, "download_model", lambda _: _glb(textured=False))
    cleanup_args = []

    def _record_cleanup(path, limit, material, output_format=None, *_, **__):
        cleanup_args.append((limit, material, output_format))
        return path

    monkeypatch.setattr(server, "_cleanup_with_blender", _record_cleanup)
    monkeypatch.setattr(
        meshy_client,
        "create_retexture_task",
        lambda *_: pytest.fail("material-only assets must not call Meshy Retexture"),
    )

    async with session() as client:
        composed = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
        submitted = await client.call_tool(
            "submit_3d_asset_generation",
            {
                "featureId": "laptop-art",
                "assetSpec": asset_spec,
                "referenceImageUrls": [
                    "data:image/png;base64,ZnJvbnQ=",
                    "data:image/png;base64,c2lkZQ==",
                    "data:image/png;base64,YmFjaw==",
                ],
                "referenceProvenance": {
                    "source": "gpt_image_api",
                    "humanApproved": True,
                },
            },
        )
        refined = await client.call_tool(
            "refine_3d_asset_generation",
            {
                "taskId": submitted.structured_content["taskId"],
                "geometryReviewApproved": True,
            },
        )

    assert composed.structured_content["textureStrategy"]["mode"] == "material_only"
    assert refined.structured_content["status"] == "AWAITING_FINAL_REVIEW"
    assert refined.structured_content["phase"] == "material_review"
    assert "unityAssetPath" not in refined.structured_content
    assert cleanup_args == [
        (2500, asset_spec["texture"]["material"], None),
        (2500, asset_spec["texture"]["material"], "fbx"),
    ]


async def test_final_review_gate_blocks_unity_and_approval_finalizes(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    source = tmp_path / "staged.glb"
    source.write_bytes(_glb(textured=True))
    server._write_json(
        server._task_path("task-final"),
        {
            "provider": "meshy",
            "taskId": "task-final",
            "method": "retexture",
            "phase": "retexture",
            "status": "AWAITING_FINAL_REVIEW",
            "assetPath": str(source),
            "modelFormat": "glb",
            "outputFormat": "glb",
            "maxTriangles": 2500,
            "normalPolicy": "mixed",
            "textureStrategy": {"mode": "generated_texture"},
            "provenance": {"license": "generated"},
        },
    )
    monkeypatch.setattr(server, "_cleanup_with_blender", lambda path, *_, **__: path)
    monkeypatch.setattr(
        server,
        "_blender_inspection",
        lambda _path: {"triangleBudgetPassed": True, "gameReadyPassed": True},
    )

    async with session() as client:
        blocked = await client.call_tool(
            "finalize_3d_asset_generation", {"taskId": "task-final"}
        )
        approved = await client.call_tool(
            "finalize_3d_asset_generation",
            {
                "taskId": "task-final",
                "finalVisualReviewApproved": True,
                "finalVisualReviewNote": "silhouette and materials approved",
            },
        )

    assert blocked.is_error is True
    assert approved.structured_content["status"] == "SUCCEEDED"
    assert server._read_task("task-final")["finalVisualReview"]["humanApproved"] is True


def test_multiple_color_or_material_regions_keep_generated_texture():
    asset_spec = _asset_spec("prop", "image_to_3d")
    asset_spec["texture"].update(
        {
            "surfaceDetails": [],
            "material": {"baseColor": "#111111", "metallic": 0.15, "roughness": 0.65},
        }
    )

    spec = server._validate_asset_spec(asset_spec)

    assert server._texture_strategy(spec)["mode"] == "generated_texture"


def test_packs_metallic_and_roughness_into_one_urp_map(tmp_path):
    """URP reads metallic from RGB and smoothness from alpha of a single map."""

    from PIL import Image

    model = tmp_path / "chest-clean.fbx"
    model.write_bytes(b"Kaydara FBX Binary  ")
    sidecar = tmp_path / "chest-clean.fbm"
    sidecar.mkdir()
    Image.new("L", (4, 4), 200).save(sidecar / "texture_0_metallic.png")
    Image.new("L", (4, 4), 30).save(sidecar / "texture_0_roughness.png")

    packed = server._pack_metallic_smoothness(model)

    assert packed == sidecar / "texture_0_metallicSmoothness.png"
    with Image.open(packed) as image:
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0)) == (200, 200, 200, 225)
    assert sorted(path.name for path in sidecar.iterdir()) == [
        "texture_0_metallicSmoothness.png"
    ]


def test_drops_an_emission_map_that_emits_nothing(tmp_path):
    from PIL import Image

    model = tmp_path / "chest-clean.fbx"
    model.write_bytes(b"Kaydara FBX Binary  ")
    sidecar = tmp_path / "chest-clean.fbm"
    sidecar.mkdir()
    # Providers return near-black rather than exactly black for an unused map.
    Image.new("RGB", (4, 4), (2, 1, 2)).save(sidecar / "texture_0_emission.png")
    Image.new("RGB", (4, 4), (10, 0, 0)).save(sidecar / "texture_1_emission.png")

    dropped = server._drop_empty_maps(model)

    assert dropped == ["texture_0_emission.png"]
    assert (sidecar / "texture_1_emission.png").is_file()


def test_metallic_pack_is_skipped_without_both_maps(tmp_path):
    model = tmp_path / "chest-clean.fbx"
    model.write_bytes(b"Kaydara FBX Binary  ")
    (tmp_path / "chest-clean.fbm").mkdir()

    assert server._pack_metallic_smoothness(model) is None


def test_reads_blender_game_ready_report(tmp_path):
    model = tmp_path / "asset.fbx"
    report = {
        "triangles": 999,
        "triangleBudgetPassed": True,
        "gameReadyPassed": True,
    }
    Path(f"{model}.report.json").write_text(json.dumps(report), encoding="utf-8")

    assert server._blender_inspection(model) == report


def test_blender_cleanup_converts_srgb_and_checks_material_slots():
    source = server.BLENDER_SCRIPT.read_text(encoding="utf-8")

    assert "from_srgb_to_scene_linear" in source
    assert 'result["materialPassed"]' in source
    assert 'and result["materialPassed"]' in source


def test_local_cc0_manifest_verifies_license_and_hash(tmp_path, monkeypatch):
    model = tmp_path / "pack" / "green-prop.glb"
    model.parent.mkdir()
    content = _glb(textured=True)
    model.write_bytes(content)
    manifest = tmp_path / "pack.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "id": "green-prop",
                        "name": "Green prop",
                        "tags": ["green", "prop"],
                        "path": str(model),
                        "format": "glb",
                        "license": "CC0-1.0",
                        "provider": "Kenney",
                        "sha256": __import__("hashlib").sha256(content).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CC0_MANIFEST_PATHS", str(manifest))

    result = cc0_client.search_local(_asset_spec("prop", "image_to_3d"))

    assert result["status"] == "found"
    assert result["provenance"]["license"] == "CC0-1.0"
    assert result["provenance"]["provider"] == "Kenney"


def test_local_manifest_distinguishes_license_and_hash_rejections(tmp_path, monkeypatch):
    model = tmp_path / "green-prop.glb"
    model.write_bytes(_glb())
    manifest = tmp_path / "pack.json"
    manifest.write_text(
        json.dumps({"assets": [{"name": "green prop", "path": str(model), "license": "CC-BY", "format": "glb"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CC0_MANIFEST_PATHS", str(manifest))
    assert cc0_client.search_local(_asset_spec("prop"))["status"] == "license_rejected"

    manifest.write_text(
        json.dumps({"assets": [{"name": "green prop", "path": str(model), "license": "CC0", "format": "glb", "sha256": "0" * 64}]}),
        encoding="utf-8",
    )
    assert cc0_client.search_local(_asset_spec("prop"))["status"] == "quality_rejected"


async def test_poly_haven_fake_response_found_and_not_found(monkeypatch):
    class Download:
        content = _glb()

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return Download()

    responses = {
        "/assets?t=models": {
            "green_prop": {"name": "Green prop", "tags": ["green", "prop"]}
        },
        "/files/green_prop": {
            "glb": {"1k": {"glb": {"url": "https://cdn.polyhaven.com/green_prop.glb"}}}
        },
    }

    async def fake_json(_client, path):
        return responses[path]

    monkeypatch.setattr(cc0_client.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(cc0_client, "_get_json", fake_json)

    found = await cc0_client.search_poly_haven(_asset_spec("prop"))
    responses["/assets?t=models"] = {}
    missing = await cc0_client.search_poly_haven(_asset_spec("prop"))

    assert found["status"] == "found"
    assert found["provenance"]["license"] == "CC0-1.0"
    assert missing["status"] == "not_found"


def test_cc0_candidate_requires_an_explicit_identity_word():
    spec = _asset_spec("prop", "image_to_3d")
    spec["assetId"] = "memory-chest"
    spec["assetName"] = "Memory Fragment Chest"
    spec["design"]["description"] = "A compact weathered chest with a large lid"

    wrong = {"id": "rusted_hacksaw", "name": "Rusted Hacksaw", "tags": ["weathered", "compact"]}
    right = {"id": "wooden_military_crate", "name": "Wooden Military Crate", "tags": ["chest", "weathered"]}

    assert cc0_client._candidate_score(spec, wrong)[0] == 0
    assert cc0_client._candidate_score(spec, right) > cc0_client._candidate_score(spec, wrong)


def test_cc0_candidate_requires_primary_noun_when_only_one_identity_word_matches():
    spec = _asset_spec("environment", "image_to_3d")
    spec["assetId"] = "bus-stop-shelter"
    spec["assetName"] = "Abandoned Bus Stop Shelter"

    wrong = {"id": "old_tyre", "name": "Old Tyre", "tags": ["door stop", "abandoned"]}
    right = {"id": "urban_bus_shelter", "name": "Urban Bus Shelter", "tags": ["street"]}

    assert cc0_client._candidate_score(spec, wrong)[0] == 0
    assert cc0_client._candidate_score(spec, right)[0] >= 1


async def test_poly_haven_ignores_texture_urls_nested_under_fbx(monkeypatch):
    downloaded = []

    class Download:
        content = b"fbx"

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            downloaded.append(url)
            return Download()

    responses = {
        "/assets?t=models": {"green_prop": {"name": "Green prop", "tags": ["green", "prop"]}},
        "/files/green_prop": {
            "fbx": {
                "1k": {
                    "fbx": {
                        "url": "https://cdn.polyhaven.com/green_prop.fbx",
                        "include": {"textures": {"normal": {"url": "https://cdn.polyhaven.com/green_prop_nor.exr"}}},
                    }
                }
            }
        },
    }

    async def fake_json(_client, path):
        return responses[path]

    monkeypatch.setattr(cc0_client.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(cc0_client, "_get_json", fake_json)

    found = await cc0_client.search_poly_haven(_asset_spec("prop"))

    assert found["status"] == "found"
    assert downloaded == ["https://cdn.polyhaven.com/green_prop.fbx"]
    assert found["format"] == "fbx"


async def test_poly_haven_network_error_is_provider_failed(monkeypatch):
    monkeypatch.delenv("CC0_MANIFEST_PATHS", raising=False)

    async def failed(_asset_spec):
        raise cc0_client.CC0ProviderError("offline")

    monkeypatch.setattr(cc0_client, "search_poly_haven", failed)

    result = await REAL_CC0_ACQUIRE(_asset_spec("prop"))

    assert result == {
        "status": "provider_failed",
        "provider": "poly_haven",
        "reason": "offline",
    }


@pytest.mark.parametrize(
    ("status_code", "message", "expected"),
    [
        (401, "unauthorized", "auth_failed"),
        (402, "payment required", "insufficient_credits"),
        (429, "rate limit", "rate_limited"),
        (429, "concurrent queue limit", "queue_limit"),
    ],
)
async def test_meshy_http_failures_have_distinct_states(
    monkeypatch, status_code, message, expected
):
    class Response:
        headers = {}
        text = message

        def json(self):
            return {"message": message}

        @property
        def status_code(self):
            return status_code

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            return Response()

    async def no_sleep(_delay):
        return None

    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    monkeypatch.setattr(meshy_client.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(meshy_client.asyncio, "sleep", no_sleep)

    with pytest.raises(meshy_client.MeshyUnavailable) as caught:
        await meshy_client._request_async("GET", "/test")

    assert caught.value.status == expected


async def test_duplicate_spec_blocks_second_paid_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    calls = []
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    monkeypatch.setattr(
        meshy_client,
        "create_image_task",
        lambda *_args: calls.append("submitted") or "task-image",
    )

    arguments = {
        "featureId": "slime-art",
        "gameId": "slime-ranch",
        "assetSpec": _asset_spec("slime", "image_to_3d"),
        "referenceImageUrl": "data:image/png;base64,aW1hZ2U=",
        "referenceProvenance": {
            "source": "gpt_image_api",
            "humanApproved": True,
        },
    }
    async with session() as client:
        first = await client.call_tool("submit_3d_asset_generation", arguments)
        second = await client.call_tool("submit_3d_asset_generation", arguments)

    assert first.structured_content["status"] == "submitted"
    assert second.structured_content["status"] == "duplicate_blocked"
    assert calls == ["submitted"]


async def test_cancel_persists_terminal_task_state(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    server._write_json(
        server._task_path("task-image"),
        {"provider": "meshy", "taskId": "task-image", "method": "image_to_3d"},
    )
    monkeypatch.setattr(meshy_client, "cancel_task", lambda *_args: {"result": "ok"})

    async with session() as client:
        result = await client.call_tool(
            "cancel_3d_asset_generation", {"taskId": "task-image"}
        )

    assert result.structured_content["status"] == "CANCELED"
    assert server._read_task("task-image")["status"] == "CANCELED"


def test_runtime_root_rejects_relative_and_repository_paths(monkeypatch):
    monkeypatch.setattr(server, "ROOT", server._DEFAULT_ROOT)
    monkeypatch.setenv("ASSET3D_RUN_ROOT", "relative/staging")
    with pytest.raises(Exception, match="absolute path"):
        server._require_external_runtime_root()

    monkeypatch.setenv("ASSET3D_RUN_ROOT", str(server.REPOSITORY_ROOT / "var" / "3d"))
    monkeypatch.setattr(server, "ROOT", server.REPOSITORY_ROOT / "var" / "3d")
    monkeypatch.setattr(server, "_DEFAULT_ROOT", server.REPOSITORY_ROOT / "var" / "3d")
    with pytest.raises(Exception, match="outside the orchestration repository"):
        server._require_external_runtime_root()
