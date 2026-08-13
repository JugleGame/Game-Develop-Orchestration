# MCP Boundaries

These stdio servers expose external boundaries for the Agent-first workflow.

| Module | Server | Responsibility |
|---|---|---|
| `strategic.server` | `ResearchMcpServer` | Evidence retrieval; blueprint/spec storage and lint |
| `unity.server` | `UnityMcpServer` | C#/design validation; Editor effects, build, and checks |
| `asset.server` | `AssetGenMcpServer` | PixelLab 2D generation; style and review metadata |
| `asset3d.server` | `Asset3DGenMcpServer` | Provider-neutral 3D prompt validation and request packages |

The host agent reasons. These servers do not call models, host HTTP APIs, orchestrate state, or
proxy QA/Git. See `docs/contracts.md` and `docs/operations.md`.
