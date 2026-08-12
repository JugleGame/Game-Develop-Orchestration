# Game Develop Orchestration

An Agent-first workflow where Codex or Claude Code coordinates Unity game development across 2D and 3D projects.
The host agent reasons and decides; MCP servers only validate data or access external systems.

## Rules

- The host agent is the only reasoning layer. Never call an LLM from an MCP server.
- Keep only real external boundaries as MCP servers: Research, Unity, and Asset.
- Use native Git only within the user's approved scope.
- Store generated assets and run state under `var/`; never commit them.
- Keep shared policy here and in `docs/`, not in agent-specific copies.

## Layout

```text
docs/                    Authoritative architecture, contracts, operations, backlog
Src/McpServers/          Research, Unity, and Asset MCP servers
AGENTS.md                Thin Codex adapter
CLAUDE.md                Thin Claude Code adapter
scripts/bootstrap.py     Local environment and MCP client setup
var/                     Reproducible runtime output (ignored)
```

Read only what the task needs:

- Structure: [docs/architecture.md](docs/architecture.md)
- MCP tools and ownership: [docs/contracts.md](docs/contracts.md)
- Setup and runtime: [docs/operations.md](docs/operations.md)
- Open work: [docs/backlog.md](docs/backlog.md)
- AI migration, hand-off, graph, asset, and Unity-tool boards: [docs/guide-boards.md](docs/guide-boards.md)

## Workflow

1. Interpret the user's idea and query Research for evidence and counterexamples.
2. Draft a blueprint and feature specs; obtain user approval.
3. Draft the architecture and C#.
4. Use Unity MCP to validate, apply, assemble, build, and run PlayMode checks.
5. Use Asset MCP to generate and validate required assets.
6. Judge the collected evidence. Retry a repeated failure at most three times, then escalate.
7. After user approval, use native Git to commit, push, or tag.

## Setup

```powershell
python scripts/bootstrap.py
```

Fill only the required values in `.env`, then restart the host agent. See
[docs/operations.md](docs/operations.md) for commands.

## Development

- Python 3.12+, type hints, async I/O for external calls.
- Put business and validation rules in testable pure functions.
- Do not add speculative features, one-use interfaces, or unused options.
- Return explicit MCP error codes and causes; do not swallow failures.
- Confirm scope before cost, file writes, external APIs, or Git changes.
- Keep completed investigation in Git history, not new permanent reports.
