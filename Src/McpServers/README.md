# MCP Boundaries

These stdio servers expose external boundaries for the Agent-first workflow.

| Module | Server | Responsibility |
|---|---|---|
| `strategic.server` | `ResearchMcpServer` | Evidence retrieval; blueprint/spec storage and lint |
| `unity.server` | `UnityMcpServer` | C#/design validation; Editor effects, build, and checks |
| `asset.server` | `AssetGenMcpServer` | PixelLab generation; style and review metadata |

The host agent reasons. These servers do not call models, host HTTP APIs, orchestrate state, or
proxy QA/Git. See `docs/contracts.md` and `docs/operations.md`.
