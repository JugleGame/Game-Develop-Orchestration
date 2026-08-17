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
phase_runner/            Deterministic approval gates and fresh Codex phase execution
issue_runner/            Approval-gated fresh Codex execution for repository Issue work
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
- Unity feature QA workflow and test contract: [docs/unity-functional-qa.md](docs/unity-functional-qa.md)
- GitHub Issue-based work and required repository settings: [docs/github-issue-workflow.md](docs/github-issue-workflow.md)

## Workflow

1. Interpret the user's idea and query Research for evidence and counterexamples.
2. Draft a blueprint and feature specs; obtain user approval.
3. Draft the architecture and C#.
4. For every Unity feature, follow [the functional QA policy](docs/unity-functional-qa.md):
   define its named test, implement it, then run compile, focal functional, console smoke,
   regression, and layout checks. Run the final build only after those gates pass.
5. Use Asset MCP to generate and validate required assets.
6. Judge the collected evidence. Retry a repeated failure at most three times, then escalate.
7. After user approval, use native Git to commit, push, or tag.

`build_project` success and `run_playmode_test` success alone never prove that a feature works.

## Setup

```powershell
python scripts/bootstrap.py
```

This creates context-efficient `research`-only MCP configurations for Codex
(`.codex/config.toml`) and compatible hosts (`.mcp.json`) by default. Switch to the role needed
for the next task, then restart the host agent:

```powershell
python scripts/bootstrap.py --repair-mcp-config --mcp-profile unity
```

Available profiles are `research`, `unity`, `asset2d`, `asset3d`, and the explicit compatibility
profile `all`. Fill only the required values in `.env`; see [docs/operations.md](docs/operations.md)
for profile and verification commands.

For an approval-gated end-to-end run, use the local Python Phase Runner. It starts a fresh
`codex exec` thread per role and persists resumable state under ignored `var/runs/`:

```powershell
.venv\Scripts\python.exe -m phase_runner start --prompt-file request.md
```

See the [Phase Runner guide](phase_runner/README.md) for the complete CLI workflow, approval,
rejection, resume, and failure recovery. The broader environment and manual MCP profile procedures
remain in [docs/operations.md](docs/operations.md#automatic-phase-runner).

For this repository's own Issue-backed maintenance, ask `Issue #N 작업 시작` in a new Desktop
session. The repository-local Skill validates the complete open Issue and starts the separate
[Issue Work Runner](issue_runner/README.md), which gates implementation on analysis approval and
uses fresh Codex threads for analysis, implementation, verification, and review.

## Development

- Python 3.12+, type hints, async I/O for external calls.
- Put business and validation rules in testable pure functions.
- Do not add speculative features, one-use interfaces, or unused options.
- Return explicit MCP error codes and causes; do not swallow failures.
- Confirm scope before cost, file writes, external APIs, or Git changes.
- Keep completed investigation in Git history, not new permanent reports.
