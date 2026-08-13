"""Contract tests for the isolated, provider-neutral 3D asset boundary."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client, ClientSession

from asset3d import server


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
            "materials": ["soft matte body"],
            "preserve": ["round silhouette"],
            "exclude": ["text", "weapons"],
        },
        "geometry": {
            "maxTriangles": 2500,
            "separateMeshes": ["body", "eyes"] if asset_type == "interactive" else [],
        },
        "output": {
            "format": "glb",
            "scale": 1.0,
            "pivot": "ground center",
            "collider": "single capsule",
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
    } <= set(tools)
    assert {"featureId", "assetSpec"} <= set(
        tools["prepare_3d_asset_request"].input_schema["properties"]
    )


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


async def test_image_to_3d_composes_consistent_front_side_and_back_views():
    reference = (await _compose(_asset_spec()))["referenceSearchPrompt"]

    assert reference["required"] is True
    assert reference["requiredViews"] == ["front", "side", "back"]
    assert set(reference["viewPrompts"]) == {"front", "side", "back"}
    for view, prompt in reference["viewPrompts"].items():
        assert view in prompt
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
