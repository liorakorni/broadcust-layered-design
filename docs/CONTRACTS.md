# Frozen contracts

These interfaces are fixed. Later work imports them; nothing redefines them. Interface
drift between stages is the most likely way this build fails, so treat a change here as
a breaking change that requires updating every consumer in the same commit.

`docs/SPEC.md` remains the authority on *behaviour*. This file fixes the *shapes*.

## 1. Stage invocation

Step Functions invokes `handler_stage.py` with exactly this, and nothing larger:

```json
{ "jobId": "...", "stage": "DECORATION_OVERLAY", "attempt": 1, "assetId": null }
```

`assetId` is non-null only inside the `INDEPENDENT_ASSETS` Map. Image bytes, HTML and
scene documents never travel through workflow state — only pointers.

## 2. Stage return

```json
{
  "ok": true,
  "stage": "DECORATION_OVERLAY",
  "artifacts": {
    "overlay": "layered-design/<jobId>/DECORATION_OVERLAY/1/overlay.png"
  }
}
```

Keys in `artifacts` are stage-specific logical names. Values are always S3 keys under the
job's own prefix. A stage persists its output **before** returning.

On failure a stage raises a `LayeredDesignError` subclass; `handler_stage` converts it
with `lib.errors.as_error_dict` and lets the workflow record a terminal failure. A stage
never returns `{"ok": false}` — raising keeps the Step Functions retry/catch wiring in
one place.

## 3. Artifact keys

```
layered-design/<jobId>/<stage>/<attempt>/<name>
```

Immutable and append-only. A retry writes a **new attempt**; it never overwrites. Reads
go through `lib.artifacts.job_key`, which rejects any key outside the job's prefix.

Stable logical names used across stages:

| Stage | Name | Contents |
|---|---|---|
| `ACCEPT_REQUEST` | `request.json` | Canonical validated start request |
| `PREPARE_COPY` | `copy.json` | Authoritative copy manifest, keyed by content key |
| `CONCEPT` | `concept.png` | Exact concept bytes — the immutable separation input |
| `EXTRACT_SCENE` | `scene.json` | Validated scene document |
| `CLEAN_PHOTO` | `photo.png`, `photo-after-repair-N.png` | Background photograph, no text or graphics; rebuilt after each overlay repair |
| `DECORATION_OVERLAY` | `overlay.png`, `overlay-repair-N.png`, `overlay-cleared-N.png` | RGBA decoration layer; each repair is a fresh extraction from the concept, and `cleared` is one whose duplicate component boxes were erased in code |
| `INDEPENDENT_ASSETS` | `assets/<assetId>.png` | One independent asset |
| `VALIDATE_ASSETS` | `validation.json` | Per-asset measurements and verdicts |
| `ASSEMBLE` | `design.html` | Exact final HTML bytes |
| `RENDER` | `native.png`, `feed.png` | Native-size and 360px-wide renders |
| `REVIEW` | `review.json` | Strict-JSON review verdict |
| `FINALIZE` | `result.json` | Completed result per SPEC section 2.5 |

### Retention tags

Every artifact is written with the S3 tag `lifecycle=temporary`. The bucket's
lifecycle rule expires only objects carrying that tag, after 30 days.

When a job succeeds, FINALIZE retags the objects that make up the saved design
`lifecycle=retained` (`lib.artifacts.retain`), before the job is marked
`SUCCEEDED`:

- `result.json`, the delivered `design.html`, ASSEMBLE's `scene.json`
- the reviewed `native.png` and `review.json`
- the canonical `request.json`
- the source object behind every entry in the asset manifest, since the manifest
  hash covers those keys

`mark_succeeded` also removes the job record's `expiresAt`. A failed job retags
nothing and keeps its expiry, so its artifacts and record expire after 30 days.
The retained set is "excluded from automatic deletion"; it is not copied to
separate storage.

The tag is applied on write, so `s3:PutObjectTagging` is required by every role
that writes artifacts.

### Requester decisions on 7.5

SPEC 7.5's `final/` key paths are illustrative. Decisions:

1. **Keys.** Keep the `<stage>/<attempt>` structure and the combined `result.json`.
   Return `resultKey`; return `previewKey` and `qaKey` for objects that exist
   (`qaKey` is the review artifact; the 2026-10-19 spec names it). `htmlKey` stays
   in the full direct result but is not part of the compact Bedrock result. Do not
   return `layersKey`, and create no files merely to match the example paths. QA is
   embedded in `result.json` as `qa` and is also referenced through `qaKey`.
2. **Two response modes.** Bedrock actions get compact references and hashes. The
   authenticated direct call gets the complete result. The backend therefore needs
   no S3 access. Ownership checks stay mandatory in both.
3. **Preview.** `previewKey` is an S3 key (the reviewed native render);
   `previewUrl` is the Cloudinary URL. Unavailable fields are omitted.
4. **Retention.** 30 days for temporary artifacts; saved designs and their
   dependencies are excluded from automatic deletion (see Retention tags).
5. **QA.** `qa` carries `passed`, blocking issues, suggestions, render-check
   results, the reviewed render's identifier (`renderKey`, `renderSha256`),
   `htmlHash` and `assetManifestHash`. Approval needs passing checks and zero
   blocking issues; a check that did not run is reported `missing` and blocks.

### designFreedom, reviewStrictness and the concept loop (SPEC 2.7, ANALYZE_REFERENCE, REVIEW_CONCEPT)

Pipeline: `PREFLIGHT -> PREPARE_COPY -> ANALYZE_REFERENCE -> CONCEPT ->
REVIEW_CONCEPT -> [CorrectConcept -> CONCEPT]* | SelectConcept -> EXTRACT_SCENE ->
... -> REVIEW -> FINALIZE`.

- `designFreedom` is an integer 0..100, default 0. `reviewStrictness` is a string,
  default `"balanced"` (the only supported level). The job stores both and
  `policyVersion` (`"3.0"`). `designFreedom` is part of the canonical request hash;
  `reviewStrictness`, `layoutDirection`, `textDirection` and
  `referenceLayoutPolicy` are excluded from it while they equal their defaults, so
  a request accepted before those fields existed hashes as it did.
- **Direction is resolved before generation, never patched after.**
  ANALYZE_REFERENCE resolves `{layoutDirection, textDirection, source, reason}` from
  the request, then explicit placement in the brief or
  `referenceLayoutPolicy: "preserve"` (which resolves `layoutDirection` to
  `preserve-reference`), then the locale of the approved copy; it is carried in
  `policy.json` under `direction` and rendered into the ANALYZE_REFERENCE, CONCEPT,
  concept-correction and REVIEW_CONCEPT prompts. Only column positions mirror:
  photographs, logos and pictograms never do, and Latin runs inside a right-to-left
  line (business names, phone numbers, emails, URLs) keep their character order.
  REVIEW_CONCEPT answers `compositionDirectionOk`, which becomes the required
  `compositionDirection` check; a failure is a blocking
  `COMPOSITION_DIRECTION_WRONG` issue so the bounded concept correction fixes it.
  Decomposition never changes direction.
- **Independent assets are recorded one key at a time.** INDEPENDENT_ASSETS runs as
  a Map, so each branch writes its own entry under the job's `assets` map
  (`jobs.record_asset_item`, read back with `jobs.asset_items`) rather than relying
  on the Map's collected output: ASSEMBLE reads the job, and an asset that never
  reaches it is an asset with no delivered URL. Floats are converted to `Decimal`
  on the way in (`jobs.storable`); DynamoDB rejects a float and fails the whole
  write.
- **A component the artwork would not give up.** After the bounded overlay repair,
  a duplicate still in the overlay is erased arithmetically inside its own box
  (`overlay-cleared-N.png`, metric `overlayAreasClearedInCode`) - safe because
  CLEAN_PHOTO repaints under that box and the layout draws the shape over it.
  Gradients are too large for that, so instead the shape is given back to the
  asset that kept it (`scene_patch.rasterize_shapes`): its effect's owner becomes
  that asset, its component entry moves onto the asset's own entry, any overlay
  sample that claimed the photograph shows through there is dropped with it, and
  the job records a `COMPONENT_LEFT_RASTERIZED` warning naming the layers. The
  call-to-action background, the contact circles and the pictograms may never be
  surrendered this way (`validate_assets.mandatory_component_ids`): missing
  editability there is a failure, not a warning.
- **The overlay keeps a field whole up to a torn edge.** A flat colour field divided
  from the photograph by a ragged or hand-cut edge belongs entirely on its side of
  that edge; the overlay prompt says so, and the extractor must own the field the
  text sits on and list every instance of a repeated motif. Measured live: an
  overlay that kept only the ribbon left a hole nothing else filled
  (a failed region sample in VALIDATE_ASSETS).
- **A rejected scene is re-asked once.** EXTRACT_SCENE sends the validator's own
  complaint back to the model and takes the corrected document; if the second
  answer is also invalid the first complaint is the error. The retry is a vision
  call, never an image call.
- ANALYZE_REFERENCE writes `policy.json` (including `reviewStrictness` and
  `reviewPolicy`) and `reference-analysis.json` (content-neutral `anchors`,
  `copyPlacements`, `policyVersion`) once. Stages refuse any stored policy or
  analysis whose `policyVersion` is not current: `POLICY_VERSION_MISMATCH`.
- CONCEPT attempt 1 generates from the reference. Attempt N>1 reads
  `job.conceptCorrection {forAttempt, planKey}` and edits the planned base candidate
  (`REVIEW_CONCEPT/<n>/correction-plan.json`); without a plan it fails rather than
  regenerating. It writes `concept.png`, `concept-provider-output.png` (the provider's
  untouched output), `correction.json` and, when masked, `correction-mask.png`.
- REVIEW_CONCEPT writes `concept-review.json` per attempt (schema 2.0: `eligible`,
  `copyChecks`, `requiredChecks`, `issues`, `blockingIssues`, `warnings`,
  `structuralFailures`, `regressions`, the raw `verdict`) and a candidate record under
  `job.conceptCandidates[<attempt>]`. Its result carries `approved` (= eligible) and
  `correctionAvailable`. Conflict raises `REFERENCE_CONSTRAINT_CONFLICT`; a review that
  skipped a required check raises `INVALID_MODEL_JSON` (retried).
- Not eligible and `correctionAvailable`: `CORRECT_CONCEPT` consumes one of
  `MAX_CONCEPT_ROUNDS` (2) and plans the edit. Not eligible and no round left:
  `SELECT_CONCEPT` picks the best eligible stored candidate or fails with
  `REQUIRED_COPY_MISSING` (category `content`) / `REFERENCE_ADHERENCE_FAILED`
  (category `reference-adherence`). The budget (`conceptRounds`) is separate from
  `correctionRounds`.
- **Frozen concept.** The selected candidate is recorded on the job as
  `approvedConcept {key, sha256, attempt, reviewKey, designFreedom, policyVersion,
  reviewStrictness, selectedBy, selectionReason, selectionKey}`. EXTRACT_SCENE, both
  separation branches, VALIDATE_ASSETS and REVIEW read it, never the latest `CONCEPT`
  artifact, and record `conceptSha256`, which FINALIZE checks.
- REVIEW reports `conceptDefect` when its blockers come from the approved concept
  itself. It is honoured only while a concept round remains; the correction then edits
  the approved candidate and that candidate is never selected again.
- Errors carry an additive `category` (`content`, `reference-adherence`,
  `asset-validation`, `technical`) beside the unchanged `code`.
- `result.json` and `qa` carry `designFreedom`, `reviewStrictness`, `policyVersion`,
  `conceptRounds`, `conceptSelection {attempt, key, sha256, policyVersion, selectedBy,
  reason, selectionKey}`, the concept review (`key`, `passed`, `mode`,
  `documentedExceptions`, `requiredChecks`) and `warnings` (non-blocking findings
  from both reviews: `{stage, code, category, affected, impact, suggestion}`).

## 4. Error shape

Identical for API rejections, stage failures and failed-job status:

```json
{
  "stage": "DECORATION_OVERLAY",
  "code": "INVALID_TRANSPARENCY",
  "message": "The decoration image is fully opaque",
  "retryable": false,
  "category": "asset-validation"
}
```

Stage names come from `lib.errors.Stage`, codes from `lib.errors.Code`. `category` (`content`, `reference-adherence`, `asset-validation` or `technical`) is an additive field naming the kind of failure; it never changes what a `code` means. `message` is
caller-visible: no secrets, signed URLs or raw provider payloads.

## 5. HTML nesting

Reproduce this structure exactly, scaled to the requested canvas. Do not add wrapper
divs, rename classes, reorder the nesting, or "improve" it. A golden-file test enforces
it.

```html
<div class="image container" style="opacity:1;width:1080px;height:1080px;position:relative;overflow:hidden;">
  <div class="content" style="width:100%;height:100%;position:relative;">
    <div class="box" style="width:100%;height:100%;position:relative;">
      <img class="background" id="background-1" src="PHOTO_URL" alt="" style="position:absolute;left:0;top:0;width:1080px;height:1080px;object-fit:cover;z-index:0;">
      <div class="badges b1" id="decorations-1" data-asset-type="image" style="position:absolute;left:0;top:0;width:1080px;height:1080px;z-index:1;">
        <img src="DECORATIONS_URL" alt="" style="display:block;width:100%;height:100%;object-fit:contain;">
      </div>
      <div class="words d1001 fxs" id="text-1001" data-role="title" style="position:absolute;left:540px;top:80px;width:480px;height:180px;font-family:Heebo,Arial;font-size:72px;font-weight:800;color:#FFFFFF;text-align:right;direction:rtl;line-height:1.12;box-sizing:border-box;z-index:10;">
        <div class="text">Editable marketing text</div>
      </div>
    </div>
  </div>
</div>
```

Rules (full set in SPEC section 5):

- `.background`, `.badges` and `.words` are **direct children** of `.box`.
- All IDs and `dNNNN` / `bN` classes are unique. Image groups continue `b2`, `b3`.
- `.badges` contains images only. Text is plain copy inside `.words > .text`, with all
  styling on the outer `.words` element.
- Allowed roles: `businessName`, `businessDescriptor`, `title`, `subtitle`,
  `serviceItem`, `expertiseItem`, `benefitItem`, `informationItem`, `eventDetail`,
  `offerDetail`, `cta`, `userInformation`, `icon`.
- Hebrew defaults to `rtl` / `right`; separate Latin contacts use `ltr`.
- `object-fit: contain` for full-canvas overlays, logos and cutouts; `cover` only for
  intended photo crops.
- **Amendment (code-owned shapes):** a design may also contain editable shapes,
  emitted in the same structure as text so the editor treats them as ordinary
  layers:

  ```html
  <div class="words d3001" id="cta-background" data-role="decoration"
       data-shape="panel" data-effect="ctaBackground"
       style="position:absolute;left:80px;top:840px;width:360px;height:96px;
              box-sizing:border-box;background-color:#FFD43B;border-radius:48px;
              z-index:9;">
    <div class="text"></div>
  </div>
  ```

  - Direct children of `.box`, ordered with the images by `z-index` (an image
    precedes a shape at equal `z-index`). No wrapper elements.
  - `.text` is intentionally empty. `data-role="decoration"` is the only marker
    that exempts a `.words` element from the empty-text check, the text-layer
    count and text fitting; `decoration` is deliberately not an allowed text
    role, so nothing editable can hide behind the exemption. Out-of-bounds and
    duplicate-id checks still apply.
  - Classes are numbered from `d3001`, so they never collide with text at
    `d1001+`. The `id` is derived from the scene layer id (sanitised to
    `[a-z0-9-]`, suffixed on collision), so it is stable when other layers are
    added - unlike the positional text ids.
  - `data-shape` names the primitive and `data-effect` the effect it draws.
  - They draw panels, gradients, circles, dots, rings, borders and chevrons with
    plain CSS (`background`, `linear-gradient`, `border`, `border-radius`,
    `opacity`, `transform: rotate`). Set `CODE_SHAPES_ENABLED=false` to disable
    them. See docs/REPAIR_ROUTING.md.
  - Native editor save/reopen is verified in-repo by an EXPORT_DESIGN round trip;
    verification in the actual editor is pending.
- Text may also carry `letter-spacing`, `white-space:nowrap`, `-webkit-text-stroke`,
  `paint-order` and `-webkit-text-fill-color:transparent`, only when the scene asks for
  them; `text-align` is always physical (`left`/`right`/`center`).
- Forbidden: scripts, SVG, arbitrary CSS, CSS `background-image`, generated final text
  baked into images, and clipping used to hide text defects.

## 6. Geometry

- All coordinates are **pixels from the canvas top-left**.
- `fontSize` is pixels. `lineHeight` is a **dimensionless multiplier**.
- `zIndex` is an integer in `0..100`.
- Never mix normalized `0..1` coordinates with pixel values.
- Any vision call that returns coordinates must pass `detail="original"`. Other detail
  levels resize the image before the model sees it, so returned coordinates refer to the
  resized frame and every layer lands in the wrong place — with no error raised.

Default stacking: photograph `z=0`, overlay `z=1`, independent imagery above, text above
its intended backing.

## 7. Canvas and generation sizes

GPT Image renders only `1024x1024`, `1024x1536` and `1536x1024`. A requested canvas
(`256..2048` per side) is generated at the closest supported aspect and resampled to the
target in exactly one place, `lib/validate_assets.py`. No other module rescales geometry.

### Best usable result: `SUCCEEDED_WITH_WARNINGS` and the selected candidate

A design whose quality is unresolved is returned, not discarded. Statuses are now
`QUEUED`, `RUNNING`, `SUCCEEDED`, `SUCCEEDED_WITH_WARNINGS` and `FAILED`.

- `SUCCEEDED`: the design passed review. `qualityPassed: true`, `publishReady: true`.
- `SUCCEEDED_WITH_WARNINGS`: structurally valid, editable HTML exists, its assets are
  available and its snapshot is coherent, but review findings remain, the bounded
  repairs were used up, a repair made no progress, or a later stage failed after a
  usable candidate existed. `qualityPassed: false`, `needsReview: true`. The HTML is
  in `result.html`. `publishReady` is `false` whenever mandatory content is missing
  or a blocking finding remains; the client must not publish on its own then.
- `FAILED`: no usable candidate exists (no valid HTML, assets missing, or the job
  never got as far as assembling one). The error is the original technical error.

Status polling returns the same `status` and an `outcome` object
(`needsReview`, `publishReady`, `qualityPassed`, `selectedAttempt`, `stopReason`) once
the result exists. A client that polls until `SUCCEEDED` only must also stop on
`SUCCEEDED_WITH_WARNINGS`: the job is complete, and the result is available.

New `result` fields (the existing ones are unchanged):

| Field | Meaning |
|---|---|
| `selectedAttempt` | the candidate that was delivered |
| `selectedSnapshotKey` | the versioned design state of that candidate; the HTML, scene, assets, layer map and preview all come from it |
| `qualityPassed` | review passed with no blocking finding |
| `needsReview` | a person should look before it is used |
| `publishReady` | safe to publish without review |
| `stopReason` | why the pipeline stopped: `APPROVED`, `REPAIR_NO_PROGRESS`, `CORRECTION_EXHAUSTED`, `REPAIR_ROUTE_UNRESOLVED`, or the error code of a later stage that failed after a usable candidate existed |
| `warnings[]` | `{code, severity, affectedElementIds, message, suggestedAction}` for the selected candidate |
| `designSelection` | why this candidate won over the others |
| `diagnostics` | per-candidate findings and, for a failure after a usable candidate, the original technical error |

Candidates are ranked on usability, mandatory content, readability, editability,
measured fidelity to the approved concept, and remaining findings - not on the
latest attempt or the fewest blocking findings. A candidate whose stored document no
longer matches its recorded hash is never returned.

Repair progress is judged from evidence, not from counts. Findings carry a stable
identity (element or region, requirement, defect type); a repair is a verified
improvement, a partial improvement, no meaningful change, a verified regression or
inconclusive, from the concept, the previous and repaired renders and the changed
regions. A warning being promoted to blocking proves nothing by itself.

Scene fields added for reconstruction: `alignmentGroups` (a shared edge and the
measured ink edges of the layers on it) and `fillGroups` (shapes that share a fill,
each keeping its own colour). An editor that moves one layer of an alignment group
should release it from the group; `scene_patch` already does for x/align changes.

Job metrics now report `renderCalls` apart from `imageCalls`, and `fittedGradients`
and `codeOwnedEffects` where the layout drew shading in code.

Optional request field `reuseConceptFromJobId` (owner-scoped) rebuilds a design from
the approved concept of an earlier job of the same owner instead of generating one.
It is for re-running reconstruction after a pipeline fix; it is not exposed to the
Bedrock agent.
