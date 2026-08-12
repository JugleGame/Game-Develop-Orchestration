from __future__ import annotations

import hashlib
import json

import pytest

from strategic.handoff import HandoffError, export_handoff, verify_handoff
from strategic.specs import SpecDocument


def _spec() -> SpecDocument:
    return SpecDocument(
        spec_id="demo__spec-001",
        game_id="demo",
        title="Movement",
        version=1,
        blueprint_version=2,
        refs=["GENRE-006"],
        goal="Move the player.",
        implementation_scope=["movement input"],
        out_of_scope=["combat"],
        acceptance_criteria=["The PlayMode test observes movement within 1 second."],
        status="published",
    )


def test_export_handoff_writes_a_versioned_manifest_and_verifiable_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_ROOT", str(tmp_path / "var" / "handoffs"))
    spec = _spec()
    result = export_handoff(
        game_id="demo",
        blueprint={"title": "Demo", "status": "published"},
        blueprint_version=2,
        specs=[spec],
        feature_prompts=[{"feature_id": spec.spec_id, "dependencies": []}],
    )

    package = tmp_path / "var" / "handoffs" / "demo" / "v2"
    manifest = json.loads((package / "execution-manifest.json").read_text(encoding="utf-8"))
    assert result["handoffPath"] == str(package)
    assert (package / "specs" / "demo__spec-001.md").exists()
    for file in manifest["files"]:
        path = package / file["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == file["sha256"]
    assert verify_handoff(package)["manifestSha256"] == result["manifestSha256"]


def test_verify_handoff_rejects_manifest_and_input_tampering(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_ROOT", str(tmp_path / "var" / "handoffs"))
    spec = _spec()
    export_handoff(
        game_id="demo",
        blueprint={"title": "Demo", "status": "published"},
        blueprint_version=1,
        specs=[spec],
        feature_prompts=[{"feature_id": spec.spec_id, "dependencies": []}],
    )
    package = tmp_path / "var" / "handoffs" / "demo" / "v1"
    (package / "blueprint.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(HandoffError, match="file checksum"):
        verify_handoff(package)
    (package / "execution-manifest.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(HandoffError, match="manifest checksum"):
        verify_handoff(package)


def test_export_handoff_rejects_path_traversal_and_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_ROOT", str(tmp_path / "var" / "handoffs"))
    spec = _spec()
    kwargs = dict(
        blueprint={"title": "Demo", "status": "published"},
        blueprint_version=1,
        specs=[spec],
        feature_prompts=[{"feature_id": spec.spec_id, "dependencies": []}],
    )

    with pytest.raises(HandoffError, match="gameId"):
        export_handoff(game_id="../outside", **kwargs)
    export_handoff(game_id="demo", **kwargs)
    with pytest.raises(HandoffError, match="already exists"):
        export_handoff(game_id="demo", **kwargs)
