"""Local bare-repository backend for GitMcpServer.

Design decisions worth knowing before editing:

* **Package is named ``gitmcp``, not ``git``.** ``tests/conftest.py`` puts
  ``Src/McpServers`` on ``sys.path`` so servers import as top-level packages;
  a folder literally named ``git`` would shadow the installed GitPython
  package (also imported as ``git``) for every module in this project, not
  just this one. Confirmed by import-order testing before writing this module.

* **Local bare repos stand in for a real remote** (confirmed project
  decision). Each game gets its own bare repository under
  ``GIT_ROOT/bare/{repoName}.git``; a working clone at
  ``GIT_ROOT/work/{repoName}`` pushes to and pulls from it exactly like a
  real hosted remote, so switching to a hosted remote later only changes the
  remote URL, not the git plumbing below.

* **A rejected push does NOT raise — it returns a flag.** Verified
  empirically: pushing a diverged branch, or a tag name the remote already
  holds, returns a ``PushInfo`` with ``flags=1032`` and summary
  ``[rejected] (...)`` and does *not* raise ``GitCommandError``. Every push
  in this module therefore checks ``PushInfo.ERROR`` explicitly; a
  ``try/except GitCommandError`` alone would silently report success. This
  is why ``_push_ref`` exists rather than each caller pushing directly.

* **The remote is the source of truth for tag conflicts.** A working clone
  only learns about tags at clone/fetch time, so a tag pushed by an earlier
  deployment from a *different* clone is invisible locally. Checking only
  ``repo.tags`` would let this server create a duplicate tag locally, push it,
  have the push rejected, and report success — with the remote tag pointing
  at a completely different commit than the one just deployed.

* **Five of the seven §03 tools carry no repoName.** ``git_branch``,
  ``git_pull``, ``git_commit``, ``git_push`` and ``git_tag`` take only
  ``branch``/``message``/``tag`` per the contract (``verify_contract.py``),
  and the orchestrator opens a fresh MCP session *per call* (§03 §2), so
  there is no session state to carry a repo identity across calls either.
  Every function here accepts an optional ``repo_name`` for a future
  contract update; when omitted, it falls back to the most recently
  established repo (module-level, process-local). That fallback is correct
  as long as jobs run serially (``MAX_CONCURRENT_JOBS=1``, the orchestrator's
  current default) — concurrent jobs would need the orchestrator to start
  passing ``repoName`` explicitly on every call, not just ``git_init``.

* **``git_commit`` is idempotent by construction, not by policy.** If the
  working tree has no changes to stage, it returns the current HEAD instead
  of creating an empty commit — because ``git_commit`` is never retried
  (§03 §6), a caller that lost the response must still get back a valid
  commit hash rather than an error.

* **Something has to put the game into the clone.** §03's seven Git tools take
  no path and no content, and the Unity project lives wherever
  ``UNITY_PROJECT_PATH`` points on the Unity side — not in this server's
  working clone. Left as the contract defines it, every deployment reports
  success and publishes an empty commit (measured). ``git_commit`` therefore
  takes an optional ``sourcePath`` and, when it is omitted, falls back to
  ``GIT_SOURCE_PATH``; the env fallback is what makes deployment work today
  without a contract change, and the argument is what the orchestrator should
  send once §03 gains it. See ``Doc/설계/05_계약_변경_제안서.md`` §1.

* **The sync is a mirror, and it skips what Unity regenerates.** A Unity
  project carries ``Library/``, ``Temp/`` and ``obj/`` — machine-local caches
  that are routinely larger than the game itself and must never be published.
  A plain copy would commit them. Deletions must propagate too, or a script
  removed from the project lives on in the repository forever.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

import git
from git.exc import GitCommandError
from git.remote import PushInfo

from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error

logger = logging.getLogger("GitMcpServer.repo")

ROOT = Path(os.getenv("GIT_ROOT", "./git_output")).resolve()
BARE_DIR = ROOT / "bare"
WORK_DIR = ROOT / "work"

# Directories Unity (and the usual toolchain around it) regenerates. Copying
# them would publish hundreds of megabytes of machine-local cache and, in
# Library/'s case, an artifact database that is meaningless on another machine.
# Matched by exact directory name at any depth.
IGNORED_DIRS = frozenset(
    {
        ".git",
        "Library",
        "Temp",
        "Obj",
        "obj",
        "Build",
        "Builds",
        "Logs",
        "UserSettings",
        "MemoryCaptures",
        "Recordings",
        ".vs",
        ".idea",
        ".gradle",
        "__pycache__",
        ".venv",
        "node_modules",
        ".pytest_cache",
    }
)

# IDE project files Unity regenerates from the .asmdef/.csproj graph.
IGNORED_SUFFIXES = (
    ".csproj",
    ".sln",
    ".unityproj",
    ".user",
    ".booproj",
    ".pidb",
    ".suo",
    ".userprefs",
    ".pyc",
)

# Written into every repository so the published history carries the same
# rules, and so a human cloning it does not re-add the caches by hand.
_GITIGNORE = """# Unity 가 재생성하는 것들 — GitMcpServer 가 동기화에서도 제외한다.
[Ll]ibrary/
[Tt]emp/
[Oo]bj/
[Bb]uild/
[Bb]uilds/
[Ll]ogs/
[Uu]serSettings/
[Mm]emoryCaptures/
[Rr]ecordings/

# IDE / 도구
.vs/
.idea/
.gradle/
*.csproj
*.sln
*.unityproj
*.user
*.booproj
*.pidb
*.suo
*.userprefs

# OS
.DS_Store
Thumbs.db
"""

_lock = threading.Lock()
_last_repo: str | None = None


def _bare_path(repo_name: str) -> Path:
    return BARE_DIR / f"{repo_name}.git"


def _work_path(repo_name: str) -> Path:
    return WORK_DIR / repo_name


def _remember(repo_name: str) -> None:
    global _last_repo
    with _lock:
        _last_repo = repo_name


def _current_repo() -> str | None:
    """Read the fallback repo under the lock, like every other access."""

    with _lock:
        return _last_repo


def resolve_repo(explicit: str | None) -> str:
    """Resolve which repo a repoName-less tool call targets (see module docstring)."""

    if explicit and explicit.strip():
        return explicit.strip()
    current = _current_repo()
    if current is None:
        raise tool_error(
            VALIDATION_ERROR,
            "no repository established yet: call git_init first, or pass repoName explicitly",
        )
    return current


def _open_work_repo(repo_name: str) -> git.Repo:
    path = _work_path(repo_name)
    if not path.exists():
        raise tool_error(
            VALIDATION_ERROR, f"unknown repository: {repo_name!r} (call git_init first)"
        )
    return git.Repo(path)


def _ls_remote(repo: git.Repo, *args: str) -> str:
    """``git ls-remote`` with transport failures mapped to a §03 error code."""

    try:
        return repo.git.ls_remote(*args)
    except GitCommandError as exc:
        raise tool_error(MCP_ERROR, f"git ls-remote failed: {exc}") from exc


def _push_ref(repo: git.Repo, refspec: str, what: str) -> None:
    """Push ``refspec``, treating a rejection flag as the failure it is.

    A rejected push returns a flag instead of raising (see module docstring),
    so both paths must be handled or failures are reported as successes.
    """

    try:
        results = repo.remotes.origin.push(refspec)
    except GitCommandError as exc:
        raise tool_error(MCP_ERROR, f"git {what} push failed: {exc}") from exc

    if not results:
        raise tool_error(MCP_ERROR, f"git {what} push returned no result for {refspec!r}")

    info = results[0]
    if info.flags & PushInfo.ERROR:
        raise tool_error(MCP_ERROR, f"git {what} push rejected: {info.summary.strip()}")


def init_repo(repo_name: str) -> bool:
    """Create the bare repo + working clone if absent; reuse as-is otherwise.

    Returns ``True`` if a new bare repository was created, ``False`` if an
    existing one was reused — this is the ``created`` field §03 requires.
    """

    BARE_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    bare_path = _bare_path(repo_name)
    work_path = _work_path(repo_name)

    created = not bare_path.exists()
    if created:
        git.Repo.init(bare_path, bare=True, initial_branch="main")

    if not work_path.exists():
        clone = git.Repo.clone_from(bare_path, work_path)
        with clone.config_writer() as cfg:
            cfg.set_value("user", "name", "GitMcpServer")
            cfg.set_value("user", "email", "gitmcpserver@local")

    _remember(repo_name)
    # NOTE: the key must not be "created" — that is a reserved ``LogRecord``
    # attribute (the record timestamp), and passing it via ``extra`` raises
    # KeyError inside ``Logger.makeRecord``, turning this whole tool into a
    # failure. See tests/test_logging_safety.py.
    logger.info("Repository ready", extra={"repo": repo_name, "newly_created": created})
    return created


def checkout_branch(repo_name: str, branch: str) -> None:
    repo = _open_work_repo(repo_name)
    _remember(repo_name)

    if not repo.head.is_valid():
        # Unborn HEAD (no commits in this clone yet): point the symbolic ref at
        # the target branch so the first commit lands on it, rather than on
        # whatever "git init" defaulted to on this machine.
        repo.git.symbolic_ref("HEAD", f"refs/heads/{branch}")
        logger.info("Branch set on unborn HEAD", extra={"repo": repo_name, "branch": branch})
        return

    if branch in (head.name for head in repo.heads):
        repo.git.checkout(branch)
    else:
        repo.git.checkout("-b", branch)
    logger.info("Branch checked out", extra={"repo": repo_name, "branch": branch})


def pull_branch(repo_name: str, branch: str) -> bool:
    """Fetch and merge ``branch`` from origin. Returns True if HEAD moved.

    A local unborn HEAD does *not* mean there is nothing to pull: another job
    may have already pushed ``branch`` to this repo's bare remote before this
    working copy caught up. Only an actually empty remote ref is a true no-op,
    checked with ``ls-remote`` first — ``git pull`` on a branch the remote has
    never heard of raises instead of doing nothing.
    """

    repo = _open_work_repo(repo_name)
    _remember(repo_name)

    if not _ls_remote(repo, "origin", branch).strip():
        logger.info(
            "Pull skipped: remote branch does not exist",
            extra={"repo": repo_name, "branch": branch},
        )
        return False

    before = repo.head.commit.hexsha if repo.head.is_valid() else None
    try:
        repo.remotes.origin.pull(branch)
    except GitCommandError as exc:
        raise tool_error(MCP_ERROR, f"git pull failed: {exc}") from exc

    after = repo.head.commit.hexsha if repo.head.is_valid() else None
    updated = after != before
    logger.info("Pull complete", extra={"repo": repo_name, "branch": branch, "updated": updated})
    return updated


def resolve_source(explicit: str | None) -> Path | None:
    """Where the game's files live, or None if nothing is configured.

    The argument wins; ``GIT_SOURCE_PATH`` is the fallback that lets deployment
    work before §03 gains the argument (see the module docstring).
    """

    raw = (explicit or "").strip() or os.getenv("GIT_SOURCE_PATH", "").strip()
    if not raw:
        return None

    source = Path(raw).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    if not source.is_dir():
        raise tool_error(VALIDATION_ERROR, f"소스 경로가 디렉터리가 아닙니다: {source}")
    return source


def _is_ignored(relative: Path) -> bool:
    """True for anything Unity regenerates (see IGNORED_DIRS / IGNORED_SUFFIXES)."""

    if any(part in IGNORED_DIRS for part in relative.parts):
        return True
    return relative.suffix in IGNORED_SUFFIXES


def _same_file(left: Path, right: Path) -> bool:
    """Cheap change test: identical size and modification time.

    Hashing every file of a Unity project on every deployment would dominate
    the commit; size+mtime is what rsync-style tools settle on for the same
    reason.
    """

    try:
        a, b = left.stat(), right.stat()
    except OSError:
        return False
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def sync_source(repo_name: str, source: Path) -> dict[str, int]:
    """Mirror ``source`` into the working clone, minus the ignored paths.

    A mirror, not a copy: files deleted from the project are deleted from the
    clone too, so a removed script does not live on in the published history.
    ``.git`` is never touched — it is in IGNORED_DIRS for the source walk and
    excluded explicitly from the destination walk.
    """

    work = _work_path(repo_name)
    copied = deleted = 0

    wanted: set[Path] = set()
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if _is_ignored(relative):
            continue
        target = work / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        wanted.add(relative)
        if target.exists() and _same_file(path, target):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1

    for path in sorted(work.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        relative = path.relative_to(work)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_dir():
            # Prune directories the project no longer has, once emptied above.
            if not any(path.iterdir()) and not (source / relative).is_dir():
                path.rmdir()
            continue
        if relative.name == ".gitignore" and len(relative.parts) == 1:
            continue  # ours, not the project's
        if relative not in wanted:
            path.unlink()
            deleted += 1

    gitignore = work / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE, encoding="utf-8")

    logger.info(
        "Source synced into working clone",
        extra={"repo": repo_name, "source": str(source), "copied": copied, "removed": deleted},
    )
    return {"copied": copied, "deleted": deleted}


def commit_all(repo_name: str, message: str, source: Path | None = None) -> str:
    """Stage and commit everything in the working tree; a no-op tree returns HEAD as-is."""

    repo = _open_work_repo(repo_name)
    _remember(repo_name)

    if source is not None:
        sync_source(repo_name, source)

    repo.git.add(all=True)
    if repo.head.is_valid() and not repo.is_dirty(untracked_files=True):
        head_sha = repo.head.commit.hexsha
        logger.info(
            "Commit skipped: nothing to commit", extra={"repo": repo_name, "commit": head_sha}
        )
        return head_sha

    try:
        commit = repo.index.commit(message)
    except GitCommandError as exc:
        raise tool_error(MCP_ERROR, f"git commit failed: {exc}") from exc

    tracked = sum(1 for _ in commit.tree.traverse())
    logger.info(
        "Commit created",
        extra={"repo": repo_name, "commit": commit.hexsha, "tracked_entries": tracked},
    )
    if tracked == 0:
        # Not an error here — but it is the signature of the pipeline shipping
        # an empty repository, so it must be visible in the logs.
        logger.warning(
            "Commit contains no files; nothing was staged into the working clone",
            extra={
                "repo": repo_name,
                "commit": commit.hexsha,
                "work_dir": str(_work_path(repo_name)),
            },
        )
    return commit.hexsha


def push_branch(repo_name: str, branch: str) -> None:
    repo = _open_work_repo(repo_name)
    _remember(repo_name)

    if not repo.head.is_valid():
        raise tool_error(VALIDATION_ERROR, "nothing to push: no commits exist yet")

    _push_ref(repo, f"{branch}:{branch}", "branch")
    logger.info("Branch pushed", extra={"repo": repo_name, "branch": branch})


def _remote_tag_sha(repo: git.Repo, tag: str) -> str | None:
    """Commit that ``tag`` points at on the remote, or None if absent.

    An annotated tag yields two ``ls-remote`` lines: the tag object and a
    ``^{}`` peeled line naming the commit. The peeled line wins when present.
    """

    output = _ls_remote(repo, "--tags", "origin", f"refs/tags/{tag}")
    plain: str | None = None
    peeled: str | None = None
    for line in output.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if ref == f"refs/tags/{tag}":
            plain = sha.strip()
        elif ref == f"refs/tags/{tag}^{{}}":
            peeled = sha.strip()
    return peeled or plain


def create_tag(repo_name: str, tag: str) -> None:
    """Tag HEAD and publish the tag, refusing to move an existing tag.

    Idempotent when the tag already points at the commit being deployed;
    a validation error when the same name already points somewhere else,
    whether that name exists locally or only on the remote.
    """

    repo = _open_work_repo(repo_name)
    _remember(repo_name)

    if not repo.head.is_valid():
        raise tool_error(VALIDATION_ERROR, "nothing to tag: no commits exist yet")

    head_sha = repo.head.commit.hexsha

    remote_sha = _remote_tag_sha(repo, tag)
    if remote_sha is not None:
        if remote_sha == head_sha:
            logger.info(
                "Tag already published at this commit",
                extra={"repo": repo_name, "tag": tag, "commit": head_sha},
            )
            return
        raise tool_error(
            VALIDATION_ERROR,
            f"tag {tag!r} already exists on the remote at commit {remote_sha[:8]}, "
            f"which is not the commit being deployed ({head_sha[:8]})",
        )

    local = next((t for t in repo.tags if t.name == tag), None)
    if local is not None:
        if local.commit.hexsha != head_sha:
            raise tool_error(
                VALIDATION_ERROR,
                f"tag {tag!r} already exists locally at commit {local.commit.hexsha[:8]}, "
                f"which is not the commit being deployed ({head_sha[:8]})",
            )
        # Local tag is correct but was never published: push it and stop.
        _push_ref(repo, f"refs/tags/{tag}", "tag")
        logger.info("Existing local tag published", extra={"repo": repo_name, "tag": tag})
        return

    repo.create_tag(tag)
    try:
        _push_ref(repo, f"refs/tags/{tag}", "tag")
    except Exception:
        # Undo the local tag so a later retry is not blocked by this server's
        # own half-finished state.
        repo.delete_tag(tag)
        raise
    logger.info("Tag created and pushed", extra={"repo": repo_name, "tag": tag, "commit": head_sha})


def is_clean(repo_name: str | None) -> bool:
    """True if the working tree has no pending changes. No repo yet counts as clean."""

    if repo_name is None and _current_repo() is None:
        return True
    repo = _open_work_repo(resolve_repo(repo_name))
    return not repo.is_dirty(untracked_files=True)
