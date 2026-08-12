"""``design_architecture`` 를 실제 MCP 세션으로 태운다.

검증 규칙 자체는 ``test_architecture_design.py`` 가 본다. 여기서는 **도구 경계**를
본다 — 인자 이름(camelCase), 오류 코드, 호스트가 넘긴 설계의 검증 여부를 고정한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp import Client

from unity.server import mcp  # noqa: E402

_FEATURES = [
    {"feature_id": "spec-001", "title": "청크 로더", "description": "청크를 스트리밍한다."},
]


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with Client(mcp) as client:
        yield client


def _valid_design() -> dict[str, Any]:
    return {
        "files": [
            {
                "path": "Assets/Scripts/World/ChunkLoader.cs",
                "className": "ChunkLoader",
                "kind": "MonoBehaviour",
                "featureIds": ["spec-001"],
                "responsibility": "청크를 로드하고 해제한다.",
                "dependsOn": [],
            }
        ],
        "prefabs": [],
        "scene": {
            "name": "Main",
            "objects": [
                {"name": "WorldRoot", "parent": "", "components": ["ChunkLoader"], "prefab": ""}
            ],
        },
        "notes": [],
    }


async def test_host_design_is_validated():

    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {
                "gameId": "g1",
                "featurePrompts": _FEATURES,
                "design": _valid_design(),
            },
        )

    assert result.is_error is False, result.content
    body = result.structured_content
    assert body["gameId"] == "g1"
    assert body["files"][0]["className"] == "ChunkLoader"
    assert "ChunkLoader" in body["typeMap"]
    assert "usage" not in body


async def test_invalid_host_design_is_rejected():

    broken = _valid_design()
    broken["files"][0]["className"] = "Spec001"
    broken["files"][0]["path"] = "Assets/Scripts/World/Spec001.cs"
    broken["scene"]["objects"][0]["components"] = ["Spec001"]

    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {
                "gameId": "g1",
                "featurePrompts": _FEATURES,
                "design": broken,
            },
        )

    assert result.is_error is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert '"errorCode": 1000' in text
    assert "Spec001" in text


async def test_design_is_required():
    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {"gameId": "g1", "featurePrompts": _FEATURES},
        )

    assert result.is_error is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "design" in text


async def test_empty_feature_prompts_is_a_validation_error():
    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {"gameId": "g1", "featurePrompts": [], "design": _valid_design()},
        )

    assert result.is_error is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


async def test_feature_prompt_without_an_id_is_rejected():
    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {
                "gameId": "g1",
                "featurePrompts": [{"title": "이름만 있는 feature"}],
                "design": _valid_design(),
            },
        )

    assert result.is_error is True
    assert "feature_id" in "".join(getattr(b, "text", "") for b in result.content)
