# MCP Contracts

## Common

- Local `stdio` transport.
- Return `dict[str, Any]` through `structuredContent`.
- Use camelCase input names.
- Reject empty required values, unsafe paths, and schema errors with code `1000`.
- Return the failed Unity/external tool and root cause.
- MCP validates, stores, or performs external effects; it does not reason or author content.

## Research MCP

Server: `ResearchMcpServer`.

| Tool | Responsibility |
|---|---|
| `research_idea` | Retrieve evidence, counterexamples, and architecture cards |
| `propose_concept` | Store an evidence-backed proposal for review |
| `list_pending_concepts`, `get_concept` | Read concept review state |
| `decide_concept` | Record approve, revise, or reject |
| `publish_game_design` | Validate and store a host-authored blueprint and specs; default to an editable draft |
| `list_specs`, `get_spec` | Read specs; only published specs include implementation prompts |
| `revise_spec`, `add_spec` | Lint and store host-authored specs |
| `export_execution_handoff` | Export published, dependency-valid planning files for an execution AI |
| `research_status` | Report DB and search mode |

Research does not author titles, goals, scope, or acceptance criteria. The host must provide
complete documents to `publish_game_design`, `revise_spec`, and `add_spec`.

`publish_game_design` accepts `blueprint.status` of `draft` (the default) or
`published`. Drafts never return execution prompts. `export_execution_handoff`
accepts only a fully published graph and writes versioned, SHA-256-described
files beneath `HANDOFF_ROOT` (default `./var/handoffs`).

The stored blueprint contains game-level fields and `specIds`, while each full
task exists only in its feature spec. The generated package includes an external
`execution-manifest.sha256`; execution must verify it before reading the manifest.

## Unity MCP

Server: `UnityMcpServer`.

- `design_architecture.design` is required; validate files, types, scenes, prefabs, and cycles.
- `create_script.contents` is required; run the C# gate before writing to Unity.
- External effects: `create_scene`, `create_prefab`, `compose_scene`, `bind_reference`,
  `define_assemblies`, `import_asset`.
- Evidence: `build_project`, `run_playmode_test`, `get_compile_errors`,
  `inspect_project_layout`, `unity_bridge_status`.
- Return evidence; never declare final PASS.

## Asset MCP

Server: `AssetGenMcpServer`.

- Generation takes a game ID, feature ID, asset kind, and host-authored prompt.
- Keep all output under `ASSET_ROOT`.
- Style and manifests are runtime state, never committed source.
- Surface configured-provider failures; never hide them with fake placeholders.
- Human approval is metadata, not a model judgment.

## 3D Asset MCP

Server: `Asset3DGenMcpServer` (`asset3d.server`).

- `validate_3d_asset_prompts` validates a structured asset specification plus its derived generation and reference-search prompts.
- `prepare_3d_asset_request` stores that validated package beneath `ASSET_ROOT/3d/requests`; it records SHA-256 provenance for all three inputs.
- No 3D provider is configured in this repository. The server returns `provider_unconfigured`, never a model path or a 2D placeholder.
- A future provider client may perform only external-provider access in this server. It must not call a model to author prompts or hide provider failures.
- Prompt fields and the Slime example are defined in [3D asset prompt contract](3d-asset-prompts.md).

## Role-boundary lint

Planning defines **what** a feature does; development defines **which files and types** implement
it. Reject C# type names, file paths, and concrete MonoBehaviour names in specs. If code adds an
unapproved game rule, ask whether the spec should change.

## Git

Git is not an MCP contract. Resolve exact targets and use native Git only within user-approved
scope. Never guess a repository or automatically push or tag.
