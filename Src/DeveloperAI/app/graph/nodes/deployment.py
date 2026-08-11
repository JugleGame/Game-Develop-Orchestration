"""Deployment node: inits/pulls, commits, pushes, and tags the QA-passed build.

Each game gets its own dedicated Git repository, named by Planning (§utils.naming) as
``{genre}-{prompt-slug}-{gameId}`` and carried in ``state["repo_name"]`` from then on so
re-deployments (e.g. after revising an already-shipped game) target the same repo.
``git_init`` is idempotent on GitMcpServer: a brand-new name creates a new repository, while
a name that already exists is reused as-is. Pulling before commit/push keeps the repo's
``main`` branch in sync with any remote commits from a prior QA-pass iteration, avoiding a
rejected non-fast-forward push.

Two things every call carries beyond §03's tool table, both optional server-side:

* ``repo_name`` on all six calls. §03 only gives ``git_init`` a repo identity and the
  orchestrator opens one session per call, so the server would otherwise fall back to
  "most recently initialised repo" — correct only while jobs run one at a time.
* ``source_path`` on the commit. Without it the deployment publishes a valid commit hash
  over an empty tree, and the pipeline reports success with no game in the repository
  (measured in ``05_계약_변경_제안서`` §1.1). The value is the Unity **project root**
  reported by ``build_project``, because what ships is the source, not the binary.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.git_client import GitClient
from app.models.schemas import JobStatus
from app.utils.naming import build_repo_name

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]

_MAIN_BRANCH = "main"


def build_deployment_node(client: GitClient) -> NodeFn:
    async def deployment_node(state: GraphState) -> dict:
        game_id = state["game_id"]
        design = state.get("game_design") or {}
        iteration = state.get("qa_iteration_count", 0)
        genre = design.get("genre", "game")
        repo_name = state.get("repo_name") or build_repo_name(
            genre=genre, prompt=state.get("prompt", ""), game_id=game_id
        )
        tag = f"v{iteration}"

        # UnityMcpServer reports the project root alongside the build artifact.
        # Empty when the build result predates that field, in which case the
        # server falls back to GIT_SOURCE_PATH and, failing that, warns about
        # the empty commit instead of silently publishing one.
        build_result = state.get("build_result") or {}
        source_path = build_result.get("projectPath") or ""

        await client.git_init(repo_name=repo_name)
        await client.git_branch(branch=_MAIN_BRANCH, repo_name=repo_name)
        await client.git_pull(branch=_MAIN_BRANCH, repo_name=repo_name)
        commit_hash = await client.git_commit(
            branch=_MAIN_BRANCH,
            message=f"[AutoGen] {genre} - QA Pass v{iteration}",
            repo_name=repo_name,
            source_path=source_path,
        )
        await client.git_push(branch=_MAIN_BRANCH, repo_name=repo_name)
        await client.git_tag(tag=tag, repo_name=repo_name)

        return {
            "current_stage": Stage.DEPLOYMENT,
            "status": JobStatus.DONE.value,
            "repo_name": repo_name,
            "artifact": {
                "game_id": game_id,
                "repo_name": repo_name,
                "commit_hash": commit_hash,
                "tag": tag,
                "branch": _MAIN_BRANCH,
                # Recorded so a "deployed but empty" repository is diagnosable
                # from the artifact alone, without re-reading the build result.
                "source_path": source_path,
            },
        }

    return deployment_node
