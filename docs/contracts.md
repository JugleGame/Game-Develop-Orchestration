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

The stored blueprint contains game-level fields, including the host-authored
`visualDimension` (`2D`, `3D`, or `hybrid`), and `specIds`, while each full
task exists only in its feature spec. The generated package includes an external
`execution-manifest.sha256`; execution must verify it before reading the manifest.

For 3D work, `unityHints.assetSpecs` carries complete host-authored 3D specifications.
The exported feature prompt exposes them as `asset_specs`; the execution host passes
each object unchanged to `submit_3d_asset_generation`. `assets_needed` remains the
lightweight and backward-compatible display list.

## Unity MCP

Server: `UnityMcpServer`.

- `design_architecture.design` is required; validate files, types, scenes, prefabs, and cycles.
- `create_script.contents` is required; run the C# gate before writing to Unity.
- External effects: `create_scene`, `create_prefab`, `compose_scene`, `bind_reference`,
  `define_assemblies`, `import_asset`.
- `create_prefab.model` accepts an imported Unity `GameObject` asset such as FBX and saves a
  model-backed prefab; `compose_scene` then instantiates that prefab.
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

- `compose_3d_asset_prompts` deterministically derives provider-neutral generation, search, and per-view reference prompts from a host-authored asset specification.
- `validate_3d_asset_prompts` rejects prompts that differ from the current specification's deterministic derivation.
- `prepare_3d_asset_request` composes and stores the validated package beneath `ASSET_ROOT/3d/requests`; it records SHA-256 provenance for the specification and both prompt artifacts.
- Meshy is the single configured provider boundary. `submit_3d_asset_generation` accepts only `text_to_3d` and `image_to_3d` specifications; text generation is explicitly `preview` then `refine`, while image generation accepts a host-supplied HTTPS image or data URI.
- Image generation creates an untextured smart-topology mesh directly at the final triangle ceiling. Reference prompts move repeated and non-silhouette microdetail to flat color or normal-map information instead of geometry.
- `refine_3d_asset_generation` runs Blender headless cleanup per imported mesh, preserves part separation, applies conservative planar cleanup only for explicit hard-surface normals, triangulates and ground-centers the result, and rejects triangle-budget, loose-vertex, or zero-area failures using the Blender quality report.
- The Blender report also exposes disconnected-component and BVH self-intersection candidates. `gameReadyPassed` covers static Unity render readiness; `topologyStrictPassed` additionally requires a manifold, intersection-free mesh for workflows such as deformation, destructive baking, or 3D printing. Exact Union and voxel remesh are not automatic defaults because they can visibly destroy valid generated surfaces.
- If `texture.material` provides numeric material values and `texture.surfaceDetails` is empty, the server selects `material_only`; Blender converts the declared sRGB base color to scene-linear values, applies the material to every mesh, and Meshy Retexture is skipped. The GameReady gate rejects a required material with missing mesh material slots. Otherwise it selects `generated_texture`.
- Text preview submits a Meshy-specific geometry prompt within the provider's 600-character limit; refine submits texture requirements separately. Runtime-bound output requests triangle remeshing.
- `get_3d_asset_generation` returns a Meshy task state and downloads only a completed GLB or FBX beneath `ASSET_ROOT/3d/models`. Geometry-only previews do not require textures; GLB inspection reports vertices, triangles, and whether the requested triangle budget passed. GLTF is rejected because the selected provider does not return it directly.
- `MESHY_API_KEY` is read only from the environment. A missing key, insufficient credits, or provider failure is an explicit MCP error; the server never falls back to a 2D placeholder.
- Meshy is an external-provider boundary only. It must not call a model to author prompts or hide provider failures.
- Prompt fields and the Slime example are defined in [3D asset prompt contract](3d-asset-prompts.md).

## Role-boundary lint

Planning defines **what** a feature does; development defines **which files and types** implement
it. Reject C# type names, file paths, and concrete MonoBehaviour names in specs. If code adds an
unapproved game rule, ask whether the spec should change.

## Git

Git is not an MCP contract. Resolve exact targets and use native Git only within user-approved
scope. Never guess a repository or automatically push or tag.
