# Using the LayeredDesign service

Turns a marketing brief and an inspiration image into a **layered, editable
design**: the photograph, the decorative artwork and every text block come back
as separate pieces, so the result can be edited rather than regenerated.

Two operations, both on the API Lambda. Designs take minutes to produce, so this
is an async job API — you get a `jobId` immediately and poll for the result.

- `runLayeredDesign` — accept a brief, return a `jobId`
- `getLayeredDesignStatus` — read progress, then the finished design

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
    "suppliedAssets": []
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
| `requestId` | **yes** | Your idempotency key. See below. |

**`trustedCallerContext.ownerScope`** establishes who owns the job. It must come
from your authenticated session — never from the end user's request body, or a
caller could claim another tenant's jobs. Jobs are readable only by the scope
that created them, and an unknown job and someone else's job are deliberately
indistinguishable.

**The inspiration image is inspiration, not a template.** Its depth, lighting and
typographic feel are learned from; its layout is not copied. Supply real pixels on
an authorized origin — a description of an image will not do.

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

`status` is one of `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`. Poll every few
seconds; a typical job runs several minutes, bounded at 30.

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

**`preview`** is the rendered design as a PNG, for thumbnails. `previewUrl` is the
same URL under its newer name; `preview` is kept for existing callers.

**References to stored objects.** `resultKey`, `htmlKey`, `previewKey` and
`reviewKey` are S3 keys in the artifact bucket for objects the service really
stores: this result, the delivered HTML, the reviewed render (a PNG) and the
review verdict. They are references, not a way to fetch the design — the bucket
is private, and a caller without bucket access gets the same content from the
`html`, `layerMap` and `qa` fields of this response.

**`qa`** is the review record for the version you received:

| Field | Meaning |
|---|---|
| `passed` | `true` only when every technical check passed and there are zero blocking issues. |
| `blockingIssues`, `suggestions` | What review found. A delivered design has no blocking issues. |
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
    "retryable": false
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
| `INVALID_TRANSPARENCY` | The overlay came back opaque or with the photo baked in. | Repaired automatically up to twice, then terminal. |
| `IMPOSSIBLE_CONTENT_REQUIREMENT` | The design needs more than 3 generated images. | Simplify the brief or supply the images. |
| `CORRECTION_EXHAUSTED` | Still had blocking defects after 2 correction rounds. | Adjust the brief. |
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
correction rounds run automatically. A design with blocking defects fails rather
than being delivered.

---

## Limits

| | |
|---|---|
| Canvas | 256–2048 px per side |
| Brief | 12,000 characters |
| Supplied assets | 10 |
| Generated inset images | 3 per design |
| Correction rounds | 2 |
| Overlay repairs | 2 |
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

Both operations are on the same Lambda, invoked directly:

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

The two functions are defined in [`bedrock/action-group.json`](../bedrock/action-group.json):

| Function | Parameter | Returns |
|---|---|---|
| `runLayeredDesign` | `requestJson` (string) | `{success, accepted, jobId, status: "QUEUED"}` |
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
    "htmlKey": "layered-design/9f2c1a7b…/ASSEMBLE/1/design.html",
    "previewKey": "layered-design/9f2c1a7b…/RENDER/1/native.png",
    "previewUrl": "https://res.cloudinary.com/…/preview.png",
    "reviewKey": "layered-design/9f2c1a7b…/REVIEW/1/review.json",
    "htmlHash": "…",
    "assetManifestHash": "…"
  }
}
```

Fields that are unavailable are omitted. There is no full HTML, layer map, asset
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
