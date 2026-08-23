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


_BRIEF_FIELDS = ("gridSize", "paletteLock", "initAssetId", "initImageStrength", "direction")


async def sprite_call(client, arguments):
    """Call ``generate_2d_sprite`` through the brief the tool now requires.

    ``generate_2d_sprite`` refuses to guess a generation parameter, so a test
    that wants one has to answer it first. This helper answers the intake with
    exactly what the call passes — omitted parameters are answered as "nothing"
    (0 / "none" / the old ``paletteLock`` default), which is what the tool used
    to assume silently.
    """

    answers = {
        "assetKind": arguments.get("assetKind") or "prop",
        "subject": arguments.get("prompt") or "a test subject",
        "purpose": "an automated test",
        "composition": "full body centered",
        "mustHave": ["a readable silhouette"],
        "avoid": ["blur"],
        "artStyle": arguments.get("artStyle") or "pixel art",
        "gridSize": arguments.get("gridSize") or 0,
        "paletteLock": arguments.get("paletteLock", True),
        "initAssetId": arguments.get("initAssetId") or "none",
        "initImageStrength": arguments.get("initImageStrength") or 0,
        "direction": arguments.get("direction") or "none",
    }
    prepared = await client.call_tool("prepare_asset_prompt", answers)
    # An intake that itself refused (an invalid assetKind, say) has no brief to
    # hand over. Passing a placeholder keeps the refusal the test is after
    # instead of replacing it with one about the brief.
    body = prepared.structured_content or {}
    brief_id = body.get("briefId", "no-brief")
    passed = {key: value for key, value in arguments.items() if key not in _BRIEF_FIELDS}
    # The brief owns the prompt as well as the parameters now, so the call has
    # to carry what the intake composed rather than the caller's own wording.
    # A refused intake composed nothing; leave the argument alone there so the
    # test still gets the refusal it is after.
    if body.get("prompt"):
        passed["prompt"] = body["prompt"]
    return await client.call_tool("generate_2d_sprite", {**passed, "briefId": brief_id})


async def ui_call(client, arguments):
    """Call ``generate_ui_asset`` through the brief it now requires.

    It shares ``_generate_prototype`` with ``generate_2d_sprite``, so it takes
    the same brief gate; without one it was the way around the canonical
    prompt.
    """

    answers = {
        "assetKind": arguments.get("assetKind") or "ui_panel",
        "subject": arguments.get("prompt") or "a test panel",
        "purpose": "an automated test",
        "composition": "a clean border",
        "mustHave": ["a readable border"],
        "avoid": ["blur"],
        "artStyle": arguments.get("artStyle") or "pixel art",
        "gridSize": 0,
        "paletteLock": True,
        "initAssetId": "none",
        "initImageStrength": 0,
        "direction": "none",
    }
    prepared = await client.call_tool("prepare_asset_prompt", answers)
    body = prepared.structured_content or {}
    passed = dict(arguments)
    if body.get("prompt"):
        passed["prompt"] = body["prompt"]
    return await client.call_tool(
        "generate_ui_asset", {**passed, "briefId": body.get("briefId", "no-brief")}
    )
