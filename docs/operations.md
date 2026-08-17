# Operations

## Requirements

- Python 3.12+
- Codex CLI installed and signed in for automatic Phase Runner execution
- Unity 6 Editor and Unity MCP relay
- `RESEARCH_DSN` for Research
- `PIXELLAB_API_KEY` for Asset generation
- `MESHY_API_KEY` for 3D Asset generation

Docker, a job database, and Redis are not required. Node is required only when using npm to
install or update the standalone Codex CLI.

On Windows, Phase Runner must be able to launch a standalone Codex CLI as a child process. A
protected desktop-app binary under `WindowsApps` is not sufficient. Install and authenticate the
CLI, then verify the exact executable before starting a run:

```powershell
npm install -g @openai/codex
codex --version
codex
python scripts/bootstrap.py --check
```

When PATH discovery is ambiguous, set `GDAI_CODEX_COMMAND` to the standalone executable. An
explicit but invalid override fails closed and is not silently replaced by another PATH candidate:

```powershell
$env:GDAI_CODEX_COMMAND = Join-Path (npm prefix -g) "codex.cmd"
```

## Setup

```powershell
python scripts/bootstrap.py
python scripts/bootstrap.py --check  # verify only
```

Setup creates root `.venv`, installs MCP and test dependencies, and creates local `.env`,
`.mcp.json`, and `.codex/config.toml` without overwriting existing files. Codex reads the
project-scoped `.codex/config.toml`; `.mcp.json` remains for compatible hosts. Both default to the
`research` profile, so a new host session loads one server's tool schemas instead of every project
tool.

Select the role for the next session and restart the host after the configuration changes:

```powershell
python scripts/bootstrap.py --repair-mcp-config --mcp-profile research
python scripts/bootstrap.py --repair-mcp-config --mcp-profile unity
python scripts/bootstrap.py --repair-mcp-config --mcp-profile asset2d
python scripts/bootstrap.py --repair-mcp-config --mcp-profile asset3d
```

Use `--mcp-profile all` only for a host that cannot switch profiles between phases. The first
replacement preserves the previous local configurations as `.mcp.json.bak` and
`.codex/config.toml.bak`; later profile switches keep those original backups. The generated
profiles contain these server sets:

| Profile | MCP servers | Use |
|---|---|---|
| `research` | `research` | evidence, concepts, game design, hand-off |
| `unity` | `unity` | implementation and Unity QA |
| `asset2d` | `asset` | 2D sprites, UI, tiles, and review |
| `asset3d` | `asset3d` | 3D request, generation, and finalization |
| `all` | all four | compatibility only; largest fixed tool context |

## Automatic Phase Runner

The automatic path keeps approval, retry, and resume decisions in a deterministic local Python
controller. Every phase applies one MCP profile, starts a fresh `codex exec` thread, validates its
final response against a bounded JSON Schema, and stores only that result plus artifact paths for
the next phase. The selected MCP is marked `required`, so a phase fails instead of silently running
without its role boundary. Install and sign in to Codex CLI before the first real run; `codex exec`
reuses its saved authentication. The manual profile commands above remain the fallback.

Start a run from a prompt file. `start` executes planning only and then stops at the planning gate:

```powershell
.venv\Scripts\python.exe -m phase_runner start --prompt-file request.md
.venv\Scripts\python.exe -m phase_runner status <run-id>
```

Review the planning result under `var/runs/<run-id>/phases/planning/result.json`, then approve or
reject it. `resume` runs safe phases until it reaches the next human gate:

```powershell
.venv\Scripts\python.exe -m phase_runner approve <run-id> planning
.venv\Scripts\python.exe -m phase_runner resume <run-id>

.venv\Scripts\python.exe -m phase_runner approve <run-id> asset-generation
.venv\Scripts\python.exe -m phase_runner resume <run-id>

.venv\Scripts\python.exe -m phase_runner approve <run-id> asset-review
.venv\Scripts\python.exe -m phase_runner resume <run-id>
```

For a plan with `assetsRequired: false`, the asset gates are `not-required` and the first resume
after planning approval completes Unity implementation and final integration. Otherwise,
`visualDimension` selects `asset2d`, `asset3d`, or both sequentially. Reject a pending gate with a
reason; a rejected run is terminal:

```powershell
.venv\Scripts\python.exe -m phase_runner reject <run-id> planning --reason "기획 수정 필요"
```

`state.json` is replaced atomically. Profile changes replace `.mcp.json` and
`.codex/config.toml` as one logical operation and restore the previous contents if the second
write fails. Generated MCP process environments force UTF-8 stdin/stdout on Windows.

Each phase records `pending`, `running`, `completed`, or
`failed`, its attempt count, profile, fresh Codex thread ID, bounded result path, and error. A
process interruption can leave a phase as `running`; both `running` and `failed` require the user
to accept possible repeated external effects by invoking the explicit retry command:

```powershell
.venv\Scripts\python.exe -m phase_runner status <run-id>
.venv\Scripts\python.exe -m phase_runner retry <run-id>
.venv\Scripts\python.exe -m phase_runner resume <run-id>
```

Never approve `asset-generation` until provider cost and external changes are acceptable, and
never approve `asset-review` until the generated artifacts have been inspected. Run commands for
one run sequentially; an OS lock rejects concurrent controller processes. Full Codex JSONL events
and stderr remain beside each phase result for diagnosis but are not passed to later phases.
Before creating a run or mutating retry state, Phase Runner probes `codex --version`. A failed
execution removes stale result and log files, surfaces a bounded and credential-redacted cause
from JSONL when available, and never treats an earlier result as the current attempt. Unity phases
also fail closed unless their functional QA status is `PASS`.

Default tests use a fake executor and make no model or paid provider calls. The real CLI smoke test
is deliberately opt-in:

```powershell
$env:GDAI_RUN_CODEX_SMOKE = "1"
.venv\Scripts\python.exe -m pytest Src/McpServers/tests/test_phase_runner.py -k smoke
Remove-Item Env:GDAI_RUN_CODEX_SMOKE
```

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
| `ASSET3D_RUN_ROOT` | 3D requests, downloads, tasks, and reports | `<UNITY_PROJECT_PATH>/.asset3d-staging` |
| `ASSET_ROOT` | Asset output; relative paths resolve from the repository root | `./var/assets` |
| `HANDOFF_ROOT` | Planning files for execution AI | `./var/handoffs` |
| `UNITY_SCRIPT_ROOT` | C# root | `Assets/Scripts` |
| `UNITY_ANIMATION_ROOT` | Generated clips and animator controllers | `Assets/Animations` |
| `LOG_LEVEL` | Logging | `INFO` |

Do not use `ANTHROPIC_API_KEY`, `*_MCP_URL`, `GIT_ROOT`, or Postgres/Redis job settings.

### Meshy 3D provider

Every 3D submission uses host-supplied, human-approved reference images with Meshy. The runtime
does not automatically search or adopt local or third-party CC0 models.

`ASSET3D_RUN_ROOT` must be an absolute directory inside the external Unity project but outside
its `Assets/` directory. Relative paths and paths inside this orchestration repository are
rejected. Only a Blender `gameReadyPassed` result is copied to
`Assets/Generated3D/<featureId>/`; request metadata, source downloads, task state, and Blender
reports remain in the external staging directory.

Credit estimates follow Meshy's published API pricing for the configured operations: 20 credits
for Meshy-6 Multi-Image-to-3D, 10 for 2K retexture, and 5 for untextured T2 smart
topology image generation. The task record keeps the pricing source URL, balances before/after,
and provider-reported or balance-derived actual consumption.

The 3D Asset MCP uses [Meshy's REST API](https://docs.meshy.ai/en/api) only for Image-to-3D,
Multi-Image-to-3D, and Retexture tasks. Meshy was selected because it supports the repository's direct Unity
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

If a preserved `.mcp.json` or `.codex/config.toml` points to another checkout, inspect the warning
from bootstrap and repair it only when its backup is acceptable:

```powershell
.venv\Scripts\python.exe scripts/bootstrap.py --repair-mcp-config
```

The contract check imports no live service. For Unity integration, start the Editor and relay,
verify bridge status, and then follow [the functional QA policy](unity-functional-qa.md): compile,
focal named tests, PlayMode console smoke, regression and layout checks, then the final build.
Copy the reporter described below before calling `run_named_tests`.

### Unity named test reporter

Copy `templates/unity-editor/PipelineTestReporter.cs` and
`templates/unity-editor/PipelineTestReporter.asmdef` into the target Unity project's
`Assets/Editor/` directory. Wait for Unity compilation to finish and confirm that
`get_compile_errors.errors` is empty. A missing reporter is `INFRA_ERROR`; it is never a zero-test
pass. Keep individual game tests in the target Unity project, not in this orchestration repository.

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

### Windows Unity session CI (no build)

The normal `tests.yml` Windows job checks named-pipe discovery and a non-ASCII temporary path;
it does **not** start an Editor. To collect real PlayMode evidence without a player or WebGL
build, register a protected runner with the labels `self-hosted`, `windows`, and `unity`, then:

1. Set the repository Actions variable `UNITY_EDITOR_PATH` to that runner's `Unity.exe` path.
2. Keep a disposable Unity project on the runner, including at least one PlayMode test assembly.
3. Run **Unity session verification** manually and supply its absolute project path. The path may
   contain non-ASCII characters; the workflow passes it as one PowerShell argument rather than
   constructing a shell command.
4. Download the `unity-playmode-nunit` artifact. No XML, or a zero-test XML, is a failed test
   setup and must not be reported as gameplay verification.

This workflow uses `-runTests -testPlatform PlayMode` only; it does not produce a game build.

Verify an exported hand-off before an execution agent consumes it:

```powershell
Set-Location Src/McpServers
..\..\.venv\Scripts\python.exe -c "from pathlib import Path; from strategic.handoff import verify_handoff; print(verify_handoff(Path('../../var/handoffs/<game>/v<version>')))"
```

## Runtime output

`var/` is disposable state. Commit only minimal test fixtures needed to reproduce a bug. Never
commit user images, full generated games, or prompt experiment output.

## Context budget

Cache reads are expected for stable instructions and tool schemas. Optimize the absolute cached
and fresh token counts, not the cache-read percentage by itself:

- Keep shared policy and tool definitions stable at the start of the host prompt. Put the current
  Issue, changing state, and failure excerpts last.
- Read the Issue contract in full, but pass only relevant diff hunks and failure-adjacent console
  lines. Keep full logs, screenshots, manifests, and generated request JSON on disk and refer to
  their artifact paths.
- Keep `list_assets` at its compact default and follow `nextCursor`. Request `detail=true` only for
  the page whose prompt or provenance is needed.
- Unity evidence tools return counts, representative failures, and artifact paths by default.
  Repeat the call with `detail=true` only when full NUnit or raw bridge evidence is required.
  `run_named_tests` always returns every requested focal result because functional QA requires it.
- Do not delete `var/` as a token-cost measure. It affects prompt cost only when its contents are
  read or pasted into the conversation.

Measure 20-30 representative tasks before and after a profile or response change. Store the
working ledger under ignored `var/`, with one row per task and these fields:

```text
task_type,mcp_profile,documents_read,tool_call_count,cache_read_tokens,fresh_input_tokens,output_tokens
```

Compare medians and totals per `task_type`. Provider usage data is the authority for token counts;
repository file sizes cannot attribute billed cache reads to a particular document.
