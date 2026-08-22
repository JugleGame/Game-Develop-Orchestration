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
| `get_unity_project_setup_guidance` | Return the Unity Hub project template and initial settings for an explicit `2D` or `3D` visual dimension; it has no project-creation or GitHub Issue side effect |
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
- `design_architecture.scene.objects[].transform` and `compose_scene.objects[].transform` accept
  optional `position`, Euler `rotation`, and `scale` vectors. Each vector contains exactly three
  finite numbers. Omitted transforms keep Unity defaults, and child transforms are applied in local
  space. The older flat `position`, `rotation`, and `scale` compose arguments remain compatible.
- `create_prefab` also accepts `colliderSize`, `colliderOffset`, and `spritePivot`. The body
  size a script assumes and the collider on the prefab are one decision, so they are set in one
  call: a collider left at Unity's 1x1 default under a 2x4 sprite is a defect that nothing else
  in the pipeline reports. Asking for a collider measurement without a `Collider2D` component,
  or a pivot without a sprite, is rejected with code `1000`. Omitting them keeps the previous
  behaviour, and the applied values come back as evidence.
- `import_asset` repairs generated texture import settings after importing an FBX: maps named
  `*normal*` become `NormalMap` and `*metallic*`, `*roughness*`, `*occlusion*` lose sRGB.
- The same step applies the WebGL texture budget. Base color is capped at
  `UNITY_MAX_TEXTURE_SIZE` (1024 by default) and every other map at half that, all maps are
  crunch-compressed, and a WebGL platform override pins `DXT1Crunched`, or `DXT5Crunched` when the
  map needs alpha. Crunch trades import time for download size, which is the cost WebGL pays.
- It then binds base color, normal, `*metallicSmoothness*`, and emission into one URP material
  beside the model and remaps the model's embedded materials to it, because the FBX importer binds
  base color and normal only. Results are reported as `texturesRepaired` and `material`.
- `create_animation_clip` builds one clip from an ordered frame list; the order given is the play
  order, and each frame must already be an imported Sprite. `create_animator_controller` builds the
  state graph from host-authored states, parameters, and transitions, then binds the controller to a
  prefab's `Animator`. A transition whose condition names an undeclared parameter is rejected before
  Unity sees it, because Unity ignores such a transition silently. Clips and controllers are written
  under `UNITY_ANIMATION_ROOT` (`Assets/Animations` by default).
- `inspect_animator` returns what an Animator actually carries — controller path, states with their
  motions, parameters with their types, and per-clip frame counts — without judging it. An `Animator`
  with no controller raises no error at runtime, so `inspect_project_layout` reports that case as
  layout rule `L8`.
- `run_named_tests` runs the tests an acceptance criterion names, in EditMode or PlayMode, and
  always returns each requested test with its status, duration, and failure message because the
  functional QA contract must match every requested name to an executed result. `run_playmode_test` only
  collects console errors, so a defect that throws nothing passes it; a named test is what turns
  a criterion such as `Test_Player_NoDoubleJump` into evidence. A filter that matches no test is
  reported as an error, never as a pass. Because a run crosses a domain reload, results are
  recorded by `templates/unity-editor/PipelineTestReporter.cs`, which the target project must
  carry in `Assets/Editor`; its absence is reported as such.
- The host must apply [the Unity functional QA policy](unity-functional-qa.md). A named run is
  acceptable evidence only when it completed, executed at least one requested test, and reports no
  failure. `run_playmode_test` is console-smoke evidence only. Neither it nor a successful build can
  replace a named functional test, and the MCP never assigns the final QA status.
- `run_playmode_smoke` only detects runtime console errors during a bounded PlayMode
  session. `run_playmode_test` remains a deprecated compatibility alias; neither tool
  proves gameplay behavior. `run_playmode_function_tests` runs the installed Unity Test
  Framework's PlayMode tests and returns per-test evidence plus the produced NUnit XML path;
  zero discovered cases are a configuration error.
- `unity_bridge_status` reports the local Editor and Test Framework versions found in the
  configured project even when the relay is disconnected.
- Evidence: `build_project`, `run_playmode_smoke`, `run_playmode_test`,
  `run_playmode_function_tests`, `run_named_tests`, `get_compile_errors`,
  `inspect_project_layout`, `unity_bridge_status`, `inspect_animator`.
- Potentially large Unity evidence is compact by default. Build results omit the raw bridge payload,
  while compile, smoke, and Unity Test Framework results return total counts plus at most five
  representative failures. Use `detail=true` for the full payload or NUnit per-test records.
  `run_named_tests` is deliberately exempt and retains all focal per-test evidence. NUnit artifact
  paths remain available in compact results.
- Return evidence; never declare final PASS.

## Asset MCP

Server: `AssetGenMcpServer`.

- `prepare_asset_prompt` returns kind-specific intake questions before any paid generation. It
  requires the subject, intended use and readable scale, composition, must-have visual structure,
  and shared art style. Revision briefs additionally require what to preserve and a positively
  stated replacement for what should change.
- **`generate_2d_sprite` requires `assetKind`.** It is typed as the `AssetKind` literal, so the
  accepted values are published in the tool schema and a missing or wrong one is refused by the
  schema before any provider call. The kind is not guessed from prompt wording, because one wrong
  guess sets the canvas ratio, the forced palette, the shading, and the framing together — and that
  is paid for in generation credits and human review time, not by the caller who omitted an
  argument. `prepare_asset_prompt` already requires the same value.
- `generate_ui_asset` still infers when `assetKind` is omitted. Every outcome there is a UI kind, so
  a wrong guess picks the wrong UI shape rather than turning a character into a tile.
- Responses and provenance carry `kindSource` / `kind_source`: `"explicit"` when the caller named
  the kind, `"inferred"` when it came from keyword matching.
- `render.classify`'s keyword table no longer carries per-keyword exceptions. The one-syllable `적`
  is gone (it matched inside ordinary words such as `도적`, and forced a third matching rule), as is
  `cta`. English keywords are still matched on word boundaries, because they appear inside unrelated
  words — `tile` in `volatile`, `rock` in `rocket`.
- `generate_2d_sprite` and `generate_ui_asset` create the initial reviewable prototype through
  PixelLab's official remote MCP server.
- Canvas size comes from the game's locked pixel grid times a per-kind ratio. `generate_2d_sprite`
  additionally accepts `gridSize`, which replaces that grid for one asset only, so things of
  different in-world size (a boy and the giant chasing him) generate at the same pixel density
  without editing the game's stored style. The stored grid never changes, and omitting `gridSize`
  keeps the previous size and the previous asset id.
- Both derived sides must fall inside PixelLab's 16-400px per-side range, which is what
  `CreateImagePixfluxRequest.image_size` declares. A request outside it fails validation before any
  provider call, naming the derived size and the range. The floor previously sat at 32 on the
  strength of one 16x32 request that failed through the MCP path with a `TaskGroup` exception
  naming no cause; that failure was never explained and the schema contradicts it, so the contract
  follows the schema. A character generated under 32 wide still crops the figure below the thigh,
  which is a composition caution, not a validation rule.
- `/map-objects` has its own floor: `CreateMapObjectRequest.image_size` starts at 32, not 16.
- A prototype request claims its prompt digest under `var/assets/submissions/<assetId>.json` before
  PixelLab is paid, and the claim always records how the request ended. A failure that never
  reached PixelLab's meter is stored as `FAILED` with `billable: false`, and the next call with the
  same prompt retakes the claim and generates. That retry must reuse the prompt verbatim: the seed
  is derived from the prompt text, so rewording it to work around a block produces a different
  asset.
- A failure that PixelLab may already have billed (`billable: true`), or a claim left at
  `SUBMITTING` because the process died mid-call, keeps blocking that prompt. The tool answers
  `status: "duplicate_blocked"` with `claimPath`, the recorded `reason`, and a `recovery` sentence:
  review the earlier request, delete the file at `claimPath`, then call again with the same prompt.
- PixelLab MCP failures arrive inside a `TaskGroup`, whose own message names no cause. The client
  flattens the group to its leaf exceptions, so the returned message carries the provider's real
  error type and text.
- `generate_2d_variations` takes any approved PixelLab asset of the same game as a style anchor.
  The primary `prototypeAssetId` plus optional `styleAssetIds` form a deduplicated bank of one to
  four references. Anchor eligibility used to require `provenance.method == "pixellab-mcp"`
  exactly, which excluded every REST-generated asset and every approved variation; what matters is
  that a human approved a PixelLab image of this game, not which endpoint drew it. A tileset or
  other non-sprite kind is still refused, because its `asset_path` is a JSON index.
- **There is no square requirement.** Every character is a 1:2 kind, so every character prototype
  is non-square; the previous square gate made the server's only style-reference path unusable for
  exactly the assets whose style matters most. Neither endpoint requires a square reference.
- The batch picks its endpoint from the reference count and the native canvas:

  | condition | endpoint | what it gives up |
  | --- | --- | --- |
  | 1 reference, ≤200px per side | `create-image-bitforge` | at most one reference |
  | otherwise | `generate-with-style-v2` | `styleStrength`, `coveragePercentage`, `negativeDescription` |

  The chosen endpoint is returned as `endpoint` and recorded in each asset's provenance.
  `styleStrength`, `coveragePercentage`, and `negativeDescription` exist only on the bitforge path;
  passing one when the batch would fall back is a validation error, not a silent no-op.
- The provider is asked for the **native** canvas, not the stored one. Sprites are stored at four
  times their generated size, so requesting the stored size would generate a different asset — and
  a 128x256 request exceeds bitforge's 200px limit outright. The result is upscaled to the stored
  size with nearest-neighbour, as the sprite paths do.
- `style_strength` is 0-100 and its schema default is **0**, which means "ignore the style image".
  A reference sent with no explicit strength therefore gets the schema's own documented midpoint,
  50 (`BITFORGE_BALANCED_STYLE_STRENGTH`); leaving the provider default would silently discard the
  reference.
- `generate-with-style-v2` takes no output size. `GenerateWithStyleV2Request.image_size` is marked
  `deprecated` with the description `REMOVED. Output size is deduced from the style images.`, so
  no size is sent and the returned size is recorded rather than checked against a size that was
  never requested. Reference images are capped at 512px per side, which is the model's own size.
- `create-image-bitforge` stops at 200px per side, half of pixflux's 400. That is the trade for its
  controls: a larger asset still has to go through `generate_2d_sprite`.
- **The style reference must be exactly the requested canvas.** This is not in the schema and the
  provider reports it as a 500, not a 422: a 128x256 reference against a 32x64 request answered
  `style_image must be size (64, 32), not torch.Size([256, 128])` (measured 2026-08-22). The client
  therefore resizes `style_image` and `init_image` to the requested canvas before sending. Stored
  sprites are upscaled copies of their generated canvas, so this is normally an exact integer
  downscale back to the pixels the reference was drawn at. A reference whose *aspect* differs from
  the target — a 1:2 character anchoring a 1:1 prop — is squashed, so anchor a kind with its own
  aspect ratio.
- **Measured, and it is not what "style transfer" suggests** (2026-08-22, shared seed per prompt,
  `var/assets/experiments/round-1-character/` and `round-1-prop/`). `style_image` carries the
  reference's *subject*, not only its look, and it outranks the description:

  | prompt | anchor | pixflux | bitforge s30 / s50 / s80 |
  | --- | --- | --- | --- |
  | a **blue**-cloaked girl with a lantern | **red**-cloaked boy with a lantern | blue cloak, as asked | red cloak at every strength; pose and proportions follow the anchor; at 80 the face is gone and a second lantern appears |
  | a small iron **lantern** | a wooden **chest** | a clean lantern | a chest at every strength; at 80 it is the anchor with a glowing panel |

  The prop row is the important one: the described subject never appeared. Treat `style_image` as
  "another take on *this* asset", not "a different asset drawn in this asset's style".

  | goal | path |
  | --- | --- |
  | a new subject exactly as described | pixflux (`generate_2d_sprite`) |
  | variations of an asset that already exists | bitforge (`generate_2d_variations`) |
  | a *different* subject sharing a game's look | neither — use the locked palette and structured style fields |

  This is why `generate_2d_variations` is the right home for bitforge: its prompts vary an existing
  prototype ("red treasure chest", "blue treasure chest"), which is exactly the case the endpoint
  serves. A prompt there that names a different subject comes back as the anchor.
- `styleStrength` above ~50 buys reference adherence by overriding the description, including
  colours the prompt names. Below 30 was not measured. Treat it as a knob to turn down, not up.
- `readyForVariations` means "this asset can anchor a batch" and is true for any approved PixelLab
  asset, including an approved variation. `nextAction` still distinguishes a prototype from a
  batch's output: an approved variation is told to `import_asset`, not to generate more variations.
- `detail` and `shading` are fields of the game's frozen `ArtStyle`, set once through
  `establish_art_style` and stored in `var/assets/styles/<gameId>.json`. They are not per-call
  arguments: a game whose concept art holds two tones per material needs the same setting on every
  asset. A style file written before these fields existed still loads and keeps the previous
  defaults (`medium detail`; `medium shading` for characters and monsters). `shading` stays
  flattened for inanimate kinds whatever the game asks for.
- `establish_art_style` rejects a `detail` or `shading` outside the schema before freezing the
  style. The value is written into the game's style file and read by every later asset, so an
  invalid one would otherwise fail every generation in that game with no way back short of editing
  the frozen file by hand.
- **PixelLab's structured style fields are `(weakly guiding)`, in its own words.** Every one of
  them says so in `CreateImagePixfluxRequest`:

  ```
  outline    "Outline style reference (weakly guiding)"
  shading    "Shading style reference (weakly guiding)"
  detail     "Detail style reference (weakly guiding)"
  view       "Camera view angle (weakly guiding)"
  direction  "Subject direction (weakly guiding)"
  isometric  "Generate in isometric view (weakly guiding)"
  ```

  They bias a result; they do not override a description. Prompt composition therefore **keeps**
  style wording rather than deleting it as a duplicate of a field — the previous policy removed the
  strong signal and left only the weak one. Both are sent. `promptMetrics.structuredClauses` lists
  which clauses a field also covers; `removedStructuredClauses` is retained as an empty list so
  existing readers do not break.
- Framing is always appended. It used to be skipped whenever a prompt contained both `Composition:`
  and `Required visual structure:` — exactly what `prepare_asset_prompt` writes — so following the
  intake procedure was the one reliable way to lose it.
- `direction` is which way the subject faces: `north`, `north-east`, `east`, `south-east`, `south`,
  `south-west`, `west`, `north-west`. It is locked per game like the view, and `generate_2d_sprite`
  and `generate_2d_variations` accept a per-asset override for the cases that genuinely differ (a
  door on the west wall, an NPC turned toward the player). A value outside the enum is refused
  before the request. `CreateTilesetRequest` and `CreateMapObjectRequest` do not declare the field
  at all, so passing one there is a caller error rather than a wrong value.
- `isometric` is a boolean, **not** a camera view. Top-down looks straight down a vertical axis;
  isometric looks along a diagonal one. The keyword used to be listed in the `CameraView` table, so
  a game asking for isometric was sent `high top-down` and no field ever carried the request. It is
  now its own locked `ArtStyle` field; such a game still gets `high top-down` as its view, plus the
  boolean.
- A Wang tileset derives its boundary from its two terrain descriptions differing, so the tile
  prototype path splits the prompt on `|` — `grass meadow | grey stone cliff`. It used to send the
  same text as both, which left nothing to transition between. A prompt with no separator is
  refused with that instruction rather than silently producing a flat set.
- Measured (2026-08-22, `var/assets/experiments/round-2-compose/`, one character and one prop, same
  seed per subject): `direction: west` visibly turned the walking character around, while the prop —
  a symmetric lantern — was unchanged, which is the correct behaviour for something with no facing.
  Keeping the style wording instead of deleting it was a **small** effect: the prop's shading came
  out flatter and its glass more rectangular, closer to the requested `flat shading`; the character
  was near-identical. The change rests on the schema quote above, not on a large visual difference.
- Structured style values are checked against the endpoint's own declared enum before the request is
  sent, so a wrong value costs no generation and the error names the accepted values. **The allowed
  values differ per endpoint** and the differences are not guessable, so they live in one table,
  `pixellab_client.STYLE_ENUMS`:

  | field | `create-image-pixflux` | `tilesets` | `map-objects` |
  | --- | --- | --- | --- |
  | `outline` | `single color black outline`, `single color outline`, `selective outline`, `lineless` | same as pixflux | `single color outline`, `selective outline`, `lineless` |
  | `shading` | `flat shading`, `basic shading`, `medium shading`, `detailed shading`, `highly detailed shading` | same as pixflux | `flat shading`, `basic shading`, `medium shading`, `detailed shading` |
  | `detail` | `low detail`, `medium detail`, `highly detailed` | same as pixflux | `low detail`, `medium detail`, `high detail` |
  | `view` | `side`, `low top-down`, `high top-down` | `low top-down`, `high top-down` | `low top-down`, `high top-down`, `side` |

  The server therefore translates the locked `ArtStyle` onto `/map-objects`' vocabulary rather than
  passing it through: `single color black outline` becomes `single color outline` and
  `highly detailed` becomes `high detail`.
- A map object that is sent no style takes the endpoint's defaults, and its `view` default is
  `high top-down` — which drew a side-view game's decorations as if seen from above.
  `generate_map_object` therefore always sends the game's locked `view`, outline, shading, and
  detail, plus the material palette.
- Palettes travel as `color_image`, a base64 PNG PixelLab samples colours from. Neither
  `CreateImagePixfluxRequest`, `CreateTilesetRequest`, nor `CreateMapObjectRequest` has an
  array-of-colours or string palette field.
- `text_guidance_scale` is how literally the description is followed, 1-20, provider default 8.
  Both generation paths send the same value. The MCP path previously hard-coded 16 while the REST
  path sent nothing at all, so two assets in one game were generated at different strengths.
- `tileSize` is an enum, not a range: `TileSize` declares 16, 32, and 64, and 64 additionally
  requires a `pro` mode this server never sends, so only 16 and 32 are accepted. Values such as 24
  and 48 sit inside the old 16-64 range and are still rejected by the provider.
- Negations never reach the provider's **description**, and on the bitforge path they are recovered
  as `negative_description`. That field is live in `CreateImageBitforgeRequest`
  (`Text description of what to avoid in the generated image`) and `(Deprecated)` in
  `CreateImagePixfluxRequest`, so the same clause is usable on one path and inert on the other.
  `promptMetrics.negativeDescription` reports what would be sent: `no city, without buildings`
  becomes `city, buildings`, with the negation word taken off — the field wants the thing to avoid,
  not the instruction to avoid it.
- Negations never reach the provider description. PixelLab draws the noun and ignores the negation:
  `no city, no buildings, no street` returned a city, while the same subject without those clauses
  returned none, and the positive `empty background` worked. `prepare_asset_prompt` therefore
  returns `avoid` answers as `exclusions` instead of writing them into the prompt, and prompt
  composition removes clause-leading negations and reports them in `promptMetrics.removedNegations`.
  Restate an exclusion positively in `mustHave`. An inline negation (`a knight with no helmet`) is
  left alone, because dropping the clause would drop the subject with it.
- A larger canvas biases the provider toward drawing a scene instead of a subject. 56x112 requests
  returned an opaque city background five times out of five (opaque pixel ratio 0.46-0.76) where the
  same prompt family at 32x64 returned a clean sprite. Prefer the smaller canvas for a subject, and
  inspect opacity before accepting a large one.
- `character` and `monster` prototypes are generated without a forced palette; only tile, prop, and
  UI kinds lock the game ramp. A change to that game palette therefore does not affect character
  generation.
- Prompt composition may normalize and remove duplicated structured directives, but it must
  preserve the host-authored subject intent and report original/composed character counts. The
  provider prompt orders subject and required structure before exclusions, and keeps the shared
  art style in PixelLab's structured controls **and** in prose: the controls are `(weakly guiding)`
  and do not replace the description.
- `generate_2d_animation` turns one approved prototype into an ordered frame sequence through
  PixelLab's `animate-with-text-v3`. The approved asset is submitted as the first frame, so the
  human gate that guards a static sprite also guards every frame derived from it. The endpoint
  accepts only an even frame count of 4 to 16 and answers 422 otherwise, so any other value is
  rejected before a request is spent. It returns one image more than requested, because the first
  frame is echoed at the head of the sequence. The request always sets
  `no_background`: the endpoint defaults it to false and then returns every frame on an opaque
  plate, which cannot be used as a sprite. Each frame is inspected as it is saved, so a sequence
  that still comes back opaque is reported as `technicalStatus: fail` with
  `transparent_background_missing` instead of waiting for a later `inspect_asset` call.
  The frames are also measured *as a motion*, which a per-image check cannot do: subject drift
  between frames, subject size stability, how much of the canvas each step redraws, and how far
  the last frame sits from the first. Those come back as `sequenceWarnings` and `sequenceMetrics`.
  They are warnings, never failures — a sequence that legitimately crosses the canvas measures the
  same as one that shakes in place, so the judgment stays with the host and the human.
  Each frame also carries a `footAnchor`: the normalised pivot at the bottom centre of its own
  subject. Unity anchors a sprite by its canvas unless told otherwise, so without it a few pixels
  of drift per frame become on-screen shake. Frames land under `ASSET_ROOT/assets/<game>/
  animations/<feature>_<digest>/` with zero-padded names that sort into play order, beside an
  `animation.json` index recording the action, first-frame asset ID, provider job ID, and usage.
  Each frame is an ordinary manifest asset: it starts `pending` and needs the same human review
  before `readyForImport`. A poll timeout, a failed provider job, and a rejected request are
  distinct failures.
- `review_asset` stores optional `preserve`, `change`, and `artStyleFeedback` fields separately
  from the free-form review note so the next host-authored revision can distinguish content fixes
  from shared style changes.
- `inspect_asset` returns deterministic canvas, transparency, silhouette occupancy, clipping, and
  horizontal tile-seam evidence. Its `technicalStatus`, `semanticStatus`, and `humanReviewStatus`
  remain separate. `readyForVariations` applies only to approved MCP prototypes, while
  `readyForImport` marks any technically valid, human-approved asset. It also returns the next
  workflow action and escalates after three rejected attempts.
- `list_assets` lets a new host-agent session recover prior pending, approved, or rejected records
  by game and feature. It defaults to compact pages of 20 records, accepts `limit` from 1 to 100,
  and returns `nextCursor`. Compact records retain identity, state, path, and structured review
  feedback. Use `detail=true` only for a page that needs full prompts and provenance.
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
- `submit_3d_asset_generation` sends only host-supplied, human-approved reference images to
  Meshy. It does not automatically search or adopt third-party assets.
- The current 3D provider path supports static models only. Asset specifications with
  `animation.required: true` are rejected until an approved rigging and clip-generation path
  can create and verify the requested output.
- Meshy remains the only paid generation boundary. The host may use the GPT image API to turn the
  user's prompt into reference images, but the MCP server never calls GPT or another model. The
  host passes one to four user-approved references plus `referenceProvenance` to the MCP.
- Every 3D request defaults to a single GPT-generated, consistent front/side/back contact sheet,
  followed by human approval and Meshy Multi-Image-to-3D. `referenceContactSheetUrl` must contain
  three equal-width left-to-right panels (allowing a one-pixel rounding difference) of that one
  generation; independently generated images stitched together are invalid. The server splits the
  sheet into three PNG inputs before
  submission. Single-image submission is disabled;
  `text_to_3d` is rejected by the asset specification contract and cannot be used as a fallback.
- The reference-generation plan exposes a required visual review checklist. It checks one-object
  consistency, declared-palette-only appearance, absent undeclared details, plus every explicit
  `design.preserve` and `design.exclude` constraint. The same checklist is retained with the
  staged final preview; failed checks must not be finalized into Unity.
- `referenceProvenance` requires a non-empty source (for example `gpt_image_api`),
  `humanApproved: true`, and `captureMode: single_generation_contact_sheet`; an optional
  `sourcePromptSha256` records the user-prompt lineage without storing the prompt itself.
  Reference prompts move repeated and non-silhouette microdetail to flat color or normal-map
  information instead of geometry.
- `refine_3d_asset_generation` requires explicit human geometry-preview approval before any paid
  refine/retexture. Completed output is staged as `AWAITING_FINAL_REVIEW`.
- Retexture always submits FBX. Meshy returns GLB geometry, so Blender converts the cleaned GLB to
  FBX before submission and the task requests an FBX result. `finalize_3d_asset_generation` still
  converts to `assetSpec.output.format` when the specification asks for GLB.
- A Unity-targeted asset specification should request `output.format: fbx`; the stock Unity
  importer does not load GLB as a prefab-ready `GameObject` without an additional importer.
- Geometry is rebuilt once, during refine. Every later Blender pass runs in preserve mode: it keeps
  vertices, planar faces, and UVs untouched and only re-applies transforms, triangulates, grounds,
  and exports. Merging or dissolving a textured mesh would destroy the UV layout the maps were
  baked against.
- FBX exports write their maps into a sidecar `<model>.fbm` folder, and the Unity copy keeps that
  folder name so the relative references resolve. Unity cannot extract embedded FBX media on its
  own, so embedding is not used. A `generated_texture` result with no image in that sidecar is
  rejected before the FBX is copied; Unity must not accept a silently white material. Separate
  metallic and roughness maps are packed into one
  `*_metallicSmoothness.png` (metallic in RGB, inverted roughness in alpha) for URP, and the two
  consumed inputs are deleted. An emission map whose brightest pixel is at or below
  `EMPTY_MAP_THRESHOLD` carries no light and is dropped rather than shipped.
- Each asset lands in its own folder, `Assets/Generated3D/<featureId>/<assetId>-<sha12>/`, holding
  the model and its texture sidecar. The Unity import step treats everything in that folder as
  belonging to that one model.
- `assetSpec.geometry.maxTriangles` must stay at or below the WebGL per-asset triangle ceiling,
  15000 by default and overridable with `ASSET3D_WEBGL_MAX_TRIANGLES`. The ceiling is enforced at
  specification validation, Meshy requests the same number as `target_polycount`, Blender decimates
  anything above it, and the GameReady gate rejects a report whose `triangleBudgetPassed` is false.
- `finalize_3d_asset_generation` requires explicit human final-visual approval, then runs Blender
  headless cleanup, preserves part separation, applies conservative planar cleanup only for
  explicit hard-surface normals, triangulates and ground-centers the result, and rejects
  triangle-budget, loose-vertex, or zero-area failures using the Blender quality report. Only then
  can it copy the asset into Unity.
- The Blender report also exposes disconnected-component and BVH self-intersection candidates. `gameReadyPassed` covers static Unity render readiness; `topologyStrictPassed` additionally requires a manifold, intersection-free mesh for workflows such as deformation, destructive baking, or 3D printing. Exact Union and voxel remesh are not automatic defaults because they can visibly destroy valid generated surfaces.
- `material_only` is allowed only for an explicitly uniform palette: numeric `texture.material`, no `texture.surfaceDetails`, and at most one declared design color and material. Blender converts the sRGB base color to scene-linear values and applies it to every mesh; the GameReady gate rejects missing material slots. Multiple appearance regions select `generated_texture` instead of flattening visual structure into one material.
- Image generation preserves the complete host-authored specification and submits texture
  requirements separately during retexture. The texture prompt restricts the provider to the
  declared palette and material, preserves explicit design constraints, and prohibits invented
  colors, patterns, text, logos, symbols, and accessories. Runtime-bound output requests triangle remeshing.
- A successful geometry poll persists Meshy's thumbnail URLs with its provider evidence, so a
  later approval gate can render the same preview without parsing provider-specific evidence.
- `get_3d_asset_generation` resumes a Meshy task by provider task ID. Meshy I/O is async with a
  bounded retry/backoff policy; authentication, insufficient credit, rate, queue, provider, and
  expired-download failures are distinct. `cancel_3d_asset_generation` cancels a resumable task,
  and identical specification submissions are deduplicated before another paid task is created.
- Balance evidence and estimated/actual consumed credits are preserved with task provenance.
- Completed GLB or FBX source files remain beneath the external staging root. Meshy outputs pass
  the Blender GameReady gate, and only passing files are copied beneath Unity
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

S7 contamination findings remain fatal unless the host records an exact
`contaminationAcceptance` entry with `cardId`, `guardId`, and a non-empty `reason`. The record means
the named card text is only a cross-reference and the referenced system is absent from this game.
It exempts only that card and guard pair; another card or another contamination guard still fails.
The stored spec Markdown and exported feature prompt retain every acceptance record so development
and QA can see the human judgment instead of silently dropping source guidance.

## Git

Git is not an MCP contract. Resolve exact targets and use native Git only within user-approved
scope. Never guess a repository or automatically push or tag.
