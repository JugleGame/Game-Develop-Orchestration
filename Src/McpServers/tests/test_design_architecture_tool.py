"""``design_architecture`` 를 실제 MCP 세션으로 태운다.

검증 규칙 자체는 ``test_architecture_design.py`` 가 본다. 여기서는 **도구 경계**를
본다 — 인자 이름(camelCase), 오류 코드, 그리고 CLAUDE.md 가 정한 "키 없이 쓰는
인자"가 실제로 모델을 부르지 않는지.

마지막 것이 특히 중요하다. ``design`` 우회로가 검증을 건너뛰면 경로 B 는 규칙을
지키지 않는 설계안을 그대로 통과시키게 되고, 두 경로가 서로 다른 산출물을 내게
된다. 그래서 여기서 **넘긴 값도 거부되는지**를 함께 고정한다.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

os.environ.setdefault("UNITY_PROJECT_PATH", "C:/nonexistent-unity-project")

from unity.server import mcp  # noqa: E402

_GAME_DESIGN = {
    "game_id": "g1",
    "genre": "2D 오픈월드",
    "core_mechanics": ["탐험", "채집"],
    "art_style": "pixel art",
    "target_platform": "PC",
    "structure_overview": "청크 기반 월드",
}

_FEATURES = [
    {"feature_id": "spec-001", "title": "청크 로더", "description": "청크를 스트리밍한다."},
]


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
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


@pytest.fixture
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


async def test_supplied_design_needs_no_key(no_api_key):
    """완성된 설계안을 넘기면 모델을 부르지 않는다 (CLAUDE.md 의 경로 B 규칙)."""

    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {
                "gameId": "g1",
                "gameDesign": _GAME_DESIGN,
                "featurePrompts": _FEATURES,
                "design": _valid_design(),
            },
        )

    assert result.isError is False, result.content
    body = result.structuredContent
    assert body["gameId"] == "g1"
    assert body["files"][0]["className"] == "ChunkLoader"
    # 첫 파일부터 전체 타입을 보게 하는 캐시 접두사.
    assert "ChunkLoader" in body["typeMap"]
    # 지출이 0 이므로 usage 를 싣지 않는다.
    assert "usage" not in body


async def test_supplied_design_is_validated_just_like_a_generated_one(no_api_key):
    """우회로가 검증에 구멍을 내면 두 경로가 다른 산출물을 낸다."""

    broken = _valid_design()
    broken["files"][0]["className"] = "Spec001"
    broken["files"][0]["path"] = "Assets/Scripts/World/Spec001.cs"
    broken["scene"]["objects"][0]["components"] = ["Spec001"]

    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {
                "gameId": "g1",
                "gameDesign": _GAME_DESIGN,
                "featurePrompts": _FEATURES,
                "design": broken,
            },
        )

    assert result.isError is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert '"errorCode": 1000' in text
    assert "Spec001" in text


async def test_missing_api_key_names_the_argument_that_avoids_it(no_api_key):
    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {"gameId": "g1", "gameDesign": _GAME_DESIGN, "featurePrompts": _FEATURES},
        )

    assert result.isError is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "ANTHROPIC_API_KEY" in text
    assert "design" in text


async def test_empty_feature_prompts_is_a_validation_error():
    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {"gameId": "g1", "gameDesign": _GAME_DESIGN, "featurePrompts": []},
        )

    assert result.isError is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


async def test_feature_prompt_without_an_id_is_rejected():
    async with session() as client:
        result = await client.call_tool(
            "design_architecture",
            {
                "gameId": "g1",
                "gameDesign": _GAME_DESIGN,
                "featurePrompts": [{"title": "이름만 있는 feature"}],
            },
        )

    assert result.isError is True
    assert "feature_id" in "".join(getattr(b, "text", "") for b in result.content)
