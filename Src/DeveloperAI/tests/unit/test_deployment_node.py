"""Unit tests for the Deployment node's Git call order and repo-name handling.

Beyond call order, these pin the two arguments that decide whether a
deployment is real: ``repo_name`` on every call (otherwise the server guesses
the target repository) and ``source_path`` on the commit (otherwise a valid
commit hash is published over an empty tree).
"""

from typing import Any

from app.graph.nodes.deployment import build_deployment_node
from app.graph.state import Stage
from app.models.schemas import JobStatus


class _FakeGitClient:
    """Records call order/args instead of hitting a real GitMcpServer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def git_init(self, *, repo_name: str) -> dict[str, Any]:
        self.calls.append(("git_init", {"repo_name": repo_name}))
        return {"success": True}

    async def git_branch(self, *, branch: str, repo_name: str = "") -> dict[str, Any]:
        self.calls.append(("git_branch", {"branch": branch, "repo_name": repo_name}))
        return {"success": True}

    async def git_pull(self, *, branch: str, repo_name: str = "") -> dict[str, Any]:
        self.calls.append(("git_pull", {"branch": branch, "repo_name": repo_name}))
        return {"success": True}

    async def git_commit(
        self, *, branch: str, message: str, repo_name: str = "", source_path: str = ""
    ) -> str:
        self.calls.append(
            (
                "git_commit",
                {
                    "branch": branch,
                    "message": message,
                    "repo_name": repo_name,
                    "source_path": source_path,
                },
            )
        )
        return "commit-1"

    async def git_push(self, *, branch: str, repo_name: str = "") -> dict[str, Any]:
        self.calls.append(("git_push", {"branch": branch, "repo_name": repo_name}))
        return {"success": True}

    async def git_tag(self, *, tag: str, repo_name: str = "") -> dict[str, Any]:
        self.calls.append(("git_tag", {"tag": tag, "repo_name": repo_name}))
        return {"success": True}

    def args_for(self, tool: str) -> dict[str, Any]:
        return next(args for name, args in self.calls if name == tool)


_REPO = "platformer-double-jump-hero-3f9a1c2e"

_STATE: dict[str, Any] = {
    "game_id": "3f9a1c2e-aaaa",
    "prompt": "Double Jump Hero!",
    "game_design": {"genre": "Platformer"},
    "repo_name": _REPO,
    "qa_iteration_count": 2,
    "build_result": {
        "buildId": "b-1",
        "artifactPath": "Builds/3f9a1c2e/game.exe",
        "projectPath": "C:/Unity/AutoGenProject",
    },
}


async def test_deployment_node_reuses_repo_name_set_by_planning():
    client = _FakeGitClient()
    node = build_deployment_node(client)

    # Planning always sets state["repo_name"] before Deployment runs (§app/graph/nodes/planning.py).
    result = await node(dict(_STATE))

    assert [name for name, _ in client.calls] == [
        "git_init",
        "git_branch",
        "git_pull",
        "git_commit",
        "git_push",
        "git_tag",
    ]

    assert client.calls[0][1]["repo_name"] == _REPO
    assert client.calls[1][1]["branch"] == "main"
    assert client.calls[2][1]["branch"] == "main"
    assert client.calls[4][1]["branch"] == "main"
    assert client.calls[5][1]["tag"] == "v2"

    assert result["current_stage"] == Stage.DEPLOYMENT
    assert result["status"] == JobStatus.DONE.value
    assert result["repo_name"] == _REPO
    assert result["artifact"]["repo_name"] == _REPO
    assert result["artifact"]["commit_hash"] == "commit-1"
    assert result["artifact"]["branch"] == "main"


async def test_deployment_node_computes_repo_name_when_missing_from_state():
    """Defensive fallback: Deployment should never crash if repo_name wasn't
    threaded through (e.g. a hand-built test state), it just derives one."""

    client = _FakeGitClient()
    node = build_deployment_node(client)

    result = await node(
        {
            "game_id": "abcdefgh1234",
            "prompt": "",
            "game_design": None,
            "qa_iteration_count": 0,
        }
    )

    assert result["artifact"]["repo_name"] == "game-prototype-abcdefgh"


async def test_deployment_node_names_the_target_repo_on_every_call():
    """Without repoName the server falls back to 'most recently initialised
    repo', which is only correct while jobs run strictly one at a time."""

    client = _FakeGitClient()
    await build_deployment_node(client)(dict(_STATE))

    assert all(args["repo_name"] == _REPO for _, args in client.calls)


async def test_deployment_node_commits_the_unity_project_source():
    """The published artifact is the game's source, so the commit carries the
    project root reported by build_project — not the built binary."""

    client = _FakeGitClient()
    await build_deployment_node(client)(dict(_STATE))

    assert client.args_for("git_commit")["source_path"] == "C:/Unity/AutoGenProject"


async def test_deployment_node_records_source_path_on_the_artifact():
    """Makes an empty publish diagnosable from the artifact alone."""

    client = _FakeGitClient()
    result = await build_deployment_node(client)(dict(_STATE))

    assert result["artifact"]["source_path"] == "C:/Unity/AutoGenProject"


async def test_deployment_node_omits_source_path_when_build_did_not_report_one():
    """An older build result leaves the server on its GIT_SOURCE_PATH fallback
    rather than making the node fail."""

    client = _FakeGitClient()
    state = dict(_STATE)
    state["build_result"] = {"buildId": "b-1", "artifactPath": "Builds/x/game.exe"}

    await build_deployment_node(client)(state)

    assert client.args_for("git_commit")["source_path"] == ""
