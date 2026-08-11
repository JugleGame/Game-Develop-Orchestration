"""Unit tests for the CodeGen node's error-feedback loop and shared context.

The retry loops (compile failure, QA failure) only converge if the previous
failure actually changes the next generation attempt — these tests pin that
``last_error`` is injected into the affected file's prompt and consumed, that
the retry repairs the previous source rather than rewriting from blank, and
that every call carries the game-wide context a lone ``create_script``
conversation cannot otherwise see.

They also pin the change made for ``Doc/설계/06`` §3.2: **the unit of work is a
file, not a feature.** One spec may become several files, so the properties that
used to be stated per feature are now stated per file, and the retry looks a
previous result up by path instead of by list position.
"""

from typing import Any

import pytest

from app.graph.nodes.codegen import build_codegen_node
from app.mcp.unity_client import CreatedScript

_FEATURES = [
    {"feature_id": "f-1", "title": "Jump", "description": "Add a jump ability."},
    {"feature_id": "f-2", "title": "Dash", "description": "Add a dash ability."},
]

_GAME_DESIGN: dict[str, Any] = {
    "genre": "platformer",
    "art_style": "pixel art",
    "core_mechanics": ["The player jumps.", "The player dashes."],
    "structure_overview": "One side-scrolling level.",
}

# f-1 becomes two files, f-2 one. That asymmetry is the point: it is exactly
# what the old per-feature loop could not express.
_PLANNED_FILES: list[dict[str, Any]] = [
    {
        "path": "Assets/Scripts/Player/JumpAbility.cs",
        "className": "JumpAbility",
        "kind": "MonoBehaviour",
        "featureIds": ["f-1"],
        "responsibility": "Apply upward impulse on the jump input.",
        "dependsOn": [],
    },
    {
        "path": "Assets/Scripts/Player/JumpTuning.cs",
        "className": "JumpTuning",
        "kind": "ScriptableObject",
        "featureIds": ["f-1"],
        "responsibility": "Hold jump height and gravity multiplier.",
        "dependsOn": [],
    },
    {
        "path": "Assets/Scripts/Player/DashAbility.cs",
        "className": "DashAbility",
        "kind": "MonoBehaviour",
        "featureIds": ["f-2"],
        "responsibility": "Move the player quickly along the facing direction.",
        "dependsOn": ["Assets/Scripts/Player/JumpAbility.cs"],
    },
]

_ARCHITECTURE: dict[str, Any] = {
    "files": _PLANNED_FILES,
    "prefabs": [],
    "scene": {"name": "Main", "objects": []},
    "typeMap": "Types in this game (all of them, decided up front):\n- JumpAbility …",
}


class _FakeUnityClient:
    """Records every ``create_script`` call, keyed by the file it wrote."""

    def __init__(self, architecture: dict[str, Any] | None = None) -> None:
        self._architecture = architecture if architecture is not None else _ARCHITECTURE
        self.design_calls = 0
        self.prompts: dict[str, str] = {}
        self.contents: dict[str, str] = {}
        self.context: dict[str, str] = {}
        self.existing: dict[str, list[str]] = {}
        self.previous: dict[str, str] = {}
        self.feature_ids: dict[str, str] = {}
        self.call_order: list[str] = []

    async def design_architecture(
        self, *, game_id: str, game_design: dict[str, Any], feature_prompts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.design_calls += 1
        return self._architecture

    async def create_script(
        self,
        *,
        feature_id: str,
        prompt: str,
        contents: str = "",
        project_context: str = "",
        existing_types: list[str] | None = None,
        previous_source: str = "",
        planned_path: str = "",
        planned_class: str = "",
    ) -> CreatedScript:
        self.prompts[planned_path] = prompt
        self.contents[planned_path] = contents
        self.context[planned_path] = project_context
        self.existing[planned_path] = list(existing_types or [])
        self.previous[planned_path] = previous_source
        self.feature_ids[planned_path] = feature_id
        self.call_order.append(planned_path)
        # The real server reports every type the file declares, not just one.
        return CreatedScript(
            file=planned_path,
            contents=f"// {planned_class}\nclass {planned_class} {{ }}",
            class_name=planned_class,
            types=(planned_class, f"{planned_class}Key"),
        )


def _state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "game_id": "game-a",
        "feature_prompts": _FEATURES,
        "game_design": _GAME_DESIGN,
    }
    base.update(overrides)
    return base


def _created(result: dict[str, Any], path: str) -> dict[str, Any]:
    return next(item for item in result["created_scripts"] if item["file"] == path)


# ---------------------------------------------------------------------------
# The design pass
# ---------------------------------------------------------------------------
async def test_one_spec_can_become_several_files():
    """The defect this whole change exists to fix."""

    client = _FakeUnityClient()
    result = await build_codegen_node(client)(_state())

    assert result["created_files"] == [
        "Assets/Scripts/Player/JumpAbility.cs",
        "Assets/Scripts/Player/JumpTuning.cs",
        "Assets/Scripts/Player/DashAbility.cs",
    ]
    # And no file is named after the spec that asked for it.
    assert not any("f-1" in path or "Spec" in path for path in result["created_files"])


async def test_design_is_made_once_and_reused_on_retry():
    """A plan re-decided each round gives a different structure each round."""

    client = _FakeUnityClient()
    node = build_codegen_node(client)

    first = await node(_state())
    assert client.design_calls == 1

    await node(_state(architecture=first["architecture"], last_error={"message": "boom"}))
    assert client.design_calls == 1, "재시도가 설계를 다시 뽑았다"


async def test_architecture_is_stored_for_the_next_round():
    client = _FakeUnityClient()
    result = await build_codegen_node(client)(_state())

    assert result["architecture"]["files"] == _PLANNED_FILES


async def test_files_are_generated_in_the_order_the_design_returned():
    """Dependencies come first; the design pass already sorted them."""

    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    order = client.call_order
    assert order.index("Assets/Scripts/Player/JumpAbility.cs") < order.index(
        "Assets/Scripts/Player/DashAbility.cs"
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
async def test_each_file_is_asked_for_its_own_responsibility_and_its_spec():
    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    prompt = client.prompts["Assets/Scripts/Player/JumpTuning.cs"]
    assert "Hold jump height and gravity multiplier." in prompt
    assert "Add a jump ability." in prompt, "spec 전문이 여전히 실려야 한다"
    assert "JumpTuning (ScriptableObject)" in prompt


async def test_a_files_prompt_does_not_carry_an_unrelated_spec():
    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    assert "Add a dash ability." not in client.prompts["Assets/Scripts/Player/JumpAbility.cs"]


# ---------------------------------------------------------------------------
# Error attribution
# ---------------------------------------------------------------------------
async def test_a_compile_error_regenerates_only_the_file_it_names():
    """파일 경로가 있으면 그 파일만 — 형제 파일까지 재추첨하지 않는다."""

    client = _FakeUnityClient()
    node = build_codegen_node(client)
    first = await node(_state())

    await node(
        _state(
            architecture=first["architecture"],
            created_scripts=first["created_scripts"],
            created_files=first["created_files"],
            last_error={
                "error_type": "compile",
                "message": "CS1002: ; expected",
                "file": "C:/proj/Assets/Scripts/Player/JumpTuning.cs",
                "line": 12,
                "suggested_fix": "Add the missing semicolon.",
                "related_feature_id": "f-1",
            },
        )
    )

    regenerated = client.call_order[len(_PLANNED_FILES) :]
    assert regenerated == ["Assets/Scripts/Player/JumpTuning.cs"]


async def test_an_error_without_a_path_falls_back_to_the_feature():
    client = _FakeUnityClient()
    node = build_codegen_node(client)
    first = await node(_state())

    await node(
        _state(
            architecture=first["architecture"],
            created_scripts=first["created_scripts"],
            last_error={
                "error_type": "logic",
                "message": "Jump never triggers",
                "file": None,
                "line": None,
                "suggested_fix": "Bind the jump input action in Awake().",
                "related_feature_id": "f-1",
            },
        )
    )

    regenerated = set(client.call_order[len(_PLANNED_FILES) :])
    assert regenerated == {
        "Assets/Scripts/Player/JumpAbility.cs",
        "Assets/Scripts/Player/JumpTuning.cs",
    }
    assert "Jump never triggers" in client.prompts["Assets/Scripts/Player/JumpAbility.cs"]
    assert "Bind the jump input action in Awake()." in client.prompts[
        "Assets/Scripts/Player/JumpAbility.cs"
    ]


async def test_an_error_naming_nothing_widens_to_every_file():
    client = _FakeUnityClient()
    node = build_codegen_node(client)
    first = await node(_state())

    await node(
        _state(
            architecture=first["architecture"],
            created_scripts=first["created_scripts"],
            last_error={
                "error_type": "compile",
                "message": "CS1002: ; expected",
                "file": None,
                "line": None,
                "suggested_fix": "Regenerate and rebuild.",
                "related_feature_id": "",
            },
        )
    )

    regenerated = client.call_order[len(_PLANNED_FILES) :]
    assert len(regenerated) == len(_PLANNED_FILES)


async def test_retry_reuses_files_not_implicated_by_the_error():
    client = _FakeUnityClient()
    node = build_codegen_node(client)
    first = await node(_state())

    result = await node(
        _state(
            architecture=first["architecture"],
            created_scripts=first["created_scripts"],
            last_error={
                "message": "boom",
                "file": "Assets/Scripts/Player/DashAbility.cs",
                "related_feature_id": "f-2",
            },
        )
    )

    reused = _created(result, "Assets/Scripts/Player/JumpAbility.cs")
    assert reused["generated"] is False
    assert reused["contents"], "재사용한 파일의 소스가 유실되면 다음 재시도가 백지에서 쓴다"
    assert _created(result, "Assets/Scripts/Player/DashAbility.cs")["generated"] is True


async def test_retry_regenerates_a_file_with_no_previous_pass():
    """이전 결과가 없으면 재사용할 것이 없으므로 다시 만든다."""

    client = _FakeUnityClient()
    node = build_codegen_node(client)

    result = await node(
        _state(
            architecture=_ARCHITECTURE,
            created_scripts=[],
            last_error={"message": "boom", "related_feature_id": "f-2"},
        )
    )

    assert all(item["generated"] for item in result["created_scripts"])


async def test_retry_hands_back_the_previous_source_for_repair():
    """백지 재작성은 고쳐진 곳 옆을 새로 깨뜨린다 — 그래서 수정으로 돈다."""

    client = _FakeUnityClient()
    node = build_codegen_node(client)
    first = await node(_state())

    await node(
        _state(
            architecture=first["architecture"],
            created_scripts=first["created_scripts"],
            last_error={
                "message": "boom",
                "file": "Assets/Scripts/Player/DashAbility.cs",
                "related_feature_id": "f-2",
            },
        )
    )

    assert "DashAbility" in client.previous["Assets/Scripts/Player/DashAbility.cs"]


async def test_first_pass_sends_no_previous_source():
    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    assert set(client.previous.values()) == {""}


async def test_last_error_is_consumed_so_it_never_leaks_into_the_next_loop():
    client = _FakeUnityClient()
    result = await build_codegen_node(client)(
        _state(architecture=_ARCHITECTURE, last_error={"message": "boom"})
    )

    assert result["last_error"] is None


async def test_a_state_without_types_still_reuses_the_previous_file():
    """``types`` 가 생기기 전에 체크포인트된 상태로도 재시도가 돌아야 한다."""

    client = _FakeUnityClient()
    node = build_codegen_node(client)

    result = await node(
        _state(
            architecture=_ARCHITECTURE,
            created_scripts=[
                {
                    "file": "Assets/Scripts/Player/JumpAbility.cs",
                    "contents": "// old\nclass JumpAbility { }",
                    "class_name": "JumpAbility",
                }
            ],
            last_error={"message": "boom", "related_feature_id": "f-2"},
        )
    )

    assert _created(result, "Assets/Scripts/Player/JumpAbility.cs")["generated"] is False


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------
async def test_every_file_gets_the_same_project_context():
    """캐시 접두사는 바이트가 같아야 성립한다 — 파일마다 다르면 매번 캐시 쓰기다."""

    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    assert len(set(client.context.values())) == 1


async def test_project_context_carries_the_whole_type_map_from_the_first_file():
    """첫 파일이 형제를 모른 채 쓰이던 문제를 없앤 것이 설계 패스의 목적 중 하나다."""

    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    first_context = client.context["Assets/Scripts/Player/JumpAbility.cs"]
    assert "Types in this game" in first_context
    assert "platformer" in first_context


async def test_existing_types_carries_every_type_a_file_declared():
    """파일당 첫 타입만 실으면 같은 파일의 형제 타입이 뒤에 안 보인다 (06 §2.1)."""

    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    seen_by_last = client.existing["Assets/Scripts/Player/DashAbility.cs"]
    assert "JumpAbility" in seen_by_last
    assert "JumpAbilityKey" in seen_by_last, "같은 파일의 형제 타입이 빠졌다"


async def test_first_file_sees_no_generated_types_yet():
    client = _FakeUnityClient()
    await build_codegen_node(client)(_state())

    assert client.existing["Assets/Scripts/Player/JumpAbility.cs"] == []


async def test_project_context_is_empty_when_the_game_is_unplanned():
    client = _FakeUnityClient(architecture={"files": _PLANNED_FILES, "typeMap": ""})
    await build_codegen_node(client)(_state(game_design=None))

    assert set(client.context.values()) == {""}


# ---------------------------------------------------------------------------
# Pre-written source (the keyless escape hatch)
# ---------------------------------------------------------------------------
async def test_a_planned_files_own_contents_are_forwarded():
    architecture = {
        "files": [{**_PLANNED_FILES[0], "contents": "// handed in\nclass JumpAbility { }"}],
        "typeMap": "",
    }
    client = _FakeUnityClient(architecture=architecture)
    await build_codegen_node(client)(_state())

    assert "handed in" in client.contents["Assets/Scripts/Player/JumpAbility.cs"]


async def test_a_features_contents_are_forwarded_when_it_owns_exactly_one_file():
    architecture = {"files": [_PLANNED_FILES[2]], "typeMap": ""}
    client = _FakeUnityClient(architecture=architecture)
    features = [_FEATURES[0], {**_FEATURES[1], "contents": "// from the feature"}]

    await build_codegen_node(client)(_state(feature_prompts=features))

    assert "from the feature" in client.contents["Assets/Scripts/Player/DashAbility.cs"]


async def test_a_features_contents_are_ignored_when_it_owns_several_files():
    """소스 한 덩어리를 파일 셋에 넣을 수는 없다 — 골라 쓰면 같은 파일을 세 번 쓴다."""

    client = _FakeUnityClient()
    features = [{**_FEATURES[0], "contents": "// from the feature"}, _FEATURES[1]]

    await build_codegen_node(client)(_state(feature_prompts=features))

    assert client.contents["Assets/Scripts/Player/JumpAbility.cs"] == ""
    assert client.contents["Assets/Scripts/Player/JumpTuning.cs"] == ""


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("counters,expected", [({}, 1), ({"dev_iteration_count": 2}, 3)])
async def test_attempt_counts_every_round_this_node_has_run(counters, expected):
    client = _FakeUnityClient()
    result = await build_codegen_node(client)(_state(**counters))

    assert result["codegen_attempt"] == expected


async def test_created_scripts_record_which_specs_each_file_serves():
    client = _FakeUnityClient()
    result = await build_codegen_node(client)(_state())

    assert _created(result, "Assets/Scripts/Player/JumpTuning.cs")["feature_ids"] == ["f-1"]
