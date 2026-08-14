"""Contract tests for the isolated, provider-neutral 3D asset boundary."""

import json
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client, ClientSession

from asset3d import meshy_client, server


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
    } <= set(tools)
    assert {"featureId", "assetSpec"} <= set(
        tools["prepare_3d_asset_request"].input_schema["properties"]
    )
    assert {"featureId", "assetSpec"} <= set(
        tools["submit_3d_asset_generation"].input_schema["properties"]
    )
    assert {"taskId"} <= set(tools["get_3d_asset_generation"].input_schema["properties"])


@pytest.mark.parametrize(
    "asset_type",
    ["character", "slime", "monster", "prop", "environment", "building", "interactive"],
)
async def test_every_documented_asset_type_composes(asset_type):
    prompts = await _compose(_asset_spec(asset_type, "text_to_3d"))

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


async def test_text_to_3d_prop_does_not_require_reference_views():
    reference = (await _compose(_asset_spec("prop", "text_to_3d")))["referenceSearchPrompt"]

    assert reference == {
        "assetId": "prop-green",
        "sourceSpecSha256": reference["sourceSpecSha256"],
        "required": False,
        "queries": [],
        "requiredViews": [],
        "viewPrompts": {},
    }


async def test_character_requires_reference_views_even_for_manual_modeling():
    reference = (await _compose(_asset_spec("character", "manual_blender")))[
        "referenceSearchPrompt"
    ]
    assert reference["requiredViews"] == ["front", "side", "back"]


async def test_animation_fields_are_required_only_for_animated_assets():
    animated = _asset_spec()
    animated["animation"]["rigType"] = ""
    static = _asset_spec("prop", "text_to_3d")
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
    asset_spec = _asset_spec("prop", "text_to_3d")
    del asset_spec["design"]["form"]
    async with session() as client:
        missing_form = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
    assert missing_form.is_error is True

    asset_spec = _asset_spec("prop", "text_to_3d")
    asset_spec["output"]["pivotPolicy"] = "guess"
    async with session() as client:
        bad_pivot = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
    assert bad_pivot.is_error is True

    asset_spec = _asset_spec("prop", "text_to_3d")
    asset_spec["geometry"]["shading"]["faceOrientation"] = "mixed"
    async with session() as client:
        bad_winding = await client.call_tool("compose_3d_asset_prompts", {"assetSpec": asset_spec})
    assert bad_winding.is_error is True


async def test_prompt_preserves_shape_surface_texture_and_origin_requirements():
    prompt = (await _compose(_asset_spec("prop", "text_to_3d")))["generationPrompt"]["prompt"]
    assert "Primary volumes: one rounded body" in prompt
    assert "Part relationships: eyes remain attached" in prompt
    assert "Surface features: preserve intentional recesses and protrusions" in prompt
    assert "Bevel only: bevel only silhouette-defining hard edges" in prompt
    assert "Texture: matte stylized surface" in prompt
    assert "Normals explicit_hard; faces outward" in prompt
    assert "pivot ground center (ground_center); preserve that origin after export" in prompt


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
    assert body["status"] == "provider_unconfigured"
    assert body["providerConfigured"] is False
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
                "assetSpec": _asset_spec("prop", "text_to_3d"),
            },
        )

    assert result.is_error is True
    assert '"errorCode": 3000' in "".join(getattr(block, "text", "") for block in result.content)


def test_meshy_preview_uses_runtime_triangle_remesh(monkeypatch):
    created = []
    monkeypatch.setattr(
        meshy_client,
        "_create",
        lambda path, payload: created.append((path, payload)) or "task-preview",
    )

    meshy_client.create_text_preview("closed laptop", "glb", 5000)

    assert created[0][1] == {
        "mode": "preview",
        "prompt": "closed laptop",
        "model_type": "standard",
        "ai_model": "meshy-6",
        "should_remesh": True,
        "topology": "triangle",
        "target_polycount": 5000,
        "target_formats": ["glb"],
    }


async def test_submit_and_download_meshy_text_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    submitted_args = []
    monkeypatch.setattr(
        meshy_client,
        "create_text_preview",
        lambda *args: submitted_args.append(args) or "task-preview",
    )
    monkeypatch.setattr(
        meshy_client,
        "get_task",
        lambda *_: {
            "status": "SUCCEEDED",
            "progress": 100,
            "model_urls": {"glb": "https://assets.meshy.ai/model.glb"},
        },
    )
    monkeypatch.setattr(meshy_client, "download_model", lambda _: _glb(textured=False))
    asset_spec = _asset_spec("prop", "text_to_3d")

    async with session() as client:
        submitted = await client.call_tool(
            "submit_3d_asset_generation",
            {"featureId": "slime-art", "gameId": "slime-ranch", "assetSpec": asset_spec},
        )
        downloaded = await client.call_tool(
            "get_3d_asset_generation", {"taskId": submitted.structured_content["taskId"]}
        )

    assert submitted.structured_content["phase"] == "preview"
    assert downloaded.structured_content["status"] == "SUCCEEDED"
    prompt, model_format, max_triangles = submitted_args[0]
    assert len(prompt) <= server.MESHY_PROMPT_LIMIT
    assert "silhouette:" in prompt and "exclude: text, weapons" in prompt
    assert "preserve: round silhouette" in prompt
    assert (model_format, max_triangles) == ("glb", 2500)
    assert Path(downloaded.structured_content["assetPath"]).read_bytes() == _glb(textured=False)
    assert downloaded.structured_content["inspection"] == {
        "meshes": 1,
        "textures": 0,
        "rigs": 0,
        "vertices": 100,
        "triangles": 100,
        "triangleBudgetPassed": True,
    }


async def test_refine_passes_the_texture_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    monkeypatch.setattr(meshy_client, "create_text_preview", lambda *_: "task-preview")
    monkeypatch.setattr(meshy_client, "get_task", lambda *_: {"status": "SUCCEEDED"})
    refined_args = []
    monkeypatch.setattr(
        meshy_client,
        "create_text_refine",
        lambda *args: refined_args.append(args) or "task-refine",
    )

    async with session() as client:
        submitted = await client.call_tool(
            "submit_3d_asset_generation",
            {"featureId": "slime-art", "assetSpec": _asset_spec("prop", "text_to_3d")},
        )
        refined = await client.call_tool(
            "refine_3d_asset_generation", {"taskId": submitted.structured_content["taskId"]}
        )

    assert refined.structured_content["phase"] == "refine"
    assert refined_args == [
        ("task-preview", "glb", "matte stylized surface with readable color separation; colors: leaf green, cream; materials: soft matte body")
    ]


async def test_image_generation_accepts_data_uri_and_refines_with_blender(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    submitted = []
    retextured = []
    monkeypatch.setattr(
        meshy_client,
        "create_image_task",
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
    monkeypatch.setattr(server, "_cleanup_with_blender", lambda path, *_: path)
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
                "referenceImageUrl": "data:image/png;base64,aW1hZ2U=",
            },
        )
        refined = await client.call_tool(
            "refine_3d_asset_generation", {"taskId": result.structured_content["taskId"]}
        )

    assert result.is_error is False
    assert submitted == [("data:image/png;base64,aW1hZ2U=", "glb", 2500)]
    assert refined.structured_content["phase"] == "retexture"
    assert retextured[0][0] == _glb(textured=False)
    assert retextured[0][1:] == (
        "glb",
        "matte stylized surface with readable color separation; colors: leaf green, cream; materials: soft matte body",
    )


async def test_material_only_strategy_skips_meshy_retexture(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setenv("MESHY_API_KEY", "test-key")
    asset_spec = _asset_spec("prop", "image_to_3d")
    asset_spec["output"]["format"] = "fbx"
    asset_spec["texture"].update(
        {
            "surfaceDetails": [],
            "material": {"baseColor": "#111111", "metallic": 0.15, "roughness": 0.65},
        }
    )
    monkeypatch.setattr(meshy_client, "create_image_task", lambda *_: "task-image")
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
    monkeypatch.setattr(
        server,
        "_cleanup_with_blender",
        lambda path, limit, material, *output_format: cleanup_args.append(
            (limit, material, output_format[0] if output_format else None)
        )
        or path,
    )
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
                "referenceImageUrl": "data:image/png;base64,aW1hZ2U=",
            },
        )
        refined = await client.call_tool(
            "refine_3d_asset_generation", {"taskId": submitted.structured_content["taskId"]}
        )

    assert composed.structured_content["textureStrategy"]["mode"] == "material_only"
    assert refined.structured_content["status"] == "SUCCEEDED"
    assert refined.structured_content["phase"] == "material"
    assert cleanup_args == [
        (2500, asset_spec["texture"]["material"], None),
        (2500, asset_spec["texture"]["material"], "fbx"),
    ]


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
