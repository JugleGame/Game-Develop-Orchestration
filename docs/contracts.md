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
- **The intake also asks about the generation parameters**: `gridSize`, `paletteLock`,
  `initAssetId`, `initImageStrength`, and `direction`. `None` means unanswered; `0` and `"none"`
  are answers meaning "the game's locked grid", "no reference", "no direction override". These were
  optional arguments with defaults until an agent that never asked the user still generated —
  measured on `daeume` (2026-08-22), it swept `gridSize` through 80, 64, 48, 40, and 32,
  `paletteLock` through both values, and `initImageStrength` through 400 and 700, ten-plus billed
  generations to rediscover settings one question would have settled.
- `prepare_asset_prompt` returns a `briefId` and stores the brief under `briefs/` in the asset root.
  The id is the hash of the brief, so answering one more question yields a new id and the id is
  evidence of which answers were on the table.
- **`generate_2d_sprite` requires `briefId`.** An unknown brief, or one with unanswered required
  questions, is refused before any provider call. The five parameters above are read from the
  brief; passing one at the call site is allowed only to restate what the brief answered, and a
  contradicting value is refused rather than preferred, so a user's answer cannot be overridden at
  the call site. `assetKind` is validated first, so a wrong kind is reported as a wrong kind rather
  than sending the caller back to the intake step.
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
- **There is no square requirement.** Neither endpoint requires a square reference. The gate that
  used to demand one made the server's only style-reference path unusable for the assets whose
  style matters most, back when `character` was a 1:2 kind. It is square now, so a single-reference
  character batch reaches bitforge and keeps that endpoint's controls.
- The batch picks its endpoint from the reference count and the native canvas:

  | condition | endpoint | what it gives up |
  | --- | --- | --- |
  | 1 reference, **square**, ≤200px per side | `create-image-bitforge` | at most one reference |
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
  downscale back to the pixels the reference was drawn at.
- **A reference whose aspect differs from the canvas is padded, not stretched.** `_fit_to_canvas`
  scales it by one factor and leaves the rest transparent, placing the subject **bottom-centred** —
  a side-view sprite stands on the bottom of its canvas, which is where the generator is being
  asked to put it. Measured 2026-08-23: `var/concept-art/daeume/hero-sprite.png` is 66x161 with no
  transparent margin, and stretching it into a 64x64 character request generated two and then three
  overlapping figures at `initImageStrength` 900 and 600 alike — the strength was not the problem,
  the squashed reference was. A squashed human reads as several humans. References that already
  match the canvas aspect, which is every stored sprite of the same kind, take the plain resize and
  are unchanged by this.
- `generate-with-style-v2` needs no padding: it deduces the output size from the references instead
  of demanding a canvas, so each one is scaled by a single factor to the 512px cap and never
  squashed. Only bitforge has to match an exact canvas.
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
  which clauses a field also covers.
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
- **Characters and monsters are palette-locked too**, using `ArtStyle.character_palette()` — twelve
  swatches: the game's identity ramp first, then skin, metal, and leather. They previously got no
  palette at all, on the grounds that a five-swatch ramp cannot hold skin, cloth, and metal at once
  (the locked ramp read as "too green, no character", 5/10, against 7/10 for dropping it — a tie).
  A tie is thin ground for giving up consistency, and the alternative assumed to cover it does not
  exist: `style_image` carries the reference's subject, not its look, so it cannot make two
  different subjects share a game's colours. Widening the palette answers the range objection
  without giving up the lock. `color_image` is a PNG with one pixel per colour, so the swatch count
  is a design decision, not a provider limit.
- Measured (2026-08-22, `var/assets/experiments/round-3-palette/`, one game, three subjects —
  knight, mage, slime — same seed per subject in both arms). The question was not whether any one
  sprite is good but whether the three read as one game:

  | | unlocked | locked |
  | --- | --- | --- |
  | knight | dark blue-grey steel | dark purple-navy with warm tan accents |
  | mage | near-black robe | the same purple-navy, warm tan staff |
  | slime | bright saturated green — visibly from another game | green pulled toward the game's value range |

  Locked won on that criterion. A second effect was not expected: the locked knight and mage have
  **visible faces**, where unlocked gave a faceless helmet and a black void under the hood. The skin
  swatches are what the earlier "not enough colour range" objection was asking for.
- Caveats on that measurement: three subjects, one game, one `art_style`. The locked sprites are
  also darker and lower-contrast overall, which is worth watching at small on-screen sizes.
- **`create-image-bitforge` needs a square canvas, and the server grows the request to one.**
  Measured 2026-08-22 (`var/assets/experiments/round-5-canvas/`, same prompt and seed): a slim
  character asked for at 32x64 came back as a detached hat floating above a body, while the same
  prompt at 64x64 came back as a complete figure. The provider only documents this as a keypoint
  caveat — "Warning! Sizes that are not 16x16, 32x32 and 64x64 can cause the generations to be
  lower quality" — but the control had no keypoints in it at all, so the canvas is the problem on
  its own.
- The rule is stated about canvases, not about characters, so it holds for any kind:
  `_posable_canvas` grows a bitforge request to the **smallest square that still contains it**
  (`SKELETON_FRIENDLY_SIZES`: 16, 32, 64). Growing rather than shrinking is what keeps the original
  measurement intact — a character has 64 rows because 48 cropped the figure below the thigh, and
  64x64 keeps every one of them; only the width changes. It is a no-op for `character`, `monster`, `prop`,
  `icon`, and `tile`, which are already square — **any** square canvas is returned unchanged,
  including one larger than the friendly list, because there is nothing there for the growth to
  fix. Without that a 128x128 request fell past the list and was reported as unreliable "on a
  non-square canvas", about a canvas that is square. The friendly list is about keypoints, and a
  posed request on a square canvas outside it is reported by `skeleton_size_warning` instead. A
  genuinely non-square canvas with no square to grow to still comes back with a warning.
- **`character` is a square kind, and its rows come from `_KIND_MIN_GRID`.** `_KIND_SIZE_RATIO` is
  `(1.0, 1.0)` and `_KIND_MIN_GRID["character"]` is 64, so the locked 32 grid a game picks for its
  tiles still generates a character at 64x64. The ratio used to be 1:2, and the growth rule above
  was the only way a character reached a square canvas — which capped out at 64, so `gridSize: 64`
  generated an unusable 64x128 and an explicit non-square `canvas` was never grown at all (measured
  2026-08-22, `daeume`: three character prototypes carrying a `concept:` reference, all rejected —
  two clipped busts and one field of dithering noise). The original 1:2 measurement said 48 rows
  crop a humanoid below the thigh, not that the canvas has to be tall; 64x64 keeps all 64 rows and
  only widens. `gridSize` is not floored — that is the caller sizing one asset deliberately — and
  `canvas` still leaves the ratio entirely.
- The cost is that the ordinary pixflux path, which produced clean 32x64 characters and never had
  bitforge's problem, now draws them at 64x64. That canvas measured clean through bitforge
  (`alphaCoverage` 0.335, no background intrusion, no clipping warning); the same has not been
  measured on pixflux, where the "larger canvas invites a scene" note below applies. Inspect
  opacity on the first characters generated after this change.
- `quality.inspect` needed no new thresholds: `subject_too_small` (3%) and `subject_may_be_clipped`
  are ratios of the canvas, not pixel counts. Clipping warnings should get *rarer* — a 1:2 humanoid
  nearly always touched the top and bottom edges.
- A grown request reports `canvas` and `requestedCanvas` in its result, plus a `warnings` entry
  saying what changed, so the size difference from the game's other characters is stated rather
  than discovered.
- `generate_2d_variations` cannot grow its canvas — its output has to stay the size of the asset it
  varies — so a non-square batch takes `generate-with-style-v2` instead, and the bitforge-only
  arguments are refused with that reason. That fallback now applies to `ui_button` and
  `ui_panel` rather than to characters.
- `generate_2d_sprite` accepts `canvas` as `[width, height]`, used exactly as given. It skips the
  kind's `_KIND_SIZE_RATIO` entirely, which `gridSize` cannot do — `gridSize` scales that ratio, so
  a canvas off that ratio is unaskable without it. The per-side range check still applies, and the
  200px bitforge ceiling still applies to a request carrying a reference. `canvas` and `gridSize`
  set the same thing, so passing both is refused rather than ranked, and `canvas` joins the asset-id
  digest so the same prompt at another size is another asset rather than a blocked duplicate.
- `paletteLock` joins that digest for the same reason. The same prompt with the game ramp forced on
  is a different image from the same prompt without it, so asking for both has to produce two
  assets to compare; before this the second call came back `duplicate_blocked` and the comparison
  could only be made by rewording the prompt, which changes the seed. Only the non-default
  (`false`) value is appended, so digests written before this still resolve to the same asset id.
- A posed or init-image request with an explicit `canvas` is **not** grown to a square. The growth
  exists because bitforge is unreliable on a non-square canvas; naming the canvas is the caller
  weighing that themselves, so the request reports a `warnings` entry and generates as asked.
- `generate_2d_sprite` accepts `poseFromAssetId` and `initAssetId`, both naming an approved PixelLab
  asset of the same game. The first reads that sprite's joints with `/estimate-skeleton` and sends
  them as `skeleton_keypoints`; the second sends it as `init_image`. Both need
  `create-image-bitforge`, so a request larger than 200px per side is refused rather than generated
  without the pose it asked for. `skeletonGuidance` is 0-5 (provider default 1) and
  `initImageStrength` is 1-999.
- Either field also accepts `concept:<filename>`, resolved against `var/concept-art/<gameId>/`.
  That directory holds human-supplied reference drawings, not generated assets, so the approval and
  PixelLab-provenance gates do not apply to it — those gates exist to stop an unreviewed
  *generation* from being laundered into approved work, and a concept drawing a human committed is
  the design the asset is meant to match. The directory is the whole gate: a filename that resolves
  outside `var/concept-art/<gameId>/` is refused, as is one that names no file, and neither spends a
  generation. An asset id in the same field is gated exactly as before.
- **Keypoints are normalised to 0-1, not pixels.** The schema types `x`/`y` as bare numbers and
  says nothing about their range, so this had to be measured: a full-body 128x256 sprite came back
  with every joint between 0.4 and 0.9. They therefore transfer to any canvas unchanged. Scaling
  them by a size ratio — the obvious-looking thing to do with a 4x-upscaled stored sprite — puts
  every joint in the top-left corner and the generation comes back as noise (measured 2026-08-22).
- **A pose reference carries the pose and nothing else.** This is the opposite of `style_image`,
  which drags the reference's subject along with it. Measured 2026-08-22
  (`var/assets/experiments/round-4b-skeleton/`, `round-4c-skeleton-square/`): a walking blue-cloaked
  girl was used to pose a prompt asking for an armoured knight, and the result was a knight in the
  reference's stride — never the girl. Keypoints are coordinates, so there is no subject in them to
  leak.
- **The canvas decides whether any of it works, and the provider's warning understates it.** Same
  reference, same seed, same prompt:

  | canvas | no keypoints | `skeletonGuidance` 4.0 |
  | --- | --- | --- |
  | 32x64 (the character ratio at the time) | messy, smeared figure | **noise, no figure at all** |
  | 64x64 (keypoint-friendly) | clean knight | clean knight in the reference's stride |

  Posing therefore requires a square canvas — and the server now grows the request to one rather
  than refusing or warning. `character` is square in its own right now, so it is posable directly
  and the growth covers the kinds that are not (`ui_button`, `ui_panel`). See the canvas rule
  above.
- High guidance costs quality even on a good canvas: the 64x64 posed knight is muddier and darker
  than the unposed one. Turn `skeletonGuidance` down before turning it up.
- `/estimate-skeleton` is billed separately from the generation — measured at 0.1 generations
  against the quota — so a posed request reports both in `imagesGenerated`.
- `paletteLock: false` turns the lock off for one asset — a boss with its own scheme, a colour-coded
  pickup. It is on by default. The palette biases rather than forces: the slime stayed green in both
  arms, because `color_image` is a sampling reference, not a clamp.
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
- **The provider description carries the subject; it is not the asset spec.** `prepare_asset_prompt`
  used to serialise its answers as labelled sentences, so `Composition:`, `Required visual
  structure:`, and `Readability target:` reached PixelLab verbatim along with `purpose` — none of
  which name anything that can be drawn. The composed description is now ordered clauses with no
  labels: subject, then the structures that must be unmistakable, then feedback, then the
  arrangement. `purpose` is returned as a brief field instead. The same brief that composed to 283
  characters composes to 114.
- **This is the provider's own design, not a house style.** PixelLab's getting-started tutorial
  prompt is two words (`Human mage`); the shape, colours, and composition come from a sketch passed
  as an init image, and the documentation calls that "one of the best ways to improve the results
  you're getting from PixelLab". The description's job is a short, non-contradictory subject
  statement. This repo already has that path as `initAssetId: concept:<filename>`.
- **The description is still the strong axis.** `text_guidance_scale` (1-20, default 8) exists to
  tune how literally the description is followed; no such control exists for the `(weakly guiding)`
  structured fields. That asymmetry is why duplication between the two is kept and only
  *contradiction* is acted on: a clause naming a different value of a field than the one being sent
  is reported in `promptMetrics.controlConflicts`, and both signals are still sent. Deleting either
  would make the server pick a winner the caller never asked for.
- **Clause separation includes the sentence period.** It did not, while `prepare` joined its
  sections with `". "` — so every clause it wrote straddled a sentence boundary and duplicate
  removal never saw a whole clause. Following the intake procedure was the one way to defeat the
  check. A period only separates when followed by whitespace or end of string, so `1.5` stays one
  number.
- **Kind framing is per clause and only added where the brief did not already ask for it.** A brief
  answering `centered` used to get `full body centered` appended anyway. `readable` was dropped from
  the framing entirely: legibility is what a reviewer judges, not a shape a model can put on a
  canvas. Stripping the intake's labels and then appending the server's own production vocabulary
  would have been the same mistake in a different place. Which kinds carry framing at all is a
  measured question — see the table below.
- **Framing is left off when a starting image leads.** PixelLab documents `init_image_strength` by
  purpose — 0-300 extremely rough colour guidance, 300-400 rough shapes and colours, 400-600
  "variations on an existing image", 600-900 detail on a nearly finished piece. From
  `REFERENCE_LEAD_STRENGTH` (400) up the reference states the composition in pixels, so framing
  prose would argue with an image that already won. A *pose* reference is not the same thing —
  keypoints are coordinates and carry no composition — and an unanswered strength keeps the framing,
  because the provider's own default lands in an unknown band. Reported as
  `promptMetrics.framingSuppressed`. Which kinds carry framing worth suppressing is the measured
  question answered in the next entry.
- **Kind framing is applied per kind because it was measured per kind, not because it reads
  consistent.** Measured 2026-08-23 across three rounds, 44 paid generations, in
  `var/assets/experiments/round-{7,8}-framing/`; the method and the decision rule are in
  `Src/McpServers/experiments/framing_ab.py`. Same seed per pair, one arm with the framing and one
  without, scored with this server's own `quality.inspect` rather than a metric invented for the
  experiment: `centered` against the subject box's offset from the canvas centre, `fully visible`
  against `subject_may_be_clipped`, `edge-to-edge tile` against `tile_has_transparent_gaps`.

  | kind | pairs | flags with / without | centring better / worse | outcome |
  | --- | --- | --- | --- | --- |
  | `character` | 6 | 0 / 0 | 3 / 1 | framing removed |
  | `prop` | 6 | 0 / 0 | 0 / 1 | framing removed |
  | `icon` | 6 | 0 / 0 | 1 / 0 | framing removed |
  | `tile` | 2 | 4 / 4 | 0 / 0 | framing kept |

  For the three isolated-subject kinds the generator already centred the subject, kept it whole, and
  kept the silhouette connected without being told, so the clauses were spending 22-70 characters on
  the one axis that has a strength control (`text_guidance_scale`) to restate what was already
  happening. `tile` went the other way and decisively: at both seeds the framing roughly doubled
  edge coverage (0.498 vs 0.435, 0.309 vs 0.150) and the arm without it returned scattered debris on
  a transparent canvas rather than a tile. That was measured through pixflux, not `/tilesets`, so it
  governs the pixflux tile path.

  `monster` and the UI kinds keep their framing because they were **not** measured — not because
  they were measured and passed. `monster` is the obvious candidate to extrapolate `character` onto,
  and extrapolation is the failure this experiment exists to avoid: the round-6 pilot read a
  fidelity loss (a lost face, lost glass panels) out of a single pair per kind, attached a mechanism
  to it, and it did not replicate at the next seed. That reading is withdrawn and its output was
  deleted with it — a retracted finding's images are not evidence of anything. One sample cannot
  separate an effect from a draw, and neither can two.
- **PixelLab prompt fields are English only, and Korean is refused rather than passed through.**
  `pixellab_client` used to state that the Korean `assetsNeeded` strings were "passed through
  unchanged" and that a translation layer was undecided work — but passing them through is a
  decision too, and it is the one that bills a generation for a description the model cannot read.
  Hangul is detected by `contains_hangul` — syllables, conjoining jamo, compatibility jamo, and
  both extended blocks — and the two layers that see it want opposite things from the answer.

  **The intake asks.** `prepare_asset_prompt` turns a Korean answer into a required question
  carrying `koreanText`, exactly as it does for an answer that is missing. A Korean answer means
  the host has not written the English yet, which is a question, and asking questions is that
  tool's whole job. The question names both ways through, because the server can take neither
  itself: translate it when the meaning is unambiguous, or settle the wording with the user when a
  choice of words would change the picture. The brief stays unready meanwhile, so nothing
  generates. A refusal was the first shape of this and it was the wrong one — it ended the
  conversation at the step whose purpose is to continue it.

  **The generation gates refuse.** `_generate_prototype` refuses before `_claim_paid_prototype`
  reserves the prompt, so a request that was never going to be sent leaves no claim behind; and
  every text-taking client function refuses last, because tilesets, map objects, animations, and
  inpaint never pass through `prepare` or `compose` at all. Refusal, not stripping: dropping the
  words would spend a generation on a description missing whatever they said.

  The server never calls a model, so it cannot translate and does not pretend to — the host has
  one and does the work. Only Hangul is matched, not every non-ASCII character, because
  `_SIZE_CLAUSE` itself matches the `×` in `64×64`.
- **A deterministic budget caps the description at `PROMPT_BUDGET` (400 characters)**, dropping
  lowest-priority clauses from the tail and reporting them in `promptMetrics.droppedClauses`. It is
  a guard rail against a call site pasting paragraphs, not a tuned value: a well-formed brief lands
  near 120.
- **`generate_2d_sprite` and `generate_ui_asset` both refuse a prompt that is not the brief's.** The
  `briefId` gate pinned every generation *parameter* to a user's answer and left free the one field
  the picture is actually made of, so the preflight proved nothing about the prompt it was gating.
  `generate_ui_asset` took no brief at all and shares `_generate_prototype`, which made it the way
  around the check rather than a second path to it — a gate on one of two tools is a gate on
  neither.
- **`seed`, `prompt_digest`, and `material_for` read the caller's raw prompt, not the composed one.**
  Composition is a presentation step; feeding its output to the digest would rename every asset ever
  generated, release every duplicate claim guarding an already-billed prompt, and change which
  material ramp a tile or prop picks.
- **`avoid` answers reach the provider on the path whose field is live.** They were collected by the
  intake and then read by nothing: `negative_description` was wired only into
  `generate_2d_variations`. A prototype request carrying a reference now sends them through
  `create-image-bitforge`; a plain pixflux request cannot, because the field is `(Deprecated)`
  there, and says so in `warnings` rather than leaving the caller to infer it.
- **`generate_2d_rotations` derives eight facings from one approved sprite** through
  `/generate-8-rotations-v2`. Asking for the same character eight times returns eight characters —
  each generation is an independent sample — so consistency has to come from the endpoint rather
  than from the prompt. Images arrive in a fixed order (`pixellab_client.ROTATION_ORDER`: south,
  south-west, west, north-west, north, north-east, east, south-east) and each is registered as its
  own `pending` asset named for its facing. The canvas must be square and one of 16, 32, 64, or 128;
  stored sprites are `_PIXELLAB_UPSCALE` times their generated size, so the request is made at the
  native canvas and the results are upscaled back. This is a second, independent reason `character`
  is a square kind: a 32x64 sprite cannot be turned by this endpoint at all.
- **`inpaint_asset` repairs one region instead of re-rolling the sprite** through `/inpaint-v3`
  (32-512 per side). Regenerating to fix one wrong detail discards every detail that was right, and
  the re-roll is a fresh sample, so it rarely returns them. `maskAssetId` takes a
  `concept:<filename>` mask a human drew — white where the endpoint should generate, black where it
  must preserve. The result is always a new `pending` asset: a repair is a proposal, and overwriting
  the approved original would let an unreviewed image inherit its approval.
- **API schema limits and subscription tier limits are different axes.** `BITFORGE_SIDE_RANGE`
  (16-200) matches `CreateImageBitforgeRequest`'s declared `200x200`; the bitforge documentation
  page's Tier 1 `80x80` and Tier 2+ `140x140` are UI tier caps. Likewise pixflux's schema is
  `32x32`-`400x400` while the page lists free 200 / Tier 1 320 / Tier 2+ 400. Check a failure
  against both before changing a constant.
- **`generate_with_style`'s 1-4 `style_images` cap is the schema's**, not a conservative guess:
  `GenerateWithStyleV2Request` declares a maximum of 4. The "Create images from style references
  (Pro)" documentation page's larger allowance (64 at 32x32, 16 at 64x64) belongs to a *different*
  endpoint and does not apply here — which also means the single-reference subject-leak measurement
  above remains valid for this path.
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
