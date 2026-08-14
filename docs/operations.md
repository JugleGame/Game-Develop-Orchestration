# Operations

## Requirements

- Python 3.12+
- Unity 6 Editor and Unity MCP relay
- `RESEARCH_DSN` for Research
- `PIXELLAB_API_KEY` for Asset generation
- `MESHY_API_KEY` for 3D Asset generation

Node, Docker, a job database, and Redis are not required.

## Setup

```powershell
python scripts/bootstrap.py
python scripts/bootstrap.py --check  # verify only
```

Setup creates root `.venv`, installs MCP and test dependencies, and creates local `.env` and
`.mcp.json` without overwriting existing files.

`requirements.lock` fixes the verified Python dependency set. Bootstrap installs
that lock file first, then installs the local MCP package without resolving a
new dependency graph or downloading isolated build dependencies.

## Environment

| Variable | Used for | Default |
|---|---|---|
| `RESEARCH_DSN` | Research MCP | none |
| `UNITY_PROJECT_PATH` | Unity writes and builds | none |
| `UNITY_EDITOR_PID` | Optional explicit Unity Editor process target | auto-discover |
| `UNITY_BUILD_TARGET` | Validated Unity build target | `WebGL` |
| `UNITY_BUILD_OUTPUT` | Optional project-relative path under `Builds/` | target default |
| `PIXELLAB_API_KEY` | Asset generation | none |
| `MESHY_API_KEY` | Meshy 3D Asset generation | none |
| `BLENDER_PATH` | Blender headless mesh cleanup | `blender` on `PATH` |
| `ASSET_ROOT` | Asset output | `./var/assets` |
| `HANDOFF_ROOT` | Planning files for execution AI | `./var/handoffs` |
| `UNITY_SCRIPT_ROOT` | C# root | `Assets/Scripts` |
| `LOG_LEVEL` | Logging | `INFO` |

Do not use `ANTHROPIC_API_KEY`, `*_MCP_URL`, `GIT_ROOT`, or Postgres/Redis job settings.

### Meshy 3D provider

The 3D Asset MCP uses [Meshy's REST API](https://docs.meshy.ai/en/api) for text-to-3D and
image-to-3D tasks. Meshy was selected because it supports the repository's direct Unity
interchange formats (GLB and FBX), API-key authentication, task polling, and explicit
credit errors. Text tasks require a Meshy preview followed by refine; image tasks require
an HTTPS reference image owned or licensed by the caller.

An approved operator must create the Meshy account, purchase API credits if required, and
store the one-time-visible key only in the local `.env` or an approved secret store as
`MESHY_API_KEY`. Never commit, log, or paste the key into an MCP prompt. Meshy documents
that paid customers own generated assets; free-plan output uses CC BY 4.0 attribution.
Review its current [API pricing](https://docs.meshy.ai/en/api/pricing) and
[commercial-use terms](https://help.meshy.ai/en/articles/9992001-can-i-use-my-generated-assets-for-commercial-projects)
before buying credits or publishing generated assets.

## Direct execution

```powershell
.venv\Scripts\python.exe -m strategic.server
.venv\Scripts\python.exe -m unity.server
.venv\Scripts\python.exe -m asset.server
.venv\Scripts\python.exe -m asset3d.server
```

`strategic.server` remains as a compatibility module path; its registered name and role are
`ResearchMcpServer`.

## Verification

```powershell
.venv\Scripts\python.exe -m pytest Src/McpServers/tests
.venv\Scripts\python.exe Src/McpServers/verify_contract.py
```

If a preserved `.mcp.json` points to another checkout, inspect the warning from
bootstrap and repair it only when its backup is acceptable:

```powershell
.venv\Scripts\python.exe scripts/bootstrap.py --repair-mcp-config
```

The contract check imports no live service. For Unity integration, start the Editor and relay,
then verify bridge status, build, PlayMode, and layout in that order.

### Unity MCP bridge connection

The Windows relay executable alone is not a connection: the Unity-side bridge must also be
running. If `unity_bridge_status` reports a timeout or named-pipe `ECONNREFUSED`, do the
following in the open Unity project before retrying:

1. Open **Edit > Project Settings > AI > Unity MCP Server** and select **Start**. Its status
   must show running.
2. Use **Check Status**. If it reports a problem, keep **Show Debug Logs** enabled only while
   diagnosing it.
3. In **Integrations**, reconfigure the current MCP client when offered. The client command
   must include `--mcp`.
4. Restart Unity and the MCP client, then call `unity_bridge_status` again.

Do not disable process validation or auto-approve connections as a workaround. Approve the
specific pending client in the same settings page when Unity requests approval.

Verify an exported hand-off before an execution agent consumes it:

```powershell
Set-Location Src/McpServers
..\..\.venv\Scripts\python.exe -c "from pathlib import Path; from strategic.handoff import verify_handoff; print(verify_handoff(Path('../../var/handoffs/<game>/v<version>')))"
```

## Runtime output

`var/` is disposable state. Commit only minimal test fixtures needed to reproduce a bug. Never
commit user images, full generated games, or prompt experiment output.
