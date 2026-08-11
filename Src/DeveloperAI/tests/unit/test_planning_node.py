"""Unit tests for the Planning node's repo_name compute-once-then-preserve rule."""

from app.graph.nodes.planning import build_planning_node
from app.models.schemas import GameDesignDocument


class _FakeStrategicClient:
    def __init__(self, *, genre: str) -> None:
        self._genre = genre
        self.received_prompts: list[str] = []

    async def generate_game_design(self, *, prompt: str):
        self.received_prompts.append(prompt)
        design = GameDesignDocument(
            game_id="g1",
            genre=self._genre,
            core_mechanics=["jump"],
            art_style="pixel",
            target_platform="PC",
            structure_overview="one level",
            created_at="2026-07-15T00:00:00Z",
        )
        return design, []


async def test_planning_computes_repo_name_when_absent():
    node = build_planning_node(_FakeStrategicClient(genre="Platformer"))

    result = await node({"game_id": "3f9a1c2e-aaaa", "prompt": "Double Jump Hero!"})

    assert result["repo_name"] == "platformer-double-jump-hero-3f9a1c2e"


async def test_planning_preserves_existing_repo_name_on_revise():
    node = build_planning_node(_FakeStrategicClient(genre="Platformer"))

    result = await node(
        {
            "game_id": "3f9a1c2e-aaaa",
            "prompt": "add a double-jump ability",
            "repo_name": "platformer-double-jump-hero-3f9a1c2e",
        }
    )

    # Even though the revise prompt is completely different, the repo name
    # already assigned to this game must not change.
    assert result["repo_name"] == "platformer-double-jump-hero-3f9a1c2e"


async def test_planning_injects_rejection_feedback_into_prompt():
    client = _FakeStrategicClient(genre="Platformer")
    node = build_planning_node(client)

    result = await node(
        {
            "game_id": "3f9a1c2e-aaaa",
            "prompt": "Double Jump Hero!",
            "planning_feedback": "Make it a puzzle game instead of an action game.",
        }
    )

    sent = client.received_prompts[-1]
    assert "Double Jump Hero!" in sent
    assert "Make it a puzzle game instead of an action game." in sent
    # Feedback is consumed so an unrelated later re-plan starts clean.
    assert result["planning_feedback"] is None
    # The repo slug still derives from the original prompt, not the feedback.
    assert result["repo_name"] == "platformer-double-jump-hero-3f9a1c2e"


async def test_planning_without_feedback_sends_prompt_untouched():
    client = _FakeStrategicClient(genre="Platformer")
    node = build_planning_node(client)

    await node({"game_id": "3f9a1c2e-aaaa", "prompt": "Double Jump Hero!"})

    assert client.received_prompts == ["Double Jump Hero!"]
