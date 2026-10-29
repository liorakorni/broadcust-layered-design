# Using the LayeredDesign service

Turns a marketing brief and an inspiration image into a **layered, editable
design**: the photograph, the decorative artwork and every text block come back
as separate pieces, so the result can be edited rather than regenerated.

Four operations, all on the API Lambda. Designs take minutes to produce, so this
is an async job API — you get a `jobId` immediately and poll for the result.

- `runLayeredDesign` — accept a brief, return a `jobId`
- `getLayeredDesignStatus` — read progress (including a **draft** once the first
  HTML exists), then the finished design
- `updateLayeredDesign` — edit text, typography or layout of a finished design
  (no image call, no render) — see [REPAIR_ROUTING.md](REPAIR_ROUTING.md) section 5
- `exportLayeredDesign` — render the current edited design and return its HTML and preview

## Quick start

1. Call `runLayeredDesign` with a fresh `requestId`. Keep the `jobId` it returns.
2. Poll `getLayeredDesignStatus` every 3–5 seconds. Show `draft.html` (or
   `draft.previewUrl`) as soon as it appears, labelled as a draft.
3. Stop on `SUCCEEDED`, `SUCCEEDED_WITH_WARNINGS` or `FAILED`. All three are final.
4. On `SUCCEEDED` or `SUCCEEDED_WITH_WARNINGS`, read `result`. Publish **only** when
   `result.publishReady` is `true`; otherwise a person must review it first.
5. Edit with `updateLayeredDesign`, save with `exportLayeredDesign`.

```python
import json, time, boto3
lam = boto3.client("lambda")
fn = "broadcust-layered-design-dev-api"
ctx = {"ownerScope": "tenant-4812"}          # from YOUR authenticated session

def call(payload):
    out = lam.invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
    return json.loads(out["Payload"].read())

job = call({"operation": "runLayeredDesign", "trustedCallerContext": ctx, "request": {...}})
while True:
    r = call({"operation": "getLayeredDesignStatus", "trustedCallerContext": ctx,
              "jobId": job["jobId"]})
    if r["status"] in ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS", "FAILED"):
        break
    time.sleep(4)
```

> Terminology used throughout: an **asset** is one image file (the photo, the
> overlay, an inset). A **layer** is one positioned element in the design, which
> may reference an asset or hold text.

---

## 1. Start a job

The payload always names the operation explicitly — the service never infers it
from the shape of the request.

```json
{
  "operation": "runLayeredDesign",
  "trustedCallerContext": { "ownerScope": "tenant-4812" },
  "request": {
    "schemaVersion": "1.0",
    "requestId": "req-italy-001",
    "brief": {
      "post": "חופשת חורף באיטליה. טיסות, מלון ומדריך צמוד. יציאה בינואר.",
      "instructions": null,
      "requiredTexts": ["חופשת חורף באיטליה"],
      "business": {
        "name": "Dolce Travel",
        "phone": "03-1234567",
        "email": "info@dolce.example",
        "website": null
      }
    },
    "canvas": { "width": 1024, "height": 1024 },
    "referenceImageUrl": "https://res.cloudinary.com/broadcust/image/upload/inspiration.jpg",
    "brand": { "colors": ["#0B2545"], "fontFamilies": ["Heebo"] },
    "suppliedAssets": [],
    "designFreedom": 0,
    "templateFlexibility": 20
  }
}
```

### Fields

| Field | Required | Notes |
|---|---|---|
| `brief.post` | **yes** | The marketing brief, any language. Up to 12,000 characters. |
| `brief.requiredTexts` | no | Strings that must appear **verbatim** in the design. Enforced in code, not merely requested. |
| `brief.business` | no | `name`, `phone`, `email`, `website`. Phone and email stay separate text layers. |
| `brief.instructions` | no | Free-text guidance. |
| `canvas` | no | Defaults to 1080×1080. Each side 256–2048. |
| `referenceImageUrl` | **yes** | Inspiration image. **https on an authorized origin** (`res.cloudinary.com` by default). |
| `brand.colors` | no | `#RGB` or `#RRGGBB`. |
| `brand.fontFamilies` | no | Must be installed in the renderer — currently `Heebo` and `Arial`. An unlisted font is rejected at PREFLIGHT rather than silently substituted. |
| `suppliedAssets` | no | Up to 10 images you provide (`logo`, `photo`, `product`, `portrait`). These are used as-is and never regenerated. |
| `designFreedom` | no | Integer 0-100: how far the design may depart from the reference. Default `0` (strictest). See below. |
| `templateFlexibility` | no | Integer 0-100, default `20`: how much the generated design base (the structural artwork under the editable layers) may differ from the approved concept. Independent of `designFreedom`. See below. |
| `reviewStrictness` | no | How strictly differences are judged. Default and only supported value: `"balanced"`. Independent of `designFreedom`. See below. |
| `layoutDirection` | no | `"auto"` (default), `"rtl"` or `"ltr"`: where the columns sit. See below. |
| `textDirection` | no | `"auto"` (default), `"rtl"` or `"ltr"`: how the writing runs. |
| `referenceLayoutPolicy` | no | `"adapt-to-locale"` (default) or `"preserve"`: whether the reference's own composition direction is kept. |
| `designFlow` | no | `"legacy"` (default) or `"layout-first"` (where enabled). See below. |
| `reuseConceptFromJobId` | no | Rebuild from the approved concept of an earlier job of the **same owner** instead of generating one. For re-running reconstruction after a pipeline fix. Not available through the Bedrock agent. |
| `requestId` | **yes** | Your idempotency key. See below. |

**`trustedCallerContext.ownerScope`** establishes who owns the job. It must come
from your authenticated session — never from the end user's request body, or a
caller could claim another tenant's jobs. Jobs are readable only by the scope
that created them, and an unknown job and someone else's job are deliberately
indistinguishable.

**The inspiration image is guidance, and you control how binding it is.** Supply
real pixels on an authorized origin — a description of an image will not do. How
closely the design follows the reference is set by `designFreedom`.

### `designFreedom`

A creative-control setting from 0 to 100. It is **not** a measured pixel
difference or a similarity score, so a result is never "10% different". The
service turns the number into written constraints that drive reference analysis,
the concept prompt, the concept review and its repairs.

| Value | Policy mode | What it means |
|---|---|---|
| 0 | `strict-adherence` | Reproduce the reference composition as closely as practical; no discretionary redesign. |
| 1-25 | `close-adaptation` | Keep the recognizable layout and major anchors; adapt locally for copy, assets, brand and fit. |
| 26-50 | `guided-adaptation` | Keep selected qualities and hierarchy; reorganize regions when the brief justifies it. |
| 51-75 | `broad-inspiration` | Major structural change allowed; the reference is mostly an atmosphere reference. |
| 76-100 | `free-composition` | A new structure is allowed. At 100 no source layout is preserved. |

- **Omitted means 0.** An explicit `0` is the same request and hashes identically.
- **Validation is strict.** An integer from 0 to 100 only. `null`, `"10"`, `10.5`,
  `true`/`false`, `-1` and `101` are all `INVALID_REQUEST`.
- **It is part of the request.** Reusing a `requestId` with a different value is
  `IDEMPOTENCY_CONFLICT`. The service never changes the value during repairs, and
  never raises it to get past a conflict.
- **Facts always win.** At every setting the brief, required text, supplied
  identity assets and brand take precedence. Replacing the reference's business
  text and identities, applying your brand colours and substituting an available
  font are documented exceptions, not violations.
- **The HTML is faithful at every setting.** `designFreedom` governs reference to
  concept. The final HTML must still reconstruct the approved concept faithfully
  at 100.
- **Pixel-identical output is not promised at 0.** If mandatory content cannot fit
  a strict layout, the job fails with `REFERENCE_CONSTRAINT_CONFLICT` rather than
  silently redesigning.

The effective value and `policyVersion` come back in the result and in `qa`, with
the concept review's mode and documented exceptions.

### `templateFlexibility`

`designFreedom` governs the concept; `templateFlexibility` governs only how closely the
generated **design base** must match it. Profiles are versioned in one place
(`lib/template_policy.py`): `0` strict; `1-20` small differences tolerated; `21-40`
moderate; `41-100` broad. Whatever the value, artwork that is plainly *added* - for
example a vertical border down the middle-right that the concept does not have - is a
major finding. The resolved value is stored with the job when it is accepted and the same
tolerance is used when the design base is generated, reviewed and repaired. It is returned
as `result.templateFlexibility` / `result.templateTolerance`. Values outside 0-100 are
rejected; omitting it means 20. It passes through the Bedrock agent inside `requestJson`.

### Draft and final results

Poll `getLayeredDesignStatus` as before. Once the first HTML has been stored **and
rendered**, a running (or failed) job's response also carries `draft` and `milestones`:

- `draft`: `{state: "draft", candidateId, attempt, renderedAt, timeToFirstHtmlMs,
  reviewState, final: false, publishReady: false, needsReview: true, html, htmlUrl,
  previewUrl, linkExpiresInSeconds}`. It is **not** the result and must not be published;
  `reviewState` is `pending-review`, `under-review`, `reviewed-approved`,
  `reviewed-repair-pending` or `reviewed-findings-open`. `htmlUrl` and `previewUrl` are
  signed links made at poll time that expire in at most 15 minutes; ask again for a fresh
  one. They are never stored. The Bedrock compact status omits the HTML and links.
- `milestones`: `firstHtmlRenderedAt`, `timeToFirstHtmlMs`, `firstCandidateId`. Written once
  (a conditional write, so a later candidate never replaces it) and re-created from the
  stored artifacts, flagged `firstHtmlRecovered: true`, if the write was interrupted.

The final `result` adds, for jobs accepted by the current pipeline (`pipelineVersion:
"layered-v2"`): `timing` (`finalHtmlRenderedAt` - when the *delivered* candidate was
rendered, `timeToFinalHtmlMs`, `completedAt`, `totalDurationMs`; null when nothing was
rendered), `qualityScore`, `qualityBreakdown`, `rubricVersion`, `qualityReason`,
`criticalFindings`, `layerContract`, `fallbacksUsed`, `templateFlexibility`. Jobs from
before this version keep their old shape; nothing is migrated.

`qualityScore` is **provisional**: a weighted summary of the review's findings in five
dimensions (content 30, readability/typography 25, layout/composition 20, editability 15,
concept adherence 10), with provisional penalties and bands (`good` 85+, `acceptable`
70+). It has not been calibrated against human ratings. Use it to compare candidates of
one job, not as a publish threshold. It never chooses the delivered candidate and never
makes one publishable: a critical finding (missing mandatory text, a blocking readability
finding, a layer-contract violation) forces `publishReady: false`. When something that
applies could not be measured the score is `null` and `qualityReason` says what.

A design base that could not be used (undecodable, no alpha, hiding the photograph) is
replaced by plain fills and gradients drawn in CSS, with no extra model call and no
substitute artwork. The job is delivered as `SUCCEEDED_WITH_WARNINGS`, `fallbacksUsed`
names what was substituted and what was omitted, and the design needs review.

### `reviewStrictness`

`designFreedom` says how freely the concept may be *generated*; `reviewStrictness`
says how differences are *judged*. At `designFreedom: 0` the generator tries for
maximum adherence to the reference's structure and style, but the reference only
controls layout and styling: **your brief controls the subject and the text**, so a
different photograph is not a failure. Under `"balanced"` review:

- **Warnings** (reported, never fail the job): small position, spacing or size
  differences; minor decorative variations; an extra subtle line or ornament; natural
  photographic differences that keep the visual balance; localized Hebrew typography
  adjustments.
- **Blocking** (corrected, and fail the job if they cannot be): a material change to
  the composition or hierarchy; imagery that interferes with text; missing or wrong
  approved text (including the business name), wrong contact details, unsupported
  claims or unreadable required text; broken HTML or assets, non-editable text,
  invalid layer separation or alpha, rendering failures.

Warnings come back with a successful result as `result.warnings` (and `qa.warnings`).
Nothing that was already in the response changed.

### Direction (`layoutDirection`, `textDirection`, `referenceLayoutPolicy`)

Direction is resolved **before the concept is generated**, so a Hebrew design is
composed right to left from the start rather than mirrored afterwards - mirroring
a finished design flips the photograph, the logo and every pictogram with it.

Resolved defaults, in precedence order:

1. **An explicit value in the request.** `layoutDirection: "ltr"` lays the columns
   out left to right whatever the copy. `textDirection` is separate, because a
   Hebrew design still contains left-to-right phone numbers.
2. **Explicit placement in the brief, or `referenceLayoutPolicy: "preserve"`.** A
   brief that says "text on the left, photography on the right" (in any language),
   or asks for the reference's composition to be kept, keeps the reference's own
   direction.
3. **The locale of the approved copy** (`referenceLayoutPolicy: "adapt-to-locale"`,
   the default). Hebrew or Arabic copy produces a right-to-left composition: the
   text column on the right and, in a split layout, the photography on the left.
   Latin copy produces a left-to-right one.

What is never done: mirroring a photograph, a logo, an icon or a pictogram, and
reversing the character order of a business name, phone number, email or URL
inside a right-to-left line. Only column positions move.

The resolved values, the reason and which rule decided them are recorded in the
versioned policy document (`result.qa` references it), the concept is generated
and reviewed under them, and the concept review answers
`compositionDirectionOk`. A wrong direction is a concept defect, corrected within
the existing concept-correction bound; nothing changes direction after approval.

Leaving all three at their defaults hashes identically to a request that omits
them, so an existing `requestId` is not a conflict.

### `designFlow`

`"legacy"` (default) generates a concept and decomposes it into layers.
`"layout-first"` (only where the deployment enables it) plans an editable layout and
generates just a text-free photograph. Either way, a wording, outline or position
problem is repaired by patching the design, **not** by regenerating images. Edits to a
finished design use `updateLayeredDesign` / `exportLayeredDesign` - see
docs/REPAIR_ROUTING.md for the contract (`expectedVersion`, `requestId`, no image
generation per edit).

### Response

```json
{ "success": true, "accepted": true, "jobId": "9f2c1a7b…", "status": "QUEUED" }
```

Returned in well under a second. `accepted: true` means the job is durable and
the pipeline has started — **not** that a design exists.

### Idempotency

`requestId` is scoped to your `ownerScope`, so two tenants can reuse the same
value without colliding.

- **Same `requestId`, same content** → the original `jobId`, no second paid run.
- **Same `requestId`, different content** → `IDEMPOTENCY_CONFLICT`.

Safe to retry on a network timeout: a retry either starts the one job or adopts
the run already in flight.

---

## 2. Poll for the result

```json
{
  "operation": "getLayeredDesignStatus",
  "jobId": "9f2c1a7b…",
  "trustedCallerContext": { "ownerScope": "tenant-4812" }
}
```

While running, you get the real stage rather than an invented percentage:

```json
{
  "success": true,
  "jobId": "9f2c1a7b…",
  "status": "RUNNING",
  "stage": "DECORATION_OVERLAY",
  "updatedAt": "2026-10-17T13:35:08.918291+00:00"
}
```

`status` is one of:

| Status | Meaning |
|---|---|
| `QUEUED` | Accepted, not started. |
| `RUNNING` | In progress; `stage` says where. |
| `SUCCEEDED` | Finished and passed review. `publishReady: true`. |
| `SUCCEEDED_WITH_WARNINGS` | Finished with usable, editable HTML, but something is unresolved: findings remain, a repair budget was used up, a repair made no progress, the generated design base had to be replaced by a CSS fallback, or processing ended early. `publishReady: false`, `needsReview: true`. |
| `FAILED` | No usable candidate exists. `error` says why. |

Poll every few seconds; a typical job runs several minutes, bounded at 30. A client
that stops only on `SUCCEEDED` will poll forever on a job delivered with warnings:
stop on all three final statuses.

`stage` is one of `PREFLIGHT`, `PREPARE_COPY`, `ANALYZE_REFERENCE`, `CONCEPT`,
`REVIEW_CONCEPT`, `EXTRACT_SCENE`, `DECORATION_OVERLAY` (the design base),
`CLEAN_PHOTO` (the background photograph), `INDEPENDENT_ASSETS`, `VALIDATE_ASSETS`,
`ASSEMBLE`, `RENDER`, `REVIEW`, `FINALIZE`. The layout-first flow uses `PLAN_LAYOUT`
and `GENERATE_PHOTO`; a reused concept uses `REUSE_CONCEPT`. Repairs loop back through
the later stages, so `stage` can go backwards.

**While running, after the first render** the same response also carries the draft
(full description under "Draft and final results" above):

```json
{
  "success": true, "jobId": "9f2c1a7b…", "status": "RUNNING", "stage": "REVIEW",
  "milestones": { "firstHtmlRenderedAt": "2026-10-29T07:12:40+00:00",
                  "timeToFirstHtmlMs": 214000, "firstCandidateId": "attempt-1",
                  "firstHtmlRecovered": false },
  "draft": { "state": "draft", "candidateId": "attempt-1", "reviewState": "under-review",
             "final": false, "publishReady": false, "needsReview": true,
             "html": "<div class=\"image container\" …", "htmlUrl": "https://…",
             "previewUrl": "https://…", "linkExpiresInSeconds": 900 }
}
```

The draft may be repaired or replaced before the job ends: never publish it, and expect
the final HTML to differ.

Note that `success` describes the *lookup*, not the design. A failed job is a
successful lookup: `success: true` with `status: "FAILED"`.

---

## 3. The finished design

```json
{
  "success": true,
  "jobId": "9f2c1a7b…",
  "status": "SUCCEEDED",
  "updatedAt": "2026-10-17T13:41:52.004311+00:00",
  "result": {
    "jobId": "9f2c1a7b…",
    "status": "SUCCEEDED",
    "canvas": { "width": 1024, "height": 1024 },
    "html": "<div class=\"image container\" style=\"opacity:1;width:1024px;…",
    "htmlHash": "31d9c20b0539ac4acd2f6301218202424a9b262d5a9fcb7f82a9c15b61654cd1",
    "preview": "https://res.cloudinary.com/broadcust/image/upload/v1792268625/layered-design-dev/9f2c1a7b/preview.png",
    "assetManifestHash": "4002445200db6e2254b564aee7950444ea8228b289c596a3067dc437a0f7e619",
    "assets": [
      {
        "assetId": "background",
        "url": "https://res.cloudinary.com/broadcust/image/upload/v1792268616/layered-design-dev/9f2c1a7b/background.png",
        "sha256": "6cf14b062cc0ba3b0e8b4f6c2a1d9e7f3b5c8a2d4e6f1a3b5c7d9e1f2a4b6c8d"
      },
      {
        "assetId": "decoration",
        "url": "https://res.cloudinary.com/broadcust/image/upload/v1792268616/layered-design-dev/9f2c1a7b/decoration.png",
        "sha256": "2c44268d153626b1f7a9c3e5d8b2f4a6c9e1d3b5f7a9c2e4d6b8f1a3c5e7d9b2"
      },
      {
        "assetId": "preview",
        "url": "https://res.cloudinary.com/broadcust/image/upload/v1792268625/layered-design-dev/9f2c1a7b/preview.png",
        "sha256": "4a65f85a9111b044c2e6d8f1a3b5c7e9d2f4a6b8c1e3d5f7a9b2c4e6d8f1a3b5"
      }
    ],
    "layerMap": [
      { "id": "background-photo", "kind": "background", "assetId": null, "zIndex": 0,
        "box": { "x": 0, "y": 0, "width": 1024, "height": 1024 } },
      { "id": "graphic-overlay",  "kind": "decoration", "assetId": null, "zIndex": 1,
        "box": { "x": 0, "y": 0, "width": 1024, "height": 1024 } },
      { "id": "headline", "kind": "text", "role": "title", "contentKey": "title.primary", "zIndex": 2,
        "box": { "x": 472, "y": 48, "width": 466, "height": 174 } },
      { "id": "subtitle", "kind": "text", "role": "subtitle", "contentKey": "subtitle.primary", "zIndex": 2,
        "box": { "x": 452, "y": 260, "width": 508, "height": 42 } },
      { "id": "phone", "kind": "text", "role": "userInformation", "contentKey": "contact.phone", "zIndex": 2,
        "box": { "x": 125, "y": 939, "width": 165, "height": 38 } }
    ]
  }
}
```

### Success, warnings and `publishReady`

A finished job returns `result` for both `SUCCEEDED` and `SUCCEEDED_WITH_WARNINGS`;
the response also carries a small `outcome` object (`qualityPassed`, `needsReview`,
`publishReady`, `stopReason`, `selectedAttempt`) so a poller can decide without
opening the large result.

| `result` field | Meaning |
|---|---|
| `publishReady` | Safe to publish without review. The only field to gate publication on. |
| `needsReview` | A person should look first. |
| `qualityPassed` | Review passed with no blocking finding. |
| `stopReason` | `APPROVED`, `REPAIR_NO_PROGRESS`, `CORRECTION_EXHAUSTED`, `REPAIR_ROUTE_UNRESOLVED`, or the error code of a later step that failed after a usable candidate existed. |
| `warnings[]` | `{code, severity, affectedElementIds, message, suggestedAction}` for the delivered candidate only. |
| `selectedAttempt`, `designSelection` | Which candidate was delivered and why it beat the others. The best candidate is delivered, not the last one. |
| `diagnostics` | For a job delivered after an early stop: the original reason and the candidates considered. |
| `metrics` | Cost and time: per-stage durations, `imageGenerationCalls`, `renderCalls`, repair routes, cache hits. |
| `pipelineVersion`, `templateFlexibility`, `templateTolerance` | What produced it and the tolerance used (layered-v2 jobs). |
| `timing`, `milestones` | When the first and final HTML were rendered and how long the job took (layered-v2). |
| `qualityScore`, `qualityBreakdown`, `rubricVersion`, `qualityReason`, `criticalFindings` | The provisional score (see above). `qualityScore` may be `null`. |
| `layerContract`, `fallbacksUsed` | Whether each layer has one owner, and anything substituted (layered-v2). |

`publishReady` is `false` whenever approved text is missing, a blocking finding
remains, the layer contract is violated, or a CSS fallback replaced the design base.
A design with missing mandatory content is **never** reported as passing, and is
delivered only so it can be fixed by hand.

### What each part is for

**`html`** is the deliverable. Self-contained, no scripts, no external
stylesheet, referencing the asset URLs above. Fonts resolve by family name.
Structure is fixed and documented in [CONTRACTS.md](CONTRACTS.md) section 5 —
downstream editors parse it, so it does not vary between jobs:

```html
<div class="image container" style="width:1024px;height:1024px;position:relative;overflow:hidden;">
  <div class="content">
    <div class="box">
      <img class="background" id="background-1" src="…/background.png" …>
      <div class="badges b1" id="decorations-1" …>
        <img src="…/decoration.png" …>
      </div>
      <div class="words d1001 fxs" id="text-1001" data-role="title" style="…direction:rtl;…">
        <div class="text">חופשת חורף באיטליה</div>
      </div>
    </div>
  </div>
</div>
```

To edit text, replace the contents of `.words > .text`. All styling lives on the
outer `.words` element, so copy changes never disturb layout.

**`assets`** are the separated images on durable public URLs. `background` is the
photograph with every word and graphic removed and the scene behind them rebuilt.
`decoration` is the artwork on a genuinely transparent background — hide it and
the photo and text remain intact.

**`layerMap`** describes every layer without parsing the HTML: its box in canvas
pixels from the top-left, its stacking order, and for text its `role` and
`contentKey`. Useful for building an editing UI.

**What arrives as its own element.** After the concept is approved it is taken
apart, and what the approved picture showed as one image comes back as separate
things you can move, restyle, hide or delete on their own:

* the call-to-action background, the circles behind contact pictograms, and
  decorative panels, rings, dots and chevrons are CSS shapes — a `.words dNNNN`
  element with an empty `.text` and `data-role="decoration"`;
* each contact pictogram is its own transparent PNG asset (`icon-phone`,
  `icon-email`, `icon-location`, `icon-website`) placed in a `.badges` element,
  so a contact row is three independent layers: the circle, the pictogram and the
  value;
* the photograph and the remaining hand-drawn artwork stay as the `background`
  and `decoration` images.

None of these is painted twice: whatever the layout draws is removed from both
images, so hiding or moving one leaves nothing behind. If a decorative shape
could not be separated from the artwork, it stays part of it and the design is
delivered with a `COMPONENT_LEFT_RASTERIZED` warning naming the layers — the
picture looks as approved, those shapes just cannot be restyled on their own.
This never applies to the call-to-action background, the contact circles or the
pictograms; if one of those cannot be separated, the job fails instead.

**`preview`** is the rendered design as a PNG, for thumbnails. `previewUrl` is the
same URL under its newer name; `preview` is kept for existing callers.

**References to stored objects.** `resultKey`, `htmlKey`, `previewKey` and
`qaKey` are S3 keys in the artifact bucket for objects the service really
stores: this result, the delivered HTML, the reviewed render (a PNG) and the
review verdict. They are references, not a way to fetch the design — the bucket
is private, and a caller without bucket access gets the same content from the
`html`, `layerMap` and `qa` fields of this response.

**`qa`** is the review record for the version you received:

| Field | Meaning |
|---|---|
| `passed` | `true` only when every technical check passed and there are zero blocking issues. |
| `blockingIssues`, `suggestions` | What review found. A delivered design has no blocking issues. |
| `warnings` | Non-blocking findings from the concept and final reviews (`stage`, `code`, `category`, `affected`, `impact`, `suggestion`). Reported, never a reason for failure. |
| `conceptSelection` | Which concept candidate every stage used: `attempt`, `key`, `sha256`, `policyVersion`, `selectedBy`, `reason`. |
| `renderChecks` | One entry per required render check, each `passed`, `failed` or `missing`. |
| `renderKey`, `renderSha256` | Identifier and hash of the exact render that was reviewed. |
| `htmlHash`, `assetManifestHash` | The version the review applies to. |

A check that did not run is reported `missing` and blocks approval; it is never
counted as passing. FINALIZE refuses to deliver a document or render whose hash
differs from the one that was reviewed.

**`htmlHash` / `assetManifestHash`** let you verify nothing changed in transit.
`SUCCEEDED` is only set after every asset is durable and these hashes match.

### Geometry

All coordinates are **pixels from the canvas top-left**. `fontSize` is pixels;
`lineHeight` is a dimensionless multiplier. Normalized 0–1 coordinates are never
used anywhere.

Layer `id`s are chosen per design and are not stable between jobs. Use `role`
and `contentKey` to identify a layer's purpose; use `id` only to match a
`layerMap` entry to its element within the same design.

### Text is fitted to its box before delivery

Font sizes in the delivered design may be slightly smaller than the layout
originally called for. Before the design is rendered, each text block is
measured in the same browser that renders it, and any block whose text does not
fit its box is reduced to the largest size that does — typically by a few
percent, and never below 75% of the original.

This exists because a box that is even slightly too narrow does not overflow a
little, it overflows a lot: the text wraps onto a second line and the block
doubles in height. Measuring with the real font catches that before it reaches
your design.

Reductions are bounded on purpose. Text that still does not fit at the floor is
left at the floor and reported as a defect rather than shrunk further, because
illegible text that passes inspection is worse than visible overflow that does
not.

---

## Editing a finished design

Only for `SUCCEEDED` and `SUCCEEDED_WITH_WARNINGS` jobs. Edits patch the design; they
never regenerate images (`imageGenerationCalls: 0`) and never render.

```json
{ "operation": "updateLayeredDesign",
  "trustedCallerContext": { "ownerScope": "tenant-4812" },
  "request": { "jobId": "9f2c1a7b…", "requestId": "edit-42", "expectedVersion": 3,
    "changes": { "content":    { "title.primary": "New headline" },
                 "typography": { "t_title": { "strokeColor": "#FFFFFF", "strokeWidth": "3" } },
                 "layout":     { "t_phone": { "y": "900" } } } } }
```

- `expectedVersion` is the `designVersion` you last saw; a stale one is
  `STALE_DESIGN_VERSION` with `details.currentVersion` — reload, re-apply, resend with
  a **new** `requestId`.
- An identical retry (same `requestId`, same payload) is safe; the same `requestId`
  with different content is `IDEMPOTENCY_CONFLICT`.
- Unknown keys, empty text and out-of-range values are `INVALID_EDIT`.
- Debounce typing (about 800 ms) and send one update per burst.
- `{"operation": "exportLayeredDesign", "request": {"jobId": "…", "expectedVersion": 4}}`
  is the only call that assembles and renders: it returns `html`, `htmlHash`,
  `previewUrl` and renderer `problems`, reuses published asset URLs and calls no image
  model. Call it on an explicit Save/Export, not while typing.

Full contract: [REPAIR_ROUTING.md](REPAIR_ROUTING.md) section 5. These two operations
are **direct calls only**; they are not in the Bedrock action group.

---

## 4. Failures

A failed job names the stage that failed and why:

```json
{
  "success": true,
  "jobId": "9f2c1a7b…",
  "status": "FAILED",
  "updatedAt": "2026-10-17T13:38:20.551904+00:00",
  "error": {
    "stage": "DECORATION_OVERLAY",
    "code": "INVALID_TRANSPARENCY",
    "message": "The decoration overlay is effectively opaque across the canvas (0.4% transparent); it would hide the photograph.",
    "retryable": false,
    "category": "asset-validation"
  }
}
```

Rejections at acceptance use the identical shape, with
`success: false, accepted: false`.

`retryable` tells you whether resubmitting could plausibly help. A provider
timeout is retryable; an unapproved font is not.

### Codes worth handling

| Code | Meaning | Your move |
|---|---|---|
| `INVALID_REQUEST` | The request failed validation; `message` says which field. | Fix and resubmit. |
| `IDEMPOTENCY_CONFLICT` | `requestId` reused with different content. | Use a new `requestId`. |
| `JOB_NOT_FOUND` | Unknown job, or not yours. | Check the `jobId` and `ownerScope`. |
| `CAPABILITY_MISSING` | The deployment is misconfigured — all problems listed at once. | Operator fix, not a caller fix. |
| `COPY_INVALID` | The copy manifest invented a fact or dropped a `requiredText`. | Usually transient; retry. |
| `INVALID_SCENE` | Layout geometry was out of bounds, or copy was altered. | Retryable. |
| `INVALID_TRANSPARENCY` | (Older jobs, `layered-v1`.) The overlay came back opaque or with the photo baked in. | Repaired automatically up to twice, then terminal. For current jobs an unusable design base is not a failure: it becomes a CSS fallback and `SUCCEEDED_WITH_WARNINGS`. |
| `MANDATORY_CONTENT_MISSING` | No candidate carries all approved text, so none can be delivered. | Check the copy; retry. |
| `REVIEW_BLOCKED` | No candidate was approved and none could be delivered as a usable best effort. | Adjust the brief or reference; resubmit. |
| `RENDER_FAILED`, `ASSEMBLY_FAILED`, `FONT_MISSING` | A technical step failed. If an earlier candidate was intact the job is delivered with warnings instead. | Retry if `retryable`. |
| `FINAL_HASH_MISMATCH` | The stored document or preview is not what was reviewed. Nothing is delivered. | Resubmit. |
| `STALE_DESIGN_VERSION`, `DESIGN_NOT_EDITABLE`, `INVALID_EDIT` | Edit-call errors: stale version, job not finished, invalid change. | See "Editing a finished design". |
| `IMPOSSIBLE_CONTENT_REQUIREMENT` | The design needs more than 3 generated images. | Simplify the brief or supply the images. |
| `REQUIRED_COPY_MISSING` | No concept candidate carried all of the approved text (for example the business name) after 2 concept correction rounds. Category `content`. | Check the copy; retry. The message names what is missing. |
| `POLICY_VERSION_MISMATCH` | A stored policy or reference analysis was produced under another policy version. | Submit the request again. |
| `REFERENCE_ADHERENCE_FAILED` | No concept candidate was free of material (blocking) structural problems after 2 concept correction rounds. Category `reference-adherence`. | Raise `designFreedom` deliberately, or adjust the brief or reference. The service never raises it for you. |
| `REFERENCE_CONSTRAINT_CONFLICT` | Required content cannot fit the reference layout at this `designFreedom`. | Raise `designFreedom`, shorten the copy or choose another reference. |
| `CORRECTION_EXHAUSTED` | Still had blocking defects after 2 correction rounds. | Adjust the brief. |
| `REPAIR_ROUTE_UNRESOLVED` | The review's findings could not be turned into a bounded repair (for example a correction that names no element, or a value outside what the property accepts). No image was generated and the best candidate is preserved. | Resubmit; the message says what could not be routed. |
| `PROVIDER_OUTCOME_UNKNOWN` | A paid generation timed out; we cannot tell whether it completed. | **Not retried automatically** — a retry is what would pay twice. |

Full list in `lib/errors.py`.

---

## What the service guarantees

**No invented facts.** Prices, dates, discounts, phone numbers and claims must be
traceable to your brief or business details. Enforced in code: a model that
writes "20% off" when your brief has no discount fails the job rather than
shipping a design you would be obliged to honour. Reformatting is allowed — "499
דולר" may become "$499" — because grounding is on the value, not the wording.

**Your `requiredTexts` survive verbatim.** A reworded one fails the job.

**Real text, not pixels.** Final copy is always HTML text, never baked into an
image, so it stays editable and selectable.

**Hebrew and RTL.** Hebrew copy renders right-to-left; Latin contact details
inside an otherwise Hebrew design keep their own `ltr` direction, so a phone
number is not reordered.

**Verified before delivery.** Every design is rendered in a real browser and
checked — at full size and at 360px feed width — for overflowing text, missing
fonts, images that failed to load, and layers escaping the canvas. Up to two
correction rounds run automatically. A design with blocking defects is never
reported as passing: it is delivered as `SUCCEEDED_WITH_WARNINGS` (when usable HTML
exists) with `publishReady: false` and the unresolved findings listed, or the job
fails if there is nothing usable.

**One owner per layer.** The photograph, the structural design base, editable text,
text backgrounds (CTA, contact circles) and icons are separate layers, and nothing is
drawn twice. A violation is reported in `layerContract` and forces review.

**Light repairs cost no image.** Wording, outline, position, colour and gradient
fixes patch the design in code. Image generation is spent only on a design-base repair
(at most once, after the first HTML) and, with an explicit documented grant, a photograph
repair.

---

## Limits

| | |
|---|---|
| Canvas | 256–2048 px per side |
| Brief | 12,000 characters |
| Supplied assets | 10 |
| Generated inset images | 3 per design |
| Concept correction rounds | 2 |
| Design-base (template) image repairs | 1, after the first HTML |
| DOM/CSS repair rounds (content, typography, layout) | 2 in total |
| Photograph image repairs | 0 unless explicitly granted (cap 1) |
| Typical image calls (current pipeline) | 3: concept, design base, background photograph |
| Draft links | expire after 15 minutes; poll again for a fresh one |
| Job duration | 30 minutes |
| Retention | A **saved** (SUCCEEDED) design is kept. Working artifacts and failed jobs expire after 30 days |

Designs are generated at the nearest supported aspect (1024×1024, 1024×1536 or
1536×1024) and resampled to your exact canvas.

**What is kept, and what is not.** A job that reaches `SUCCEEDED` keeps its job
record, its `result.json`, the delivered HTML, the reviewed render, the review
verdict, the request, and the source image behind every asset. None of these are
deleted automatically, so `getLayeredDesignStatus` keeps answering for a saved
design. Delivered assets and the preview also live on the media CDN.

Everything else is temporary and expires after 30 days: the concept, earlier
attempts, repair images and the other working files of a finished design. A
**failed** job keeps nothing — its record and all its artifacts expire.

Retention here means "excluded from automatic deletion", not a copy into separate
storage. Files written before this was introduced carry no expiry tag, so they are
never auto-deleted; the job records of those older jobs still expire at 30 days.

---

## Invoking it

All operations are on the same Lambda, invoked directly:

```bash
aws lambda invoke \
  --function-name broadcust-layered-design-dev-api \
  --cli-binary-format raw-in-base64-out \
  --payload '{"operation":"runLayeredDesign","trustedCallerContext":{"ownerScope":"tenant-4812"},"request":{…}}' \
  /dev/stdout
```

**There is deliberately no HTTP endpoint.** No Function URL and no API Gateway,
unlike `openai-image-api`. The caller is an authenticated application that
invokes the Lambda directly, which keeps paid image generation off a publicly
reachable URL. A Bedrock agent reaches the same function through its Action
Group (see below), not through HTTP.

If you do put HTTP in front of it, `ownerScope` must be derived from the
authenticated session there. It is the only thing separating one tenant's jobs
from another's, so a request body that can set it is a request body that can
read someone else's designs.

### Direct calls are authenticated by IAM, not by the payload

`trustedCallerContext` is accepted because only principals allowed
`lambda:InvokeFunction` on the API function can send it. Nothing in the handler
verifies it. Grant that permission only to the application backend, and have the
backend derive `ownerScope` from its own authenticated session.

## Bedrock Action Group

The same Lambda serves a Bedrock agent (SPEC section 7). The handler recognises an
agent event by `messageVersion`, `actionGroup` and `function`, and answers in the
agent response envelope. Direct calls keep the bare format above; both paths run
the same validation, authorization and service code.

The agent can start a job and read its status. It cannot edit or export (use the direct
`updateLayeredDesign` / `exportLayeredDesign`) and cannot pass `reuseConceptFromJobId`.
When the agent reads a status, treat `SUCCEEDED_WITH_WARNINGS` as final too. The two
functions are defined in [`bedrock/action-group.json`](../bedrock/action-group.json):

| Function | Parameter | Returns |
|---|---|---|
| `runLayeredDesign` | `requestJson` (string; may include `designFreedom` and `templateFlexibility`) | `{success, accepted, jobId, status: "QUEUED"}` |
| `getLayeredDesignStatus` | `jobId` (string) | Status and stage, or a compact result |

Starting a job confirms acceptance only. Do not wait for the design inside the
agent turn; the backend keeps the `jobId` and reads the terminal status itself.

**Compact result.** On `SUCCEEDED` the agent receives references and hashes only:

```json
{
  "success": true,
  "jobId": "9f2c1a7b…",
  "status": "SUCCEEDED",
  "result": {
    "resultKey": "layered-design/9f2c1a7b…/FINALIZE/1/result.json",
    "previewKey": "layered-design/9f2c1a7b…/RENDER/1/native.png",
    "previewUrl": "https://res.cloudinary.com/…/preview.png",
    "qaKey": "layered-design/9f2c1a7b…/REVIEW/1/review.json",
    "designFreedom": 0,
    "htmlHash": "…",
    "assetManifestHash": "…"
  }
}
```

Fields that are unavailable are omitted, except `qualityScore` and `qualityReason`,
which are kept even when null (no score, and why). The compact result also carries
`publishReady`, `needsReview`, `qualityPassed`, `stopReason`, `selectedAttempt`,
`pipelineVersion`, `templateFlexibility`, `fallbacksUsed`, `milestones` and `timing`
when present. While a job runs, `draft` is reduced to its state (`candidateId`,
`reviewState`, `renderedAt`, `final: false`, `publishReady: false`, `htmlHash`); its
HTML and links are left out. There is no full HTML, layer map, asset
list or QA detail in an agent response. The backend gets the complete result —
HTML, `layerMap`, `assets`, `qa` — by calling `getLayeredDesignStatus` **directly**
on the Lambda. That is why the backend never needs read access to the bucket.

A failed design job answers `success: true` with `status: "FAILED"` and its stage
error. Invalid or unauthorized requests answer `success: false` and mark the
envelope `responseState: "FAILURE"`.

### Job ownership for agent calls

An agent event carries no trusted caller context, so ownership comes from the
session attribute `ownerScope`. The application backend sets it in its
`InvokeAgent` call; the model cannot write session attributes, and `ownerScope` is
never an Action Group parameter. A call without it fails with `UNAUTHORIZED`.
`promptSessionAttributes` are never used for identity.

**The backend must send the same value as `trustedCallerContext.ownerScope` on its
direct calls that it set as the `ownerScope` session attribute.** The two are
hashed to the same scope; any mismatch makes jobs started by the agent invisible
to the backend.

### Attaching the agent

Add the functions from `bedrock/action-group.json` to the agent's Action Group,
point it at `broadcust-layered-design-<stage>-api`, and put the agent's ARN in
`bedrock_agent_arn` in `config.<stage>.json`. The deploy then adds a Lambda
permission for `bedrock.amazonaws.com` limited to this account and that agent. An
empty value adds no permission.
