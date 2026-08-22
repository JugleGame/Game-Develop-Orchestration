"""Keep repository Issue governance out of external Unity project creation."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
UNITY_PROJECT_EXCEPTION = (
    "Creating or operating an external Unity project does not by itself require or create a "
    "GitHub Issue."
)


def test_host_adapters_do_not_require_an_issue_for_external_unity_project_creation() -> None:
    for adapter in ("AGENTS.md", "CLAUDE.md"):
        content = " ".join((ROOT / adapter).read_text(encoding="utf-8").split())
        assert UNITY_PROJECT_EXCEPTION in content, adapter


def test_issue_workflow_limits_contract_to_orchestration_repository_changes() -> None:
    content = " ".join(
        (ROOT / "docs" / "github-issue-workflow.md").read_text(encoding="utf-8").split()
    )

    assert "This contract applies to source changes and Pull Requests" in content
    assert "must not create a GitHub Issue merely because a project was created" in content
    assert "Normal pipeline maintenance that changes this repository" in content
