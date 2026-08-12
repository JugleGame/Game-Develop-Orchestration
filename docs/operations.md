# Operations

## Requirements

- Python 3.12+
- Unity 6 Editor and Unity MCP relay
- `RESEARCH_DSN` for Research
- `PIXELLAB_API_KEY` for Asset generation

Node, Docker, a job database, and Redis are not required.

## Setup

```powershell
python scripts/bootstrap.py
python scripts/bootstrap.py --check  # verify only
```

Setup creates root `.venv`, installs MCP and test dependencies, and creates local `.env` and
`.mcp.json` without overwriting existing files.

## Environment

| Variable | Used for | Default |
|---|---|---|
| `RESEARCH_DSN` | Research MCP | none |
| `UNITY_PROJECT_PATH` | Unity writes and builds | none |
| `PIXELLAB_API_KEY` | Asset generation | none |
| `ASSET_ROOT` | Asset output | `./var/assets` |
| `UNITY_SCRIPT_ROOT` | C# root | `Assets/Scripts` |
| `LOG_LEVEL` | Logging | `INFO` |

Do not use `ANTHROPIC_API_KEY`, `*_MCP_URL`, `GIT_ROOT`, or Postgres/Redis job settings.

## Direct execution

```powershell
.venv\Scripts\python.exe -m strategic.server
.venv\Scripts\python.exe -m unity.server
.venv\Scripts\python.exe -m asset.server
```

`strategic.server` remains as a compatibility module path; its registered name and role are
`ResearchMcpServer`.

## Verification

```powershell
.venv\Scripts\python.exe -m pytest Src/McpServers/tests
.venv\Scripts\python.exe Src/McpServers/verify_contract.py
```

The contract check imports no live service. For Unity integration, start the Editor and relay,
then verify bridge status, build, PlayMode, and layout in that order.

## Runtime output

`var/` is disposable state. Commit only minimal test fixtures needed to reproduce a bug. Never
commit user images, full generated games, or prompt experiment output.
