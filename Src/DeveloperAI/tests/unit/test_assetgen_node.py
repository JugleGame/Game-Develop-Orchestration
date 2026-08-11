"""Unit tests for the AssetGen node's generate-then-import contract.

Beyond the import wiring, these pin the two identities that keep separate
games from colliding: every generation call carries ``game_id``, and the
game's art style is locked *before* the first one (AssetGenMcpServer freezes
a palette on first touch, so a later lock is a no-op).

They also pin the routing added for ``05_계약_변경_제안서`` §4.4: a feature's
``assets_needed`` entries each become their own request, and each goes to the
tool that matches what was asked for.
"""

from typing import Any

from app.graph.nodes.assetgen import build_assetgen_node, select_asset_tool

_FEATURES = [
    {"feature_id": "f-1", "title": "Jump", "description": "Add a jump ability."},
    {"feature_id": "f-2", "title": "Dash", "description": "Add a dash ability."},
]

_STATE: dict[str, Any] = {
    "game_id": "game-a",
    "game_design": {"genre": "Platformer", "art_style": "dark fantasy"},
    "feature_prompts": _FEATURES,
}


class _FakeAssetClient:
    """Records every call. ``path_by_feature`` maps a feature to the path its
    generations return; ``None`` means the server produced no ``assetPath``."""

    def __init__(self, *, path_by_feature: dict[str, str | None]) -> None:
        self._path_by_feature = path_by_feature
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def establish_art_style(self, *, game_id: str, art_style: str = "") -> dict:
        self.calls.append(("establish_art_style", {"game_id": game_id, "art_style": art_style}))
        return {"gameId": game_id, "artStyle": art_style, "palette": []}

    def _record(
        self, tool: str, feature_id: str, prompt: str, game_id: str, art_style: str
    ) -> dict:
        self.calls.append(
            (
                tool,
                {
                    "feature_id": feature_id,
                    "prompt": prompt,
                    "game_id": game_id,
                    "art_style": art_style,
                },
            )
        )
        path = self._path_by_feature[feature_id]
        body: dict = {"success": True}
        if path is not None:
            body["assetPath"] = path
        return body

    async def generate_2d_sprite(
        self, *, feature_id: str, prompt: str, game_id: str = "", art_style: str = ""
    ) -> dict:
        return self._record("generate_2d_sprite", feature_id, prompt, game_id, art_style)

    async def generate_ui_asset(
        self, *, feature_id: str, prompt: str, game_id: str = "", art_style: str = ""
    ) -> dict:
        return self._record("generate_ui_asset", feature_id, prompt, game_id, art_style)

    async def generate_3d_placeholder(
        self, *, feature_id: str, prompt: str, game_id: str = "", art_style: str = ""
    ) -> dict:
        return self._record("generate_3d_placeholder", feature_id, prompt, game_id, art_style)


class _FakeUnityClient:
    def __init__(self) -> None:
        self.imported: list[tuple[str, str]] = []

    async def import_asset(self, *, feature_id: str, asset_path: str) -> dict:
        self.imported.append((feature_id, asset_path))
        return {"success": True}


def _asset_client() -> _FakeAssetClient:
    return _FakeAssetClient(
        path_by_feature={"f-1": "Assets/Sprites/f-1.png", "f-2": "Assets/Sprites/f-2.png"}
    )


async def test_assetgen_imports_every_generated_asset():
    unity = _FakeUnityClient()
    node = build_assetgen_node(_asset_client(), unity)

    result = await node(dict(_STATE))

    assert unity.imported == [
        ("f-1", "Assets/Sprites/f-1.png"),
        ("f-2", "Assets/Sprites/f-2.png"),
    ]
    assert len(result["generated_assets"]) == 2


async def test_assetgen_skips_import_when_no_asset_path_returned():
    unity = _FakeUnityClient()
    node = build_assetgen_node(
        _FakeAssetClient(path_by_feature={"f-1": None, "f-2": "Assets/Sprites/f-2.png"}),
        unity,
    )

    await node(dict(_STATE))

    assert unity.imported == [("f-2", "Assets/Sprites/f-2.png")]


async def test_assetgen_sends_game_id_with_every_generation():
    """Omitting it makes two games share one manifest, and the later one wins."""

    asset = _asset_client()
    await build_assetgen_node(asset, _FakeUnityClient())(dict(_STATE))

    generations = [args for name, args in asset.calls if name == "generate_2d_sprite"]
    assert len(generations) == 2
    assert all(args["game_id"] == "game-a" for args in generations)
    assert all(args["art_style"] == "dark fantasy" for args in generations)


async def test_assetgen_locks_art_style_before_the_first_generation():
    """The lock is a no-op once a game's palette has been touched, so order is
    the whole point of the call."""

    asset = _asset_client()
    await build_assetgen_node(asset, _FakeUnityClient())(dict(_STATE))

    assert asset.calls[0] == (
        "establish_art_style",
        {"game_id": "game-a", "art_style": "dark fantasy"},
    )


async def test_assetgen_skips_the_style_lock_without_a_game_id():
    """Nothing to lock, and the server's fallback resolves to the same value."""

    asset = _asset_client()
    await build_assetgen_node(asset, _FakeUnityClient())({"feature_prompts": _FEATURES})

    assert [name for name, _ in asset.calls] == ["generate_2d_sprite", "generate_2d_sprite"]


# ---------------------------------------------------------------------------
# assets_needed routing (05_계약_변경_제안서 §4.4)
# ---------------------------------------------------------------------------
class TestSelectAssetTool:
    def test_plain_world_objects_go_to_the_2d_tool(self) -> None:
        for description in ("플레이어 스프라이트", "grass terrain tile", "a rock"):
            assert select_asset_tool(description) == "generate_2d_sprite"

    def test_ui_wording_goes_to_the_ui_tool(self) -> None:
        for description in ("체력 HUD", "재시작 버튼", "inventory panel", "스킬 아이콘"):
            assert select_asset_tool(description) == "generate_ui_asset"

    def test_3d_wins_over_ui(self) -> None:
        """"3D 인벤토리 아이콘" is a 3D request that happens to mention UI, and the
        UI tool would render it flat without saying so."""

        assert select_asset_tool("3D 인벤토리 아이콘") == "generate_3d_placeholder"
        assert select_asset_tool("3D mesh for the boss") == "generate_3d_placeholder"

    def test_matching_is_case_insensitive(self) -> None:
        assert select_asset_tool("A 3D Mesh") == "generate_3d_placeholder"
        assert select_asset_tool("Inventory PANEL") == "generate_ui_asset"


class TestAssetsNeededRouting:
    _STATE_WITH_HINTS: dict[str, Any] = {
        "game_id": "game-a",
        "game_design": {"art_style": "dark fantasy"},
        "feature_prompts": [
            {
                "feature_id": "f-1",
                "title": "Jump",
                "description": "Add a jump ability. 재시작 버튼도 있다.",
                "assets_needed": ["플레이어 스프라이트", "체력 HUD 패널", "3D 보스 메시"],
            }
        ],
    }

    async def test_each_named_asset_becomes_its_own_request(self) -> None:
        asset = _FakeAssetClient(path_by_feature={"f-1": "Assets/Sprites/f-1.png"})

        await build_assetgen_node(asset, _FakeUnityClient())(dict(self._STATE_WITH_HINTS))

        assert [name for name, _ in asset.calls] == [
            "establish_art_style",
            "generate_2d_sprite",
            "generate_ui_asset",
            "generate_3d_placeholder",
        ]

    async def test_the_named_asset_is_the_prompt_not_the_whole_spec(self) -> None:
        """The description mentions a button; sending it whole is what made the
        server classify a platformer feature as a UI button."""

        asset = _FakeAssetClient(path_by_feature={"f-1": "Assets/Sprites/f-1.png"})

        await build_assetgen_node(asset, _FakeUnityClient())(dict(self._STATE_WITH_HINTS))

        prompts = [args["prompt"] for name, args in asset.calls if name.startswith("generate_")]
        assert prompts == ["플레이어 스프라이트", "체력 HUD 패널", "3D 보스 메시"]

    async def test_every_named_asset_is_imported(self) -> None:
        unity = _FakeUnityClient()
        asset = _FakeAssetClient(path_by_feature={"f-1": "Assets/Sprites/f-1.png"})

        await build_assetgen_node(asset, unity)(dict(self._STATE_WITH_HINTS))

        assert unity.imported == [("f-1", "Assets/Sprites/f-1.png")] * 3

    async def test_blank_entries_are_dropped(self) -> None:
        """An empty prompt makes the server classify against nothing and emit a
        default silhouette nobody asked for."""

        asset = _FakeAssetClient(path_by_feature={"f-1": "Assets/Sprites/f-1.png"})
        state = {
            "game_id": "game-a",
            "game_design": {"art_style": "dark fantasy"},
            "feature_prompts": [
                {
                    "feature_id": "f-1",
                    "title": "Jump",
                    "description": "Add a jump ability.",
                    "assets_needed": ["", "   ", "플레이어 스프라이트"],
                }
            ],
        }

        await build_assetgen_node(asset, _FakeUnityClient())(state)

        prompts = [args["prompt"] for name, args in asset.calls if name.startswith("generate_")]
        assert prompts == ["플레이어 스프라이트"]

    async def test_an_all_blank_hint_list_falls_back_to_the_description(self) -> None:
        """A malformed hint list must not cost the feature its sprite."""

        asset = _FakeAssetClient(path_by_feature={"f-1": "Assets/Sprites/f-1.png"})
        state = {
            "game_id": "game-a",
            "game_design": {"art_style": "dark fantasy"},
            "feature_prompts": [
                {
                    "feature_id": "f-1",
                    "title": "Jump",
                    "description": "Add a jump ability.",
                    "assets_needed": ["", "  "],
                }
            ],
        }

        await build_assetgen_node(asset, _FakeUnityClient())(state)

        assert [name for name, _ in asset.calls] == ["establish_art_style", "generate_2d_sprite"]
        assert asset.calls[1][1]["prompt"] == "Add a jump ability."
