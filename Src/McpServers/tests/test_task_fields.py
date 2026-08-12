from __future__ import annotations

from strategic.server import _blueprint_document, _blueprint_spec_ids, _dependencies_for, _to_feature_prompt
from strategic.specs import SpecDocument, declared_spec_ids


def test_task_prompt_carries_lightweight_handoff_fields():
    spec = SpecDocument(
        spec_id="demo__spec-001",
        game_id="demo",
        title="Movement",
        version=1,
        blueprint_version=1,
        refs=["GENRE-006"],
        goal="Move the player.",
        implementation_scope=["Read movement input."],
        out_of_scope=["Combat."],
        acceptance_criteria=["A PlayMode test observes movement within 1 second."],
        context="This is the first playable capability.",
        relevant_systems=["input", "player movement"],
        constraints=["Do not add combat rules."],
        verification_method=["Run the movement PlayMode test."],
    )

    prompt = _to_feature_prompt(spec)

    assert prompt["objective"] == "Move the player."
    assert prompt["context"] == "This is the first playable capability."
    assert prompt["relevantSystems"] == ["input", "player movement"]
    assert prompt["implementationRequirements"] == ["Read movement input."]
    assert prompt["constraints"] == ["Do not add combat rules."]
    assert prompt["verificationMethod"] == ["Run the movement PlayMode test."]
    assert "## Verification method" in prompt["description"]


def test_blueprint_keeps_only_ordered_spec_ids_not_duplicate_task_bodies():
    first = _spec("demo__spec-001")
    second = _spec("demo__spec-002", dependencies=["demo__spec-001"])

    blueprint = _blueprint_document({"title": "Demo", "specs": [{"title": "stale"}]}, [second, first])

    assert "specs" not in blueprint
    assert blueprint["specIds"] == ["demo__spec-001", "demo__spec-002"]
    assert declared_spec_ids([second, first]) == blueprint["specIds"]
    assert _blueprint_spec_ids("demo", blueprint) == set(blueprint["specIds"])


def test_dependencies_accept_local_or_already_namespaced_ids():
    assert _dependencies_for("demo", ["spec-001", "demo__spec-002"]) == [
        "demo__spec-001",
        "demo__spec-002",
    ]


def _spec(spec_id: str, dependencies: list[str] | None = None) -> SpecDocument:
    return SpecDocument(
        spec_id=spec_id,
        game_id="demo",
        title="Task",
        version=1,
        blueprint_version=1,
        refs=["GENRE-006"],
        goal="Do the task.",
        implementation_scope=["one action"],
        out_of_scope=["other action"],
        acceptance_criteria=["A test observes one action."],
        dependencies=dependencies or [],
    )
