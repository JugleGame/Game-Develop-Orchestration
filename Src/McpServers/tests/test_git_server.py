"""Tests for GitMcpServer.

Driven through a real MCP session (SDK in-memory transport) against a real
local bare Git repository (see ``gitmcp/repo.py``), so the §03 contract and
actual git semantics (idempotent init, non-fast-forward rejection, clean
commits) are exercised together rather than mocked away.

§03 gives no tool a path or file content, so ``git_commit(sourcePath=...)``
carries that job (see ``gitmcp/server.py``). Tests that exercise the sync build
a Unity-shaped source tree; tests about branch/tag/push mechanics still write
straight into the working clone (``gitmcp.repo._work_path``) because what the
tree contains is irrelevant to them.
"""

from pathlib import Path
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import git as gitpython
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from gitmcp import repo
from gitmcp.server import mcp


@asynccontextmanager
async def session() -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(mcp) as client:
        await client.initialize()
        yield client


def _write(repo_name: str, relative_path: str, content: str) -> None:
    path = repo._work_path(repo_name) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# §03 contract
# --------------------------------------------------------------------------


async def test_exposes_every_contract_tool():
    async with session() as client:
        names = {tool.name for tool in (await client.list_tools()).tools}

    assert {
        "git_init",
        "git_branch",
        "git_pull",
        "git_commit",
        "git_push",
        "git_tag",
        "git_status",
    } <= names


async def test_contract_tools_use_expected_argument_names():
    async with session() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert {"repoName"} <= set(tools["git_init"].inputSchema["properties"])
    assert {"branch"} <= set(tools["git_branch"].inputSchema["properties"])
    assert {"branch"} <= set(tools["git_pull"].inputSchema["properties"])
    assert {"branch", "message"} <= set(tools["git_commit"].inputSchema["properties"])
    assert {"branch"} <= set(tools["git_push"].inputSchema["properties"])
    assert {"tag"} <= set(tools["git_tag"].inputSchema["properties"])
    # git_status has no required args per §03; the schema may still list an
    # optional repoName, which is fine as long as nothing is required.
    assert tools["git_status"].inputSchema.get("required", []) == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("git_init", {"repoName": ""}),
        ("git_branch", {"branch": ""}),
        ("git_pull", {"branch": ""}),
        ("git_commit", {"branch": "main", "message": ""}),
        ("git_push", {"branch": ""}),
        ("git_tag", {"tag": ""}),
    ],
)
async def test_empty_required_argument_carries_error_code_1000(tool, args):
    async with session() as client:
        result = await client.call_tool(tool, args)

    assert result.isError is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# git_init idempotency
# --------------------------------------------------------------------------


async def test_git_init_creates_then_reuses_the_same_repo():
    async with session() as client:
        first = await client.call_tool("git_init", {"repoName": "t-idempotent"})
        second = await client.call_tool("git_init", {"repoName": "t-idempotent"})

    assert first.structuredContent == {"repoName": "t-idempotent", "created": True}
    assert second.structuredContent == {"repoName": "t-idempotent", "created": False}


# --------------------------------------------------------------------------
# Full deployment-order flow (mirrors app/graph/nodes/deployment.py exactly)
# --------------------------------------------------------------------------


async def test_full_deployment_sequence_produces_a_real_commit_on_the_bare_repo():
    repo_name = "t-deploy-flow"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        await client.call_tool("git_pull", {"branch": "main", "repoName": repo_name})

        _write(repo_name, "Assets/Scripts/Player.cs", "// player script\n")

        commit_result = await client.call_tool(
            "git_commit",
            {
                "branch": "main",
                "message": "[AutoGen] platformer - QA Pass v0",
                "repoName": repo_name,
            },
        )
        push_result = await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})
        tag_result = await client.call_tool("git_tag", {"tag": "v0", "repoName": repo_name})

    commit_hash = commit_result.structuredContent["commit"]
    assert len(commit_hash) == 40  # a real git SHA-1, not a placeholder
    assert push_result.structuredContent == {"branch": "main", "pushed": True}
    assert tag_result.structuredContent == {"tag": "v0"}

    # Verify independently of the server's own report: clone the bare repo
    # fresh and check the file and tag really landed there.
    verify_path = repo.WORK_DIR / "t-deploy-flow-verify"
    clone = gitpython.Repo.clone_from(repo._bare_path(repo_name), verify_path)
    assert clone.head.commit.hexsha == commit_hash
    assert (verify_path / "Assets/Scripts/Player.cs").read_text(
        encoding="utf-8"
    ) == "// player script\n"
    assert {t.name for t in clone.tags} == {"v0"}


async def test_git_commit_is_idempotent_when_nothing_changed():
    repo_name = "t-commit-idempotent"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})

        _write(repo_name, "file.txt", "v1\n")
        first = await client.call_tool(
            "git_commit", {"branch": "main", "message": "m1", "repoName": repo_name}
        )
        # No file changes before this second commit call.
        second = await client.call_tool(
            "git_commit", {"branch": "main", "message": "m2", "repoName": repo_name}
        )

    assert first.structuredContent["commit"] == second.structuredContent["commit"]


# --------------------------------------------------------------------------
# git_tag idempotency and conflict detection
# --------------------------------------------------------------------------


async def test_retagging_the_same_commit_is_a_no_op():
    repo_name = "t-tag-idempotent"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        _write(repo_name, "f.txt", "x\n")
        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m", "repoName": repo_name}
        )

        first = await client.call_tool("git_tag", {"tag": "v0", "repoName": repo_name})
        second = await client.call_tool("git_tag", {"tag": "v0", "repoName": repo_name})

    assert first.isError is False
    assert second.isError is False


async def test_tag_already_on_the_remote_at_another_commit_is_rejected():
    """The regression that shipped: a working clone only learns about tags at
    clone/fetch time, so a tag pushed by an earlier deployment from a different
    clone is invisible locally. The resulting push is rejected via a *flag*,
    not an exception - so without checking the remote and the flag, this server
    reported a successful deployment while the remote tag pointed at a
    completely different commit."""

    repo_name = "t-tag-remote-conflict"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        _write(repo_name, "f.txt", "v1\n")
        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m1", "repoName": repo_name}
        )
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    # Another clone publishes v0 on its own commit. Our clone never sees it.
    other_path = repo.WORK_DIR / f"{repo_name}-other"
    other = gitpython.Repo.clone_from(repo._bare_path(repo_name), other_path)
    with other.config_writer() as cfg:
        cfg.set_value("user", "name", "other")
        cfg.set_value("user", "email", "other@local")
    (other_path / "external.txt").write_text("x\n", encoding="utf-8")
    other.git.add(all=True)
    other_commit = other.index.commit("external")
    other.remotes.origin.push("main:main")
    other.create_tag("v0")
    other.remotes.origin.push("refs/tags/v0")

    async with session() as client:
        result = await client.call_tool("git_tag", {"tag": "v0", "repoName": repo_name})

    assert result.isError is True
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert '"errorCode": 1000' in text

    # The remote tag must be untouched, still pointing at the other clone's commit.
    bare = gitpython.Repo(repo._bare_path(repo_name))
    assert bare.tags["v0"].commit.hexsha == other_commit.hexsha

    # And no orphaned local tag may be left behind to block a later retry.
    assert "v0" not in {t.name for t in gitpython.Repo(repo._work_path(repo_name)).tags}


async def test_local_tag_that_was_never_published_gets_pushed():
    """A tag created locally but whose push failed earlier must be publishable
    on retry rather than treated as a conflict with itself."""

    repo_name = "t-tag-unpublished"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        _write(repo_name, "f.txt", "v1\n")
        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m1", "repoName": repo_name}
        )
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    # Simulate the local-only tag left by an interrupted earlier attempt.
    work = gitpython.Repo(repo._work_path(repo_name))
    work.create_tag("v0")
    assert "v0" not in {t.name for t in gitpython.Repo(repo._bare_path(repo_name)).tags}

    async with session() as client:
        result = await client.call_tool("git_tag", {"tag": "v0", "repoName": repo_name})

    assert result.isError is False
    assert "v0" in {t.name for t in gitpython.Repo(repo._bare_path(repo_name)).tags}


async def test_retagging_a_different_commit_with_the_same_name_is_rejected():
    repo_name = "t-tag-conflict"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})

        _write(repo_name, "f.txt", "v1\n")
        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m1", "repoName": repo_name}
        )
        await client.call_tool("git_tag", {"tag": "v0", "repoName": repo_name})

        _write(repo_name, "f.txt", "v2\n")
        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m2", "repoName": repo_name}
        )
        conflict = await client.call_tool("git_tag", {"tag": "v0", "repoName": repo_name})

    assert conflict.isError is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in conflict.content)


# --------------------------------------------------------------------------
# git_status
# --------------------------------------------------------------------------


async def test_status_reports_dirty_then_clean_after_commit():
    repo_name = "t-status"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})

        _write(repo_name, "f.txt", "x\n")
        dirty = await client.call_tool("git_status", {"repoName": repo_name})

        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m", "repoName": repo_name}
        )
        clean = await client.call_tool("git_status", {"repoName": repo_name})

    assert dirty.structuredContent == {"clean": False}
    assert clean.structuredContent == {"clean": True}


# --------------------------------------------------------------------------
# repoName fallback (see gitmcp/repo.py docstring: §03's other six tools
# carry no repoName at all)
# --------------------------------------------------------------------------


async def test_omitting_repo_name_falls_back_to_the_most_recently_initialized_repo():
    async with session() as client:
        await client.call_tool("git_init", {"repoName": "t-fallback"})
        # No repoName on any of the following calls, exactly like the real
        # orchestrator's GitClient (app/mcp/git_client.py) calls them today.
        await client.call_tool("git_branch", {"branch": "main"})
        _write("t-fallback", "f.txt", "x\n")
        commit = await client.call_tool("git_commit", {"branch": "main", "message": "m"})

    assert Path(repo._work_path("t-fallback") / "f.txt").exists()
    assert len(commit.structuredContent["commit"]) == 40


async def test_operating_on_a_repo_that_was_never_initialized_is_a_validation_error():
    async with session() as client:
        result = await client.call_tool(
            "git_branch", {"branch": "main", "repoName": "t-never-initialized"}
        )

    assert result.isError is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


# --------------------------------------------------------------------------
# Non-fast-forward safety (the exact reason deployment.py pulls before commit/push)
# --------------------------------------------------------------------------


async def test_push_without_pulling_diverged_remote_changes_fails_clearly():
    """Simulates two deployments racing on the same repo: the second push,
    attempted without pulling first, must fail loudly rather than silently
    overwrite the first deployment's commit."""

    repo_name = "t-race"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})

    # A second, independent working clone simulates another process/job that
    # pushed to the same repo after our clone was created.
    other_clone_path = repo.WORK_DIR / f"{repo_name}-other"
    other = gitpython.Repo.clone_from(repo._bare_path(repo_name), other_clone_path)
    other.git.symbolic_ref("HEAD", "refs/heads/main")
    with other.config_writer() as cfg:
        cfg.set_value("user", "name", "other")
        cfg.set_value("user", "email", "other@local")
    (other_clone_path / "external.txt").write_text("from another job\n", encoding="utf-8")
    other.git.add(all=True)
    other.index.commit("external commit")
    other.remotes.origin.push("main:main")

    async with session() as client:
        _write(repo_name, "mine.txt", "from this job\n")
        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m", "repoName": repo_name}
        )
        push_result = await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    assert push_result.isError is True
    text = "".join(getattr(b, "text", "") for b in push_result.content)
    assert '"errorCode": 3000' in text


async def test_pull_before_push_avoids_the_non_fast_forward_rejection():
    """Same race as above, but this time git_pull runs first (as
    deployment.py always does), so the push succeeds instead of rejecting."""

    repo_name = "t-race-resolved"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})

    other_clone_path = repo.WORK_DIR / f"{repo_name}-other"
    other = gitpython.Repo.clone_from(repo._bare_path(repo_name), other_clone_path)
    other.git.symbolic_ref("HEAD", "refs/heads/main")
    with other.config_writer() as cfg:
        cfg.set_value("user", "name", "other")
        cfg.set_value("user", "email", "other@local")
    (other_clone_path / "external.txt").write_text("from another job\n", encoding="utf-8")
    other.git.add(all=True)
    other.index.commit("external commit")
    other.remotes.origin.push("main:main")

    async with session() as client:
        pull_result = await client.call_tool("git_pull", {"branch": "main", "repoName": repo_name})
        _write(repo_name, "mine.txt", "from this job\n")
        await client.call_tool(
            "git_commit", {"branch": "main", "message": "m", "repoName": repo_name}
        )
        push_result = await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    assert pull_result.structuredContent == {"branch": "main", "updated": True}
    assert push_result.isError is False
    assert push_result.structuredContent == {"branch": "main", "pushed": True}


# --------------------------------------------------------------------------
# Source sync — the fix for the empty-deployment defect
#
# §03's Git tools take no path, so the working clone stayed empty and every
# deployment published a commit with zero files while reporting success.
# git_commit(sourcePath=...) mirrors the Unity project in first.
# --------------------------------------------------------------------------


def _unity_project(tmp_path, *, with_script: str = "// player\n") -> Path:
    """A Unity-shaped tree: real content plus the caches Unity regenerates."""

    project = tmp_path / "UnityProject"
    (project / "Assets" / "Scripts").mkdir(parents=True)
    (project / "Assets" / "Scripts" / "PlayerController.cs").write_text(
        with_script, encoding="utf-8"
    )
    (project / "Assets" / "Scenes").mkdir(parents=True)
    (project / "Assets" / "Scenes" / "Main.unity").write_text("scene\n", encoding="utf-8")
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "6000.0.0f1\n", encoding="utf-8"
    )

    # Everything below must NOT be published.
    (project / "Library" / "Artifacts").mkdir(parents=True)
    (project / "Library" / "Artifacts" / "cache.bin").write_text("x" * 1000, encoding="utf-8")
    (project / "Temp").mkdir()
    (project / "Temp" / "build.tmp").write_text("tmp\n", encoding="utf-8")
    (project / "obj").mkdir()
    (project / "obj" / "stale.o").write_text("o\n", encoding="utf-8")
    (project / "Logs").mkdir()
    (project / "Logs" / "run.log").write_text("log\n", encoding="utf-8")
    (project / "Game.csproj").write_text("<Project/>\n", encoding="utf-8")
    (project / "Game.sln").write_text("sln\n", encoding="utf-8")
    return project


def _published_files(repo_name: str) -> set[str]:
    """Read the bare repo back, so the assertion does not trust the server."""

    bare = gitpython.Repo(repo._bare_path(repo_name))
    return {entry.path for entry in bare.head.commit.tree.traverse() if entry.type == "blob"}


async def test_source_path_publishes_the_project_and_skips_unity_caches(tmp_path):
    repo_name = "t-sync"
    project = _unity_project(tmp_path)

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        commit = await client.call_tool(
            "git_commit",
            {
                "branch": "main",
                "message": "[AutoGen] platformer - QA Pass v1",
                "repoName": repo_name,
                "sourcePath": str(project),
            },
        )
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    assert commit.isError is False, commit.content
    published = _published_files(repo_name)

    assert "Assets/Scripts/PlayerController.cs" in published
    assert "Assets/Scenes/Main.unity" in published
    assert "ProjectSettings/ProjectVersion.txt" in published
    assert ".gitignore" in published, "published repo must carry the Unity ignore rules"

    regenerated = [p for p in published if p.startswith(("Library/", "Temp/", "obj/", "Logs/"))]
    assert not regenerated, f"Unity caches were published: {regenerated}"
    assert not [p for p in published if p.endswith((".csproj", ".sln"))]


async def test_sync_is_a_mirror_so_deletions_propagate(tmp_path):
    """A script removed from the project must not live on in the repository."""

    repo_name = "t-mirror"
    project = _unity_project(tmp_path)
    doomed = project / "Assets" / "Scripts" / "Doomed.cs"
    doomed.write_text("// temporary\n", encoding="utf-8")

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        await client.call_tool(
            "git_commit",
            {"branch": "main", "message": "v1", "repoName": repo_name, "sourcePath": str(project)},
        )
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})
        assert "Assets/Scripts/Doomed.cs" in _published_files(repo_name)

        doomed.unlink()
        await client.call_tool(
            "git_commit",
            {"branch": "main", "message": "v2", "repoName": repo_name, "sourcePath": str(project)},
        )
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    published = _published_files(repo_name)
    assert "Assets/Scripts/Doomed.cs" not in published
    assert "Assets/Scripts/PlayerController.cs" in published


async def test_second_commit_with_no_project_change_is_a_no_op(tmp_path):
    """Re-syncing an unchanged project must not manufacture an empty commit."""

    repo_name = "t-nochange"
    project = _unity_project(tmp_path)

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        first = await client.call_tool(
            "git_commit",
            {"branch": "main", "message": "v1", "repoName": repo_name, "sourcePath": str(project)},
        )
        second = await client.call_tool(
            "git_commit",
            {"branch": "main", "message": "v2", "repoName": repo_name, "sourcePath": str(project)},
        )

    assert first.structuredContent["commit"] == second.structuredContent["commit"]


async def test_env_fallback_supplies_the_source_without_a_contract_change(tmp_path, monkeypatch):
    """GIT_SOURCE_PATH is what makes deployment work before §03 gains the
    argument — the orchestrator sends git_commit(branch, message) only."""

    repo_name = "t-envsrc"
    project = _unity_project(tmp_path)
    monkeypatch.setenv("GIT_SOURCE_PATH", str(project))

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        # Exactly the §03 signature: no sourcePath, no repoName.
        await client.call_tool("git_commit", {"branch": "main", "message": "v1"})
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    assert "Assets/Scripts/PlayerController.cs" in _published_files(repo_name)


async def test_explicit_source_path_wins_over_the_env_fallback(tmp_path, monkeypatch):
    repo_name = "t-precedence"
    env_project = _unity_project(tmp_path / "env")
    arg_project = _unity_project(tmp_path / "arg", with_script="// from the argument\n")
    monkeypatch.setenv("GIT_SOURCE_PATH", str(env_project))

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        await client.call_tool(
            "git_commit",
            {
                "branch": "main",
                "message": "v1",
                "repoName": repo_name,
                "sourcePath": str(arg_project),
            },
        )
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    bare = gitpython.Repo(repo._bare_path(repo_name))
    blob = bare.head.commit.tree / "Assets" / "Scripts" / "PlayerController.cs"
    assert blob.data_stream.read().decode() == "// from the argument\n"


async def test_a_missing_source_directory_is_a_validation_error(tmp_path):
    async with session() as client:
        await client.call_tool("git_init", {"repoName": "t-badsrc"})
        await client.call_tool("git_branch", {"branch": "main", "repoName": "t-badsrc"})
        result = await client.call_tool(
            "git_commit",
            {
                "branch": "main",
                "message": "v1",
                "repoName": "t-badsrc",
                "sourcePath": str(tmp_path / "does-not-exist"),
            },
        )

    assert result.isError is True
    assert '"errorCode": 1000' in "".join(getattr(b, "text", "") for b in result.content)


async def test_without_a_source_the_old_behaviour_is_unchanged(tmp_path, monkeypatch):
    """The argument is additive: a caller that sends neither still commits
    whatever the clone holds, which is what the §03-only path did."""

    monkeypatch.delenv("GIT_SOURCE_PATH", raising=False)
    repo_name = "t-nosource"

    async with session() as client:
        await client.call_tool("git_init", {"repoName": repo_name})
        await client.call_tool("git_branch", {"branch": "main", "repoName": repo_name})
        _write(repo_name, "hand-placed.txt", "put here by the test\n")
        result = await client.call_tool(
            "git_commit", {"branch": "main", "message": "v1", "repoName": repo_name}
        )
        await client.call_tool("git_push", {"branch": "main", "repoName": repo_name})

    assert result.isError is False
    assert "hand-placed.txt" in _published_files(repo_name)
