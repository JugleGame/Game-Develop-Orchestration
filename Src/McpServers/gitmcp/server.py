"""GitMcpServer — deployment-branch Git operations backed by local bare repos.

Implements the seven §03 contract tools. Every tool except ``git_init``
additionally accepts an optional ``repoName`` beyond what §03 requires: the
contract's own argument lists (``verify_contract.py``) never pass a repo
identity to ``git_branch``/``git_pull``/``git_commit``/``git_push``/``git_tag``,
and the orchestrator opens a new MCP session per call (§03 §2), so there is
no way for those calls to name their target repo today. When ``repoName`` is
omitted, the server falls back to the most recently established repo (see
``repo.py`` for why that is safe under the orchestrator's current
serial-job default and what breaks if that changes).

``git_commit`` additionally takes ``sourcePath``: §03 gives this server no way
to receive the game's files, so without it every deployment publishes an empty
commit. When omitted it falls back to ``GIT_SOURCE_PATH``, which is what makes
deployment work before the orchestrator learns to send the argument.

Return type is ``dict[str, Any]``: a bare ``dict`` leaves ``structuredContent``
empty; see ``common/server.py``.

Logging: each tool logs entry and failure with the resolved repo and the §03
error code (CLAUDE.md requires logging on every MCP path; §7.4 requires it per
job). No credentials are involved — the remote is a local path, and if this is
ever pointed at a hosted remote the token must come from the environment and
must not be logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from common.errors import VALIDATION_ERROR, tool_error
from common.server import build as build_server
from common.server import expects_dict_return, serve

from . import repo

mcp = build_server("GitMcpServer", 9105)

logger = logging.getLogger("GitMcpServer")


def _require_str(value: str, field: str) -> str:
    if not value or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value.strip()


def _log_failure(tool: str, requested_repo: str | None, exc: ToolError) -> None:
    """Record a tool failure together with the §03 code the caller will see."""

    code: Any = None
    try:
        code = json.loads(str(exc)).get("errorCode")
    except ValueError:
        pass
    logger.error(
        "Git tool failed",
        extra={"tool": tool, "repo": requested_repo, "error_code": code},
    )


@mcp.tool(description="Create the repository if absent, otherwise reuse it (idempotent).")
@expects_dict_return
def git_init(repoName: str) -> dict[str, Any]:
    logger.info("git_init called", extra={"repo": repoName})
    try:
        repo_name = _require_str(repoName, "repoName")
        created = repo.init_repo(repo_name)
        return {"repoName": repo_name, "created": created}
    except ToolError as exc:
        _log_failure("git_init", repoName, exc)
        raise


@mcp.tool(description="Check out or create a branch.")
@expects_dict_return
def git_branch(branch: str, repoName: str | None = None) -> dict[str, Any]:
    logger.info("git_branch called", extra={"repo": repoName, "branch": branch})
    try:
        branch_name = _require_str(branch, "branch")
        target_repo = repo.resolve_repo(repoName)
        repo.checkout_branch(target_repo, branch_name)
        return {"branch": branch_name}
    except ToolError as exc:
        _log_failure("git_branch", repoName, exc)
        raise


@mcp.tool(description="Pull remote changes for a branch.")
@expects_dict_return
def git_pull(branch: str, repoName: str | None = None) -> dict[str, Any]:
    logger.info("git_pull called", extra={"repo": repoName, "branch": branch})
    try:
        branch_name = _require_str(branch, "branch")
        target_repo = repo.resolve_repo(repoName)
        updated = repo.pull_branch(target_repo, branch_name)
        return {"branch": branch_name, "updated": updated}
    except ToolError as exc:
        _log_failure("git_pull", repoName, exc)
        raise


@mcp.tool(
    description=(
        "Commit the working tree. Pass sourcePath (the Unity project directory) to sync the "
        "game's files into the repository first; without it the commit contains whatever the "
        "working clone already held."
    )
)
@expects_dict_return
def git_commit(
    branch: str, message: str, repoName: str | None = None, sourcePath: str = ""
) -> dict[str, Any]:
    logger.info("git_commit called", extra={"repo": repoName, "branch": branch})
    try:
        branch_name = _require_str(branch, "branch")
        commit_message = _require_str(message, "message")
        target_repo = repo.resolve_repo(repoName)
        # None when neither the argument nor GIT_SOURCE_PATH is set; the commit
        # then behaves exactly as it did before this argument existed.
        source = repo.resolve_source(sourcePath)
        commit_hash = repo.commit_all(target_repo, commit_message, source)
        return {"commit": commit_hash}
    except ToolError as exc:
        _log_failure("git_commit", repoName, exc)
        raise


@mcp.tool(description="Push a branch to the remote.")
@expects_dict_return
def git_push(branch: str, repoName: str | None = None) -> dict[str, Any]:
    logger.info("git_push called", extra={"repo": repoName, "branch": branch})
    try:
        branch_name = _require_str(branch, "branch")
        target_repo = repo.resolve_repo(repoName)
        repo.push_branch(target_repo, branch_name)
        return {"branch": branch_name, "pushed": True}
    except ToolError as exc:
        _log_failure("git_push", repoName, exc)
        raise


@mcp.tool(description="Tag the current commit.")
@expects_dict_return
def git_tag(tag: str, repoName: str | None = None) -> dict[str, Any]:
    logger.info("git_tag called", extra={"repo": repoName, "tag": tag})
    try:
        tag_name = _require_str(tag, "tag")
        target_repo = repo.resolve_repo(repoName)
        repo.create_tag(target_repo, tag_name)
        return {"tag": tag_name}
    except ToolError as exc:
        _log_failure("git_tag", repoName, exc)
        raise


@mcp.tool(description="Report the repository's working-tree status.")
@expects_dict_return
def git_status(repoName: str | None = None) -> dict[str, Any]:
    logger.info("git_status called", extra={"repo": repoName})
    try:
        return {"clean": repo.is_clean(repoName)}
    except ToolError as exc:
        _log_failure("git_status", repoName, exc)
        raise


if __name__ == "__main__":
    serve(mcp)
