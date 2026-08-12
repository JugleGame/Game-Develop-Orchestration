# Backlog

Keep completed work in Git history. This file contains only open validation.

## Unity

- Build the first approved vertical slice as WebGL and verify its browser launch before adding a second gameplay system.
- Verify that a real Editor reports compile errors through `get_compile_errors` accurately.
- Define limits and alternatives for long-running PlayMode conditions.
- Verify scene/prefab attachment with `inspect_project_layout` on a generated project.

## Research

- Re-measure ARCH expectations and the counterexample floor in semantic-search mode.
- Recheck Neon TLS on Windows paths containing non-ASCII characters.
- In a real deployment, decide whether Research should own credentials and the port-443 fallback.

## Asset

- Add regression checks for image size, transparency, and palette.
- Define external retention location and duration for reviewed originals.
- Measure concurrent tileset and map-object requests. Their current bounded polling is synchronous;
  convert the outer MCP tools to asynchronous waiting only if this blocks real asset throughput.

## Host compatibility

- Compare `tools/list` and required schemas for all three servers in Codex and Claude Code.
- Verify one real Claude Code restart after `.mcp.json` generation.

## Handoff integration

- Run one published design through `export_execution_handoff` against a real Research DB and verify the manifest digests from a separate execution-agent session.
- Confirm that a draft revision, a rejected asset, and a failed acceptance criterion return to planning without bypassing the published hand-off boundary.
