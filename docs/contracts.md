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

- `prepare_asset_prompt` returns kind-specific intake questions before any paid generation. It
  requires the subject, intended use and readable scale, composition, must-have visual structure,
  and shared art style. Revision briefs additionally require what to preserve and a positively
  stated replacement for what should change.
- Generation takes a game ID, feature ID, host-authored prompt, and an optional explicit
  `assetKind`. Explicit kinds take precedence over keyword inference and should be used for
  ambiguous prompts.
- `generate_2d_sprite` and `generate_ui_asset` create the initial reviewable prototype through
  PixelLab's official remote MCP server.
- `generate_2d_variations` accepts only approved MCP prototypes as style anchors and uses
  PixelLab's `generate-with-style-v2` REST endpoint for same-direction batch variations. The
  primary `prototypeAssetId` plus optional `styleAssetIds` form a deduplicated bank of one to four
  references. The primary prototype fixes the output canvas size for every variation. This endpoint
  accepts square primary prototypes only; non-square character batches must use a provider-specific
  character workflow rather than silent padding or distortion.
- Prompt composition may normalize and remove duplicated structured directives, but it must
  preserve the host-authored subject intent and report original/composed character counts. The
  provider prompt orders subject and required structure before exclusions, and keeps the shared
  art style in PixelLab's structured controls instead of duplicating it in prose.
- `review_asset` stores optional `preserve`, `change`, and `artStyleFeedback` fields separately
  from the free-form review note so the next host-authored revision can distinguish content fixes
  from shared style changes.
- `inspect_asset` returns deterministic canvas, transparency, silhouette occupancy, clipping, and
  horizontal tile-seam evidence. Its `technicalStatus`, `semanticStatus`, and `humanReviewStatus`
  remain separate. `readyForVariations` applies only to approved MCP prototypes, while
  `readyForImport` marks any technically valid, human-approved asset. It also returns the next
  workflow action and escalates after three rejected attempts.
- `list_assets` lets a new host-agent session recover prior pending, approved, or rejected records
  by game and feature, including structured review feedback.
- The normal sequence is intake, one MCP prototype, human review, a revised intake when rejected,
  approval, and only then REST API variations.
- Keep all output under `ASSET_ROOT`.
- Style and manifests are runtime state, never committed source.
- Surface configured-provider failures; never hide them with fake placeholders.
- Human approval is metadata, not a model judgment.

## 3D Asset MCP

Server: `Asset3DGenMcpServer` (`asset3d.server`).

- `compose_3d_asset_prompts` deterministically derives provider-neutral generation, search, and per-view reference prompts from a host-authored asset specification.
- `validate_3d_asset_prompts` rejects prompts that differ from the current specification's deterministic derivation.
- `prepare_3d_asset_request` composes and stores the validated package beneath external
  `ASSET3D_RUN_ROOT/3d/requests`; it records SHA-256 provenance for the specification and both
  prompt artifacts. The root must be absolute, inside the external Unity workspace, outside
  `Assets/`, and outside this repository.
- `submit_3d_asset_generation` always searches local CC0 manifests and Poly Haven's official API
  first. Automatic adoption requires verified CC0 provenance. `license_rejected`,
  `quality_rejected`, and `provider_failed` never authorize Meshy; only `not_found` does. Unity
  Asset Store and non-CC0 assets are not automatic sources.
- Meshy remains the only paid generation boundary. It accepts only `text_to_3d` and
  `image_to_3d` specifications; text generation is explicitly `preview` then `refine`, while
  image generation accepts a host-supplied HTTPS image or data URI.
- Image generation creates an untextured smart-topology mesh directly at the final triangle ceiling. Reference prompts move repeated and non-silhouette microdetail to flat color or normal-map information instead of geometry.
- `refine_3d_asset_generation` runs Blender headless cleanup per imported mesh, preserves part separation, applies conservative planar cleanup only for explicit hard-surface normals, triangulates and ground-centers the result, and rejects triangle-budget, loose-vertex, or zero-area failures using the Blender quality report.
- The Blender report also exposes disconnected-component and BVH self-intersection candidates. `gameReadyPassed` covers static Unity render readiness; `topologyStrictPassed` additionally requires a manifold, intersection-free mesh for workflows such as deformation, destructive baking, or 3D printing. Exact Union and voxel remesh are not automatic defaults because they can visibly destroy valid generated surfaces.
- `material_only` is allowed only for an explicitly uniform palette: numeric `texture.material`, no `texture.surfaceDetails`, and at most one declared design color and material. Blender converts the sRGB base color to scene-linear values and applies it to every mesh; the GameReady gate rejects missing material slots. Multiple appearance regions select `generated_texture` instead of flattening visual structure into one material.
- Text preview submits a Meshy-specific geometry prompt within the provider's 600-character limit; refine submits texture requirements separately. Runtime-bound output requests triangle remeshing.
- `get_3d_asset_generation` resumes a Meshy task by provider task ID. Meshy I/O is async with a
  bounded retry/backoff policy; authentication, insufficient credit, rate, queue, provider, and
  expired-download failures are distinct. `cancel_3d_asset_generation` cancels a resumable task,
  and identical specification submissions are deduplicated before another paid task is created.
- Balance evidence and estimated/actual consumed credits are preserved with task provenance.
- Completed GLB or FBX source files remain beneath the external staging root. CC0 and Meshy
  outputs share the Blender GameReady gate, and only passing files are copied beneath Unity
  `Assets/Generated3D/<featureId>/`. Geometry-only previews do not require textures; GLB
  inspection reports vertices, triangles, and whether the requested triangle budget passed.
  GLTF is rejected because the selected provider does not return it directly.
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
