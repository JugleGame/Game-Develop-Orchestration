"""Test fixtures for the MCP tool servers."""

import os
import sys
import tempfile
from pathlib import Path

# Servers are imported as top-level packages (asset.server, common.server).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# `import common` applies the repository .env (common/env.py). A developer's
# local file must not decide what the suite asserts, so opt out before any
# server package is imported.
os.environ["GDAI_SKIP_DOTENV"] = "1"

# Every test writes into a throwaway root so runs never touch real output.
os.environ.setdefault("ASSET_ROOT", tempfile.mkdtemp(prefix="mcpservers_test_"))
