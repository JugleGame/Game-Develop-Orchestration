"""Client for GitMcpServer (§2.6).

Two arguments here go beyond §03's tool table, and both are optional on the
server, so a server built strictly to the contract still works:

* ``repo_name`` on every tool. §03 gives only ``git_init`` a repo identity,
  while §03 §2 has the orchestrator open one MCP session per call — so without
  it the server can only guess (it falls back to the most recently initialised
  repo). That guess is correct at ``MAX_CONCURRENT_JOBS=1`` and wrong the
  moment two jobs overlap: they commit into each other's repositories.
* ``source_path`` on ``git_commit``. Nothing in §03 lets this server receive
  the game's files, so a commit without it publishes an empty tree while
  reporting a valid SHA (measured in ``05_계약_변경_제안서`` §1.1).
"""

from typing import Any

from app.mcp.base_client import BaseToolClient


class GitClient(BaseToolClient):
    """Wraps deployment-branch Git operations.

    ``git_init`` is idempotent by spec (§03) and ``git_pull``/``git_branch``/
    ``git_status`` are safe to repeat, so only the history-mutating tools are
    excluded from automatic retry.
    """

    _NON_IDEMPOTENT_TOOLS = frozenset({"git_commit", "git_push", "git_tag"})

    @staticmethod
    def _with_repo(payload: dict[str, Any], repo_name: str) -> dict[str, Any]:
        if repo_name:
            payload["repoName"] = repo_name
        return payload

    async def git_init(self, *, repo_name: str) -> dict[str, Any]:
        """Create ``repo_name`` if it doesn't exist yet, otherwise reuse it as-is."""

        return await self.call_tool("git_init", {"repoName": repo_name})

    async def git_branch(self, *, branch: str, repo_name: str = "") -> dict[str, Any]:
        return await self.call_tool("git_branch", self._with_repo({"branch": branch}, repo_name))

    async def git_pull(self, *, branch: str, repo_name: str = "") -> dict[str, Any]:
        return await self.call_tool("git_pull", self._with_repo({"branch": branch}, repo_name))

    async def git_commit(
        self, *, branch: str, message: str, repo_name: str = "", source_path: str = ""
    ) -> str:
        """Commit the working tree, optionally syncing ``source_path`` into it first.

        ``source_path`` is the Unity **project root** (``build_project``'s
        ``projectPath``), not the built binary: what gets published is the
        game's source. Left empty the server falls back to ``GIT_SOURCE_PATH``,
        and with neither set the commit is empty — the server logs a warning
        rather than failing, so the emptiness stays observable.
        """

        payload = self._with_repo({"branch": branch, "message": message}, repo_name)
        if source_path:
            payload["sourcePath"] = source_path
        body = await self.call_tool("git_commit", payload)
        return body["commit"]

    async def git_push(self, *, branch: str, repo_name: str = "") -> dict[str, Any]:
        return await self.call_tool("git_push", self._with_repo({"branch": branch}, repo_name))

    async def git_tag(self, *, tag: str, repo_name: str = "") -> dict[str, Any]:
        return await self.call_tool("git_tag", self._with_repo({"tag": tag}, repo_name))

    async def git_status(self, *, repo_name: str = "") -> dict[str, Any]:
        return await self.call_tool("git_status", self._with_repo({}, repo_name))
