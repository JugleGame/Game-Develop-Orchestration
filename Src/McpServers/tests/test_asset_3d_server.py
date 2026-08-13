"""Contract tests for the isolated, provider-neutral 3D asset boundary."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import Client, ClientSession

from asset3d import server


def _asset_spec() -> dict:
    return {
        "assetId": "slime-green",
        "assetType": "slime",
        "design": {"description": "Round green slime with a readable silhouette.", "style": "cute stylized"},
        "output": {"format": "glb", "maxTriangles": 2500},
    }


def _generation_prompt() -> dict:
    return {
        "assetId": "slime-green",
        "method": "image_to_3d",
        "prompt": "Round green slime, neutral pose, clean silhouette, white background.",
    }


def _search_prompt() -> dict:
    return {
        "assetId": "slime-green",
        "queries": ["cute stylized green slime turnaround"],
        "requiredViews": ["front", "side", "back"],
    }


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with Client(server.mcp) as client:
        yield client


async def test_exposes_isolated_3d_tools_with_camel_case_inputs():
    async with session() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert {"validate_3d_asset_prompts", "prepare_3d_asset_request"} <= set(tools)
    assert {"featureId", "assetSpec", "generationPrompt", "referenceSearchPrompt"} <= set(
        tools["prepare_3d_asset_request"].input_schema["properties"]
    )


async def test_validates_every_required_prompt_input():
    prompt = _generation_prompt()
    prompt["prompt"] = ""
    async with session() as client:
        result = await client.call_tool(
            "validate_3d_asset_prompts",
            {"assetSpec": _asset_spec(), "generationPrompt": prompt, "referenceSearchPrompt": _search_prompt()},
        )

    assert result.is_error is True
    assert '"errorCode": 1000' in "".join(getattr(block, "text", "") for block in result.content)


async def test_request_preserves_prompts_and_never_claims_a_2d_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    async with session() as client:
        result = await client.call_tool(
            "prepare_3d_asset_request",
            {
                "featureId": "slime-art",
                "gameId": "slime-ranch",
                "assetSpec": _asset_spec(),
                "generationPrompt": _generation_prompt(),
                "referenceSearchPrompt": _search_prompt(),
            },
        )

    assert result.is_error is False
    body = result.structured_content
    assert body["status"] == "provider_unconfigured"
    assert body["providerConfigured"] is False
    assert "assetPath" not in body
    package = json.loads(Path(body["requestPath"]).read_text(encoding="utf-8"))
    assert package["assetSpec"] == _asset_spec()
    assert package["generationPrompt"] == _generation_prompt()
    assert package["referenceSearchPrompt"] == _search_prompt()
    assert len(package["provenance"]["sourceSpecSha256"]) == 64


async def test_prompt_asset_ids_must_match_the_source_specification():
    prompt = _search_prompt()
    prompt["assetId"] = "other-slime"
    async with session() as client:
        result = await client.call_tool(
            "validate_3d_asset_prompts",
            {"assetSpec": _asset_spec(), "generationPrompt": _generation_prompt(), "referenceSearchPrompt": prompt},
        )

    assert result.is_error is True
    assert '"errorCode": 1000' in "".join(getattr(block, "text", "") for block in result.content)
