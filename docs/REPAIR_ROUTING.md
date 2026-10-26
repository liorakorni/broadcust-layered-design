# Design state, repair routing and interactive edits

This describes how the pipeline separates **image generation** from **editable text
and layout**, so that fixing a word, an outline or a badge position never pays for
another image. Source of truth for the code in `lib/design_state.py`,
`lib/repair_routing.py`, `lib/stages/repair.py`, `lib/design_edit.py` and
`lib/stages/plan_layout.py`.

## 1. Persistent design state

Every assembled candidate gets an immutable snapshot
(`DESIGN_STATE/<version>/snapshot.json`) with four independently versioned parts:

| part         | holds                                                                  |
|--------------|------------------------------------------------------------------------|
| `content`    | the editable words (business name, headline, CTA, contacts)            |
| `layout`     | the scene without copy: geometry, typography, colours, shapes, stacking |
| `assets`     | photograph / artwork as artifact key + sha256                           |
| `validation` | each check, bound to the exact hashes (and policy version) it examined |

A part's version moves **only when its hash moves**. A text edit therefore produces a
snapshot with the same `assets.hash` and `assets.version` as its parent; this is
verified (`require_assets_preserved`), not assumed. Snapshots are append-only: a repair
writes next to the previous candidate and never rewrites it. `job.designVersion` is the
optimistic-concurrency counter; `job.designState` points at the current snapshot.

## 2. Typography and code-owned elements

`TextLayer` carries `letterSpacing`, `fill` (`solid`/`transparent`), `strokeColor`,
`strokeWidth`, `wrap`, `overflow` in addition to family/weight/size/line-height/colour/
align/direction. Outlined text is real text: `-webkit-text-stroke` + `paint-order`,
never a raster. A stroke needs both colour and width; a transparent fill needs an
outline. New CSS is emitted only for non-default values, so every older scene assembles
to byte-identical HTML. `text-align` is physical (`left/right/center`), so Hebrew (RTL)
changes how text runs, never where its column sits.

Simple elements (dark panels, gradients, CTA backgrounds, circles, dots, rings,
borders, chevrons) are `ShapeLayer`s drawn by code. Each implements one `effect`, and
every effect has exactly one declared owner (`layout`, `photo` or `overlay`):
no duplicate drawing, no layout-owned effect that nothing draws, one readability
gradient per design. A repair can add a missing gradient/panel only from a preset
(`gradient-bottom`, `gradient-top`, `panel-bottom`, `panel-top`) and only when no
effect of that kind exists, whoever owns it. Existing jobs are not migrated: a gradient
baked into a photograph is never painted a second time.

> **Contract amendment.** Shapes are emitted in the editor's own layer structure:
> `<div class="words dNNNN" id="<stable-id>" data-role="decoration" data-shape
> data-effect="…"><div class="text"></div></div>`, a direct child of `.box`, between
> images by `z-index` (an image is drawn before a shape at equal z-index). Classes
> run from `d3001`; the id comes from the scene layer id. `data-role="decoration"`
> is the only exemption from the empty-text check, the text count and text fitting.
> This extends CONTRACTS §5. The editor/exporter are not in this repository, so
> compatibility is verified here by an EXPORT_DESIGN round trip and **pending** in
> the editor itself; `CODE_SHAPES_ENABLED=false` disables every shape (nothing is
> added or planned).

## 3. Repair routing

REVIEW returns, per blocking issue: `code`, `severity`, `affectedElementIds`,
`repairTarget` (`content | typography | layout | asset | concept`),
`requiresImageGeneration`, and per-element `constraints` `{elementId, property, value}`.
They are **validated before routing** (`lib/repair_routing.py`):

* `requiresImageGeneration` must equal `target in (asset, concept)`;
* a content-category finding can only target `content` (wording comes from the
  approved copy, never from the model);
* element ids must exist; each constraint property must be allowed for that target and
  layer kind and must parse and be in range;
* `concept` only when the review reports `conceptDefect` **and** states the impact.

| finding                                   | target      | route |
|-------------------------------------------|-------------|-------|
| missing business name; wrong wording/phone| content     | light: restore the approved text |
| missing headline outline                  | typography  | light |
| text overflow                             | typography / layout (computed from the renderer's measurement) | light |
| shifted CTA / badge                       | layout      | light |
| missing code-owned gradient               | layout (`addShape`) | light |
| incorrect photograph                      | asset       | regenerate that photo only |
| fundamental composition failure           | concept     | only when justified, within budget |

Unknown, missing or inconsistent classifications are **not** routed to regeneration:
the issue is `unresolved`, the job stops with `REPAIR_ROUTE_UNRESOLVED` and names the
preserved candidate. Routes recorded per review: `approve`, `light`, `asset`,
`concept`, `unresolved`, `exhausted`, `noProgress`.

State machine: `ReviewPassed` → `RepairLight` | `RepairAsset` | `CorrectConcept` |
`Finalize` | `Correct` (terminal diagnostic; it no longer loops back to `ExtractScene`).

### Lightweight path (`REPAIR_LIGHT`)

load selected candidate → patch only the affected content/layout fields → assemble →
render → relevant checks and final composition review. It never runs CONCEPT,
EXTRACT_SCENE, CLEAN_PHOTO or DECORATION_OVERLAY, makes **zero** image calls, and
re-verifies after every correction that all approved text is still present. Asset
validation is retained when asset hashes and policy version are unchanged; a layout
change to image-layer geometry reruns only the compositing checks.

### Asset repair (`REPAIR_ASSET`)

Only the photograph (`REPAIR_PHOTO`) or the decoration overlay (`REPAIR_OVERLAY` then
`REBUILD_PHOTO`) can be repaired in place, each with the review's finding as the
instruction. An independent asset stops with a specific diagnostic.

### Bounds

Per-category budgets (`conf.MAX_REPAIRS`: content 2, typography 2, layout 2, asset 1,
concept = `MAX_CONCEPT_ROUNDS`), counted atomically on the job. The same patch applied
twice, the same fingerprint of findings after a repair, or a repair that *adds* blocking
findings all end the loop (`REPAIR_NO_PROGRESS`). FINALIZE delivers the best **eligible**
candidate (approved, all mandatory text present), never merely the most recent;
failed candidates stay for diagnosis. Minor visual deviations are warnings under
`balanced`; missing required text, unreadable text and broken assets stay blocking.

## 4. Dependency-based invalidation and caches

`lib/dependencies.py` maps a change kind to the checks it invalidates (wording → text
fit and final composition; font → typography, fit, render; photo → photo and affected
composition checks; …). Validation results are cached per check under a key containing
every input: asset hashes, concept hash, model, prompt digest and
`VALIDATION_POLICY_VERSION`. Only passes are cached. Paid stages (EXTRACT_SCENE,
DECORATION_OVERLAY, CLEAN_PHOTO, GENERATE_PHOTO) are answered from their stored output
when every input is identical; a repair is never answered from cache. Published asset
URLs are reused when the bytes' hash is unchanged.

## 5. Interactive editing (backend contract)

The editor is not in this repository. The backend provides two direct operations
(IAM-authenticated like the others):

**`updateLayeredDesign`**
```json
{ "operation": "updateLayeredDesign",
  "request": { "jobId": "…", "requestId": "edit-42", "expectedVersion": 3,
    "changes": { "content":    { "title.primary": "New headline" },
                 "typography": { "t_title": { "strokeColor": "#FFFFFF", "strokeWidth": "3" } },
                 "layout":     { "t_phone": { "y": "900" } } } },
  "trustedCallerContext": { … } }
```
* Applies validated patches (same code as repairs) and writes a new immutable snapshot.
  **No image call, no render**; assets and hashes are carried over unchanged. The
  response says so (`imageGenerationCalls: 0`, `previewRendered: false`) and lists
  `parts`, `assets`, `changedParts`, `changeKinds`, `invalidatedChecks`.
* `expectedVersion` is the `designVersion` the edit was made against. If the design has
  moved on, the call fails with `STALE_DESIGN_VERSION` and `details.currentVersion`;
  the final write is itself conditional on the version, so concurrent edits cannot both
  win. Reload and re-apply.
* `requestId`: an identical retry returns the original result without writing; the same
  `requestId` with a different payload is `IDEMPOTENCY_CONFLICT`; **every changed
  payload needs a new `requestId`**. A retry is recognised until the next edit lands.
* The user's wording is authoritative for their own copy (it may differ from the
  approved copy); empty text, unknown keys/layers/properties and out-of-range values
  are `INVALID_EDIT`.

**`exportLayeredDesign`** `{ jobId, expectedVersion? }` — the only call that assembles
and renders (on the stage Lambda): returns `html`, `htmlHash`, `previewUrl`, renderer
`problems`. It reuses the published asset URLs, matched by hash, and calls no image model.

### Client integration
1. Edit locally; **debounce** (e.g. 800 ms of inactivity) and send one
   `updateLayeredDesign` per burst with a fresh `requestId` and the last known
   `designVersion`. On success store the returned `designVersion`.
2. On `STALE_DESIGN_VERSION`, refetch status/design, re-apply local edits on top,
   resend with a new `requestId`.
3. Do not request a server preview while typing. Call `exportLayeredDesign` on an
   explicit Save/Export only.
4. On a network error resend the **same** payload and `requestId`.

## 6. Phase B: layout-first (`designFlow: "layout-first"`, feature-flagged)

Off unless `LAYOUT_FIRST_ENABLED=true` (deploy param `layoutFirst`). reference analysis +
approved copy → `PLAN_LAYOUT` (editable layout: typography, code-owned shapes, effect
owners, and a **text-free** photo brief) → `GENERATE_PHOTO` (the only image) →
`VALIDATE_ASSETS` (local photo checks) → ASSEMBLE → RENDER → REVIEW → targeted repair.
No flattened concept, no scene extraction, no overlay separation, and nothing routes
back to the legacy decomposition (`concept` is not an available route). Every stage
records a hash of *reference + photo brief* in place of a concept hash, and FINALIZE
refuses unless they agree. `suppliedAssets` are not supported in this flow yet.

## 7. Observability

`result.metrics` (and `job.metrics`): `totalDurationMs`, per-stage `durationMs` and
`attempts` (repairs are timed as `REPAIR_LIGHT` / `REPAIR_ASSET` / `REPAIR_PHOTO`),
`imageGenerationCalls` and `…ByStage` (counted at every call site), `cache`
(validation hits/misses, stage-output hits, asset URLs reused), `lightRepairs`,
`repairCounts`, every review's `routes` (route, reason, categories, blocking issues,
whether image generation was required) and the `selectedCandidate`. `qa.designSelection`
says which candidate was delivered, why, and what the others looked like.

## 8. Limitations

* Editor/exporter compatibility of `<div class="shape">` is unconfirmed (switchable).
* No editor exists here; the contract above is what it must follow.
* Only the photograph and the overlay have an in-place asset repair; independent assets
  do not. `suppliedAssets` are unsupported in layout-first.
* Existing jobs are not migrated; a gradient baked into a photo has no ownership record
  (`effects: []`), so the repair adds a code gradient only when the review reports one
  missing - a design with a faint baked gradient could get a second.
* The edit retry window ends when the next edit lands.
* No runtime target is promised: see the measured comparison in the delivery notes.

## Amendments from the live dev runs

- **Gradient ownership at asset validation.** The fidelity check now reports
  `missingShading` (`top|bottom|left|right|none`). When only a soft readability
  gradient is missing, the layout draws it (`scene_patch.add_shape`, presets
  `gradient-left|right|top|bottom`) instead of regenerating the overlay: no image
  call, one owner, and the final review confirms it on the rendered page. If the
  scene cannot take the shape (shapes disabled, an owner already exists with no slot),
  the old overlay repair is the fallback. `validation.json` records `codeOwned`.
- **`gradientStrength`.** A layout constraint on a gradient shape (0-1, opacity of the
  darkest stop). Reviews cannot supply CSS strings; colours are `#RRGGBB` only.
- **A repair result always answers the choice that routes it.** `RepairRoute`
  branches on `$.repair.next`; a Step Functions choice on a path that is absent
  fails the execution outright, and that failure cannot be caught - the job is
  left `RUNNING` with no error recorded. `REPAIR_LIGHT` therefore returns
  `next: "assemble"` and `REPAIR_ASSET` the stage it needs.
- **Corrections aimed at an effect are translated into the shape that draws it.**
  An effect id is not a layer, so properties cannot be set on it. When a layout
  finding names `add:<effectId>`, names an effect no shape draws, or describes a
  missing readability shading without naming anything at all, routing turns it
  into the `addShape` it should have been: the preset's edge comes from the angle
  the correction asked for, else the finding's region, else the side the text sits
  on. The translation is recorded in `routing.issues[].translated` (what was
  asked, what is being done, why). An effect a shape already draws is never taken
  over, and more than one candidate leaves the finding unresolved.
- **An added shape is in scope by consequence.** A repair that adds a shape names its
  elements by what the preset creates (`scene_patch.shape_id_for(effect)`) and the
  `effects` list that records the new owner, not by an id the review could have
  known. A round that restyles text and adds a shape is therefore accepted; any
  other element that changes is still refused as out of scope. The shape is also
  entered in the decomposition plan, or the patched scene is rejected as
  incomplete.
- **The reviewer is told what each property accepts.** `scene_patch.property_help`
  renders the installed font families and every numeric range and enum from the
  table the executor validates against. A value outside them is still refused -
  live, a single out-of-range `letterSpacing` and a single uninstalled font each
  failed an otherwise finished design.
- **Planner tolerance (layout-first).** `PLAN_LAYOUT` thins more than four gradient
  stops, snaps rotation to 45 degrees and clamps shape numbers, withdraws a
  layout-owned effect nothing draws, and allows one corrected retry after a schema
  rejection (a vision call, never an image call; counted as `planRetries`).
- **DynamoDB.** `metrics` is a reserved word and is always aliased; numbers read back
  as `Decimal` are serialised and hashed as plain numbers.
- **Deploying layout-first.** `scripts/deploy.sh` ignores extra arguments. Use
  `npx serverless deploy --stage dev --param="layoutFirst=true"`; a plain
  `./scripts/deploy.sh dev` turns the flag back off.
