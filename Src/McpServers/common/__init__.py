"""Shared plumbing for the three Agent-first MCP boundaries.

Importing this package applies the repository-root ``.env`` (see
``common/env.py``). Every server module reads its configuration with
``os.getenv`` while its body executes, and each one imports ``common`` first,
so this is the last point where a value can still be injected.
"""

from common.env import load_repo_env

load_repo_env()
