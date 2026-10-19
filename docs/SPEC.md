# Cursor implementation instructions: standalone LayeredDesign service

Updated: 2026-10-19. Complete standalone implementation specification.

## 0. Implementation sequence and acceptance gates

Build and validate the visual pipeline before implementing production orchestration. The AWS resources and Bedrock adapter below are required for the final delivery; they are not prerequisites for the first visual prototype. This section defines implementation order, not a reduction in final scope.

### Phase A — Contracts and one complete visual prototype

First implement request normalization, designFreedom policy, authoritative copy, scene/layer schemas, provider interfaces and the HTML/editor contract. Keep stage logic independent of Lambda event handling and storage implementation.

Provide a local command:

```bash
node prototype.mjs --request examples/request.json --output artifacts/prototype
```

The local runner executes the same core stage functions later used by the stage Lambda. It may run synchronously and store artifacts locally. It does not implement or pretend to satisfy the asynchronous production start/status contract. Use real image-editing and vision providers for the acceptance run; mocks are limited to software tests.

Run one actual brief/reference through concept creation and review, asset separation, HTML assembly, rendering and review. Start with the Italy winter travel example with designFreedom omitted; verify that it resolves to 0. Obtain or configure actual authorized reference pixels, provider credentials, fonts and editor styles; do not invent working integrations.

Test separation as soon as the first concept passes review. Produce the clean photograph and aligned true-alpha decoration overlay from the SAME frozen concept. Before building orchestration, demonstrate that removing the overlay reveals a complete photo with no baked text-support panels, and that overlaying it restores the intended artwork without leaking photographic content or duplicated text.

For local inspection only, relative asset paths served by a local static server or embedded assets are allowed. Mark the output as a prototype. This exception does not permit temporary local or expiring URLs in saved customer designs. Production finalization still requires stable media delivery.

Required first-milestone artifacts:
- Exact normalized request, reference, authoritative copy, derived policy and model/provider identities.
- Approved concept and its review.
- Separate photo, true-alpha decoration overlay and any independent assets.
- Validated scene JSON, editable HTML and layer manifest.
- Native-size and 360px-wide feed renders.
- A labeled reference/concept/HTML comparison plus a separation demonstration.
- Technical and visual QA, repair counts, measured stage durations and provider usage/cost where available; mark unavailable cost data as unknown.

The first milestone passes only when the real output is visually convincing, faithfully reconstructs the approved concept and meets the technical checks. Model self-approval alone is insufficient evidence of design quality. Record a human visual review of the comparison and its specific findings. If quality fails, refine the responsible prompts, scene extraction or asset edits before proceeding.

### Phase B — Actual editor verification and variation

Import the prototype into the actual Broadcust editor or a verified integration harness that uses its real styles and layer behavior. A standalone browser preview alone does not prove editor compatibility. If editor access is unavailable, deliver a reproducible import/test package and mark this gate pending; do not claim the editor was tested.

Verify that text can be edited, saved and reopened; decorations can be hidden/moved/replaced independently; the photo and independent images can be replaced without mutating other layers; and Hebrew/LTR contacts retain correct rendering. Geometry changes after deliberate customer edits are not automatically repaired by this creation service.

Test at least three materially different brief/reference pairs, including a longer Hebrew brief and a composition with independent imagery. Initially run each at designFreedom=0, 50 and 100, then extend one common brief/reference to 25 and 75. Use distinct request IDs and preserve all evidence. Do not tune only to the travel example or require random outputs to differ monotonically.

Evaluate two relationships independently:
1. Reference to concept: obey the requested creative policy and documented exceptions.
2. Approved concept to rendered HTML: preserve visual fidelity at every freedom level.

Require readable text at native and feed sizes, accurate facts, genuine separation, coherent composition and absence of generic layout substitution that discards the approved concept. Record pass/fail reasons per case rather than inventing a numeric similarity threshold. One successful hand-crafted example is not proof of repeatable automation.

### Phase C — Production service and required Bedrock integration

After the visual and editor gates pass, wrap the proven core stages in the two-Lambda architecture, Step Functions, DynamoDB and S3 described below. Add durable acceptance, ownership, idempotency, deadlines, bounded recovery, stable asset publishing and status retrieval. Add the required Bedrock Action Group adapter and direct backend retrieval path.

Reuse the validated providers, prompts, schemas and assembler; do not replace them with a second independently implemented pipeline. Production configuration may use different storage adapters, but must preserve the same rendering and validation behavior.

### Phase D — Deployment acceptance and handoff

Run the production-shaped end-to-end, failure, authorization, idempotency, durability and Bedrock/direct-result tests in section 9 in an authorized test environment. Measure latency and provider usage, distinguish estimates from actual charges, and document unresolved dependencies. Do not claim deployment or provider validation without execution evidence.

The first deliverable is the Phase A prototype and its evidence. The final deliverable includes all production requirements below. Work may proceed through passing gates within the authorized scope; a failing gate requires correction, not a silent waiver. No automatic production deployment is authorized by this specification.

## 1. Purpose and responsibility

Implement an AWS service named `LayeredDesign` that receives a marketing brief, an inspiration image, canvas dimensions, optional branding and supplied assets. It produces a polished marketing design as editable HTML with independently replaceable visual layers.

This is a standalone specification. The implementation must not depend on previous conversations, undocumented Lambda functions, an existing agent, or older software versions. If integrating into an existing repository, inspect its actual interfaces and implement explicit adapters. Do not assume interfaces exist merely because this document describes them.

The service owns the complete process: visual concept creation, concept analysis, image separation, editable text placement, deterministic HTML assembly, browser rendering, visual review and bounded correction.

The inspiration image guides visual quality, hierarchy, atmosphere, depth and typography. The caller controls permitted departure from that image using designFreedom (0..100). At 0, preserve its composition and visual system as closely as content, branding and technical constraints allow; at 100, permit complete recomposition. The brief remains the factual source of truth at every setting. See section 2.7 for the binding policy.

Reference discovery is outside this service: the caller supplies the inspiration image. Publishing to social networks and providing a customer-facing editor are also outside scope.

Final design layers must separate:
1. A clean photographic background.
2. A genuinely transparent decoration image containing graphic panels, ribbons and text-support artwork.
3. Independent photos, subjects or products when they need separate replacement or positioning.
4. Real editable HTML text. Never bake final text into generated raster assets.

The decoration image is one editable image group. Its internal ribbons, leaves and effects are not individually editable vectors. Represent this limitation accurately.

## 2. Public operations and asynchronous behavior

Expose two operations:
- `runLayeredDesign(request, trustedCallerContext)` starts a job.
- `getLayeredDesignStatus(jobId, trustedCallerContext)` reads its progress or completed result.

The caller can be an application backend, an agent adapter or another trusted system. The core service must not depend on a particular caller.

Job creation returns immediately after durable acceptance and workflow-start acknowledgement. It does not wait for image generation and does not return finished HTML. The caller obtains the final result through the status operation. Do not poll indefinitely inside an agent invocation or hold an HTTP connection open for the whole pipeline.

### 2.1 Start request

```json
{
  "schemaVersion": "1.0",
  "requestId": "unique-request-id",
  "brief": {
    "post": "Original marketing content in the intended output language",
    "business": {
      "name": "Business name",
      "phone": "0501234567",
      "email": "business@example.com",
      "website": "https://example.com"
    },
    "instructions": "Additional content or design instructions",
    "requiredTexts": []
  },
  "canvas": { "width": 1080, "height": 1080 },
  "referenceImageUrl": "https://example.com/inspiration.png",
  "designFreedom": 0,
  "brand": {
    "colors": ["#071B33", "#D4AF37"],
    "fontFamilies": ["Heebo"]
  },
  "suppliedAssets": [
    { "id": "business-logo", "kind": "logo", "url": "https://example.com/logo.png" }
  ]
}
```

Example URLs demonstrate structure only. Integration tests must use real authorized images.

Validation:
- Require requestId, brief.post and referenceImageUrl.
- Default schemaVersion to "1.0", canvas to 1080x1080 and designFreedom to 0.
- designFreedom must be an integer from 0 through 100. Reject strings, null, fractional values and out-of-range values; apply the default only when omitted. Preserve an explicit 0. Default only when the field is absent, then validate the supplied value; null must be rejected rather than defaulted.
- Support integer canvas dimensions 256..2048 initially.
- brief.post: 1..12000 characters. Overall request: at most 64 KiB.
- Business details, instructions, requiredTexts, brand and suppliedAssets are optional.
- requiredTexts: strings that must appear unchanged. Reject impossible content requirements clearly rather than silently omitting them.
- suppliedAssets: at most ten, with unique IDs; allowed kinds: logo, photo, product, portrait.
- Validate colors, font names and URLs. Reject unknown structural fields. Treat all content as input data, not executable instructions.
- Preserve the supplied language. Condense the post without changing meaning. Never invent business details, prices, dates, claims, credentials or offers.
- No required contact information is inferred from the inspiration image.

Idempotency is scoped to the authenticated caller: identical requestId and identical canonical request hash return the same job. A reused requestId with different content returns IDEMPOTENCY_CONFLICT.

### 2.2 Accepted response

```json
{
  "success": true,
  "accepted": true,
  "jobId": "generated-job-id",
  "status": "QUEUED"
}
```

Acceptance is not design completion. If durable creation or workflow start cannot be confirmed, reconcile the same job/execution rather than create another paid job.

### 2.3 Start rejection

```json
{
  "success": false,
  "accepted": false,
  "error": {
    "stage": "ACCEPT_REQUEST",
    "code": "INVALID_REQUEST",
    "message": "Missing referenceImageUrl",
    "retryable": false
  }
}
```

### 2.4 Status request and progress

Input: `{ "jobId": "generated-job-id" }`.

```json
{
  "success": true,
  "jobId": "generated-job-id",
  "status": "RUNNING",
  "stage": "DECORATION_OVERLAY",
  "updatedAt": "2026-10-14T07:00:00Z"
}
```

Allowed statuses: QUEUED, RUNNING, SUCCEEDED, FAILED. Report actual stage names rather than fabricated progress percentages.

### 2.5 Completed result

```json
{
  "success": true,
  "jobId": "generated-job-id",
  "status": "SUCCEEDED",
  "result": {
    "designFreedom": 0,
    "html": "<div class=\"image container\">...</div>",
    "htmlUrl": "https://media.example.com/design.html",
    "previewUrl": "https://media.example.com/preview.png",
    "canvas": { "width": 1080, "height": 1080 },
    "layers": [
      {
        "id": "background-1", "type": "background",
        "url": "https://media.example.com/photo.png",
        "x": 0, "y": 0, "width": 1080, "height": 1080, "zIndex": 0
      },
      {
        "id": "decorations-1", "type": "decoration",
        "url": "https://media.example.com/decorations.png",
        "x": 0, "y": 0, "width": 1080, "height": 1080, "zIndex": 1
      },
      {
        "id": "text-1001", "type": "text", "role": "title", "contentKey": "title.primary",
        "text": "Marketing headline",
        "x": 540, "y": 80, "width": 480, "height": 180, "zIndex": 10,
        "fontFamily": "Heebo", "fontSize": 72, "fontWeight": 800,
        "color": "#FFFFFF", "align": "right", "direction": "rtl", "lineHeight": 1.12
      }
    ],
    "validation": {
      "passed": true,
      "htmlHash": "sha256-value",
      "assetManifestHash": "sha256-value"
    }
  }
}
```

All geometry in this document is illustrative, not a fixed layout. Include every actual layer and its full rendering properties. Image layers may additionally specify objectFit, objectPosition, border and radius through allowlisted fields.

The HTML, preview, layer map and QA must represent exactly the same final version. Persisted HTML must use stable, immutable/versioned image URLs, not one-hour presigned image links. Protected download links to the HTML or preview may expire if authorized links can be refreshed; embedded image URLs must remain usable in the intended editor/rendering context.

The authenticated direct application status interface returns the full result above after authorization. Compact Bedrock action responses return only real artifact pointers and hashes. The application retrieves the full result by calling the service directly; direct S3 access for the application is not required. Store full content in S3, not DynamoDB. htmlUrl is optional if no separate HTML object is produced; do not fabricate it. A combined result.json containing HTML, layers, assets, designFreedom and validation is sufficient. No mandatory final/ directory or duplicate split files are required.

Artifact key fields contain S3 keys only. A Cloudinary preview URL belongs in previewUrl, never previewKey. Omit fields for nonexistent artifacts. Persist render identity, actual technical check outcomes, issues/suggestions, htmlHash and assetManifestHash in the result or an actual review artifact. Missing checks cannot count as passing.

### 2.6 Failed job

```json
{
  "success": true,
  "jobId": "generated-job-id",
  "status": "FAILED",
  "error": {
    "stage": "DECORATION_OVERLAY",
    "code": "INVALID_TRANSPARENCY",
    "message": "The decoration image is fully opaque",
    "retryable": false
  }
}
```

success:true means the status lookup succeeded; the design job itself failed. Unknown or unauthorized jobs return success:false without exposing other callers' jobs.

### 2.7 Design freedom: caller-controlled reference adherence

`designFreedom` is a creative-control setting, not a measured pixel-difference percentage, similarity score, image-model API parameter or probability. Do not label the result as mathematically "10% different". The implementation translates the number into explicit constraints applied to reference analysis, concept prompts, review and corrections.

| Value/range | Policy mode | Required behavior |
|---|---|---|
| 0 | strict-adherence | Preserve reference composition, dominant image placement/scale, graphic-panel geometry, overlaps, typography hierarchy and reading flow as closely as practical. No discretionary redesign. |
| 1..25 | close-adaptation | Keep the recognizable layout and major anchors. Allow localized adjustments for target copy, supplied assets, brand and fit. |
| 26..50 | guided-adaptation | Preserve selected visual qualities and hierarchy. Reorganize individual regions and proportions when justified by the brief. At 50, meaningful restructuring is allowed. |
| 51..75 | broad-inspiration | Allow major structural changes while adapting selected visual qualities. At 75, the source may be primarily an atmosphere/style reference. |
| 76..100 | free-composition | Allow a new structure, hierarchy, placements and proportions. At 100, no source-layout preservation is mandatory. Factual, brand, quality and editability constraints still apply. |

Default: 0, the lowest available design freedom and strictest reference-adherence policy. Higher freedom requires an explicit caller value. Persist the effective value and policyVersion (initially "1.0") with the job, derived policy, concept evidence and final result. Changing designFreedom changes the canonical request hash; the same requestId with a different value produces IDEMPOTENCY_CONFLICT. An omitted value and explicit 0 should normalize to the same effective request. Do not silently change the value during repairs.

Policy precedence at every level:
1. Factual accuracy, required text, supplied identity assets, explicit caller brand/instructions and technical editability constraints.
2. The reference-preservation policy selected by designFreedom.
3. Optional aesthetic improvements.

At 0, replace source business text, contacts, identities and irrelevant subjects with the target content. Supplied brand colors override source colors. Use approved available fonts with equivalent visual character if the exact font is unavailable. Preserve source text hierarchy/relative scale rather than its literal spelling, business claims or exact line breaks. A source logo must not be copied as the target logo. If a different aspect ratio is requested, map source anchors into the target canvas and document necessary adaptation.

The service cannot guarantee pixel-identical reconstruction at 0. Generative editing, new copy, available fonts and HTML rendering can change pixels. If mandatory content cannot fit while preserving a strict layout, attempt localized fitting without unreadable text; if still incompatible, return REFERENCE_CONSTRAINT_CONFLICT with a concise reason. Do not silently redesign, omit mandatory content or increase designFreedom. Permitted factual/brand adaptations are documented exceptions, not violations.

Implementation requirements:
- Add pure `deriveReferencePolicy(designFreedom)` in reference-policy.mjs with explicit handling of 0 and the table boundaries.
- Include mode, selected value, policyVersion, mustPreserve, mayChange and documentedExceptions in policy.json. The numeric control is qualitative; adjacent values may share a policy band. Do not imply calibrated continuous tolerances that have not been implemented.
- Analyze the actual reference pixels to record major anchors: normalized boxes/relative scale for dominant imagery and graphic panels, approximate text zones and hierarchy, overlap relationships, alignment and reading flow. Record confidence; do not pretend to identify an exact font from pixels.
- For strict/close modes, identify concrete composition constraints from those observations. For guided/broad modes, select useful features without locking all geometry. At 100, no geometric preservation is mandatory, but record how the reference was used if any.
- Render a readable English policy instruction into the concept prompt. Passing only a number or a guessed provider flag does not implement the feature.
- Persist policy evidence and use it for concept review and targeted repairs. A model's aesthetic preference cannot override a strict preservation constraint.

Example instruction for designFreedom=10:

> Preserve the reference composition, dominant image placement and relative scale, graphic-panel geometry, overlap relationships, typography hierarchy and reading flow. Adapt only what is necessary for the supplied target copy, assets and brand. Replace all source text, business identities and irrelevant subject matter. Avoid a new generic layout. Record necessary deviations.

Example instruction for designFreedom=50:

> Adapt selected reference visual qualities and hierarchy to the target brief. You may reorganize regions and proportions to improve content fit and visual impact. Explain the retained inspiration and deliberate structural changes.

Example instruction for designFreedom=100:

> Create the strongest composition for the target brief and brand. You may redesign all layout, scale, hierarchy and visual placement. The reference is optional inspiration, not a geometric constraint. Do not copy source identities or factual content.

There are TWO separate review relationships:
- Reference -> concept: adherence depends on designFreedom and approved exceptions.
- Approved concept -> separated assets + final HTML: reconstruction must remain faithful at EVERY designFreedom value.

A high designFreedom is never permission to simplify or drift from the approved target concept while building HTML. The HTML schema, transparency requirements and quality gates do not change with this setting.

## 3. AWS architecture

Implement these resources in Phase C after the visual and editor gates. During Phases A/B, run the same core stage functions locally with filesystem artifact storage:



| Resource | Responsibility |
|---|---|
| broadcust-layered-design-api Lambda | Validate requests, enforce ownership/idempotency, start jobs and read status |
| broadcust-layered-design-stage Lambda | Execute one bounded stage per workflow invocation |
| Step Functions Standard | Sequence stages, handle branching, bounded retries and correction loops |
| DynamoDB table | Job metadata, owner scope, execution identity and idempotency records |
| S3 artifact storage | Requests, copy manifests, concepts, scenes, assets, HTML, renders and QA |

These two Lambdas implement one logical service. Use Node.js 24 ES modules. Package the stage Lambda as an x86_64 container with Playwright, Chromium, actual font files and sharp when rendering locally. A verified external renderer may be adapted instead.

Initial sizing: API 512MB/30s; stage 2048MB/240s with 2048MB ephemeral storage. Set SDK deadlines below remaining task/Lambda time. Validate resource sizing with measured tests.

Use the repository's IaC framework; use AWS SAM if no framework exists. Provide actual resource definitions, not just console instructions.

Pass only jobId, stage, attempt and small artifact pointers through Step Functions. Never pass image bytes or whole HTML through workflow state. Use immutable artifact keys such as `layered-design/<jobId>/<stage>/<attempt>/...`.

Persist status, current stage, timestamps, request hash, owner scope, execution ARN and bounded attempt counters. Use conditional writes for idempotency and deterministic execution names. Reconcile crashes between job creation and StartExecution acknowledgement without starting another execution.

## 4. Workflow and stage contracts

Implement the following sequence:

PREFLIGHT -> PREPARE_COPY -> ANALYZE_REFERENCE -> CONCEPT -> REVIEW_CONCEPT -> EXTRACT_SCENE -> CLEAN_PHOTO -> DECORATION_OVERLAY -> INDEPENDENT_ASSETS -> VALIDATE_ASSETS -> ASSEMBLE -> RENDER -> REVIEW -> FINALIZE.

In production, dispatch the stage Lambda using `{jobId, stage, attempt}`; independent asset tasks also receive assetId. A bounded Map may process at most three independent generated assets, with concurrency at most two.

The local runner and production dispatcher invoke shared core stage functions through injected provider, artifact-store and renderer interfaces. Keep business logic out of orchestration adapters.

Each stage loads authorized inputs, performs its operation, validates its output, stores immutable output artifacts and returns small pointers. Persist output before advancing. Reuse completed work when input hashes match. No stub may return successful completion.

### PREFLIGHT

Validate request, authorized asset origins, configured image editing, true-alpha capability, vision model, renderer, fonts and stable media publishing. Ordinary preflight does not perform paid image generation; explicit integration tests establish actual provider capabilities. Reject missing essential capabilities clearly.

### PREPARE_COPY

Create a concise authoritative copy manifest grounded in the brief, with stable content keys. Preserve requiredTexts and supplied factual details. Keep phone/email as distinct items. Generated concept text or OCR is never the source of truth. Do not add services, prices or claims to fill space.

### ANALYZE_REFERENCE

Send actual reference pixels, target brief/brand/copy, target canvas and effective designFreedom to vision. Record approximate source anchors, hierarchy, proportions, overlaps, palette and relevant visual qualities. Use deriveReferencePolicy to determine what is mandatory versus optional. Persist reference-analysis.json and policy.json, including justified content/brand/aspect/font exceptions. Validate structured output. These source observations guide concept creation; they do not replace analysis of the actual target concept later.

### CONCEPT

Send actual inspiration pixels, brief, approved copy, brand, supplied assets, canvas and explicit derived reference-policy instructions to the image creation/edit model. The permitted amount of recomposition comes from designFreedom; do not issue an unconditional "freely redesign" instruction at strict/close settings. Internal concept imagery MAY include target text as a layout guide. It is not the customer deliverable. Persist each attempt separately.

### REVIEW_CONCEPT

Before paid separation work, compare actual reference and current concept pixels against the derived policy and authoritative brief. Return structured `{passed, mode, checks, documentedExceptions, blockingIssues, suggestions}`. Each check identifies its observed anchor/feature and expected policy. For strict/close settings inspect panel/subject placement, proportions, overlap and typography hierarchy, using normalized anchor geometry as evidence rather than raw image-pixel similarity. For guided/broad settings review only the preservation requirements actually selected. At 100, source-layout differences are not blockers.

Do not let invented content, unreadable copy or poor composition pass at any setting. Do not report an uncalibrated global similarity percentage. Unjustified policy violations return REFERENCE_ADHERENCE_FAILED and targeted concept repair instructions. Permit at most two concept correction rounds after initial concept generation. This budget is persisted separately from the two final HTML correction rounds; both obey the overall deadline. If policy conflicts cannot be resolved, return a clear terminal error instead of silently relaxing constraints. Freeze the approved concept identity and its policy review before EXTRACT_SCENE or asset separation.

### EXTRACT_SCENE

Send actual concept pixels and the authoritative copy manifest to a vision-capable text model. Return strict JSON, not HTML. Specify text boxes, typography, directions, image positions/crops, layer order and expected transparent/opaque overlay sample regions. Use pixel coordinates from the top-left of the target canvas. lineHeight is a dimensionless multiplier; fontSize is pixels. Never mix normalized coordinates with pixel values.

Validate finite values, positive sizes, bounds, unique IDs/content keys, approved fonts and exact copy references. Restrict zIndex to 0..100. Support headline phrases split into separate text layers without duplicating copy. Reference-image geometry is not final concept geometry.

### CLEAN_PHOTO

Edit the immutable original concept, removing all words, logos, graphic panels/ribbons, separately planned foreground ornaments, independent photos and independent subjects. Reconstruct a complete photographic scene behind removed items. Do not leave blank panel-shaped gaps. Preserve photographic lighting, relevant scenery and ordinary scene participants when not planned as independent assets.

### DECORATION_OVERLAY

Edit the SAME immutable concept, not the already cleaned photograph. Retain only graphic artwork and text-support panels, aligned to the full canvas. Remove photographic scene/sky/buildings, all words/logos and independent imagery. Require genuine RGBA transparency in the photo windows, opaque panels where intended and partially transparent soft edges. No white/black rectangles or baked checkerboards. Do not bake a CTA background here when that background is implemented as CSS on its text layer.

### INDEPENDENT_ASSETS

Extract/create up to three independent generated photo/subject/product assets when needed, preserving crop mapping and compatible lighting. A standalone inset must not contain a miniature duplicate poster. Reuse supplied logos without regenerating them. Each visible element belongs to one layer only.

### VALIDATE_ASSETS

Decode bytes with sharp: validate format, orientation, dimensions, aspect and alpha. Normalize full-canvas photo and overlay consistently to the target canvas. Overlay sample regions derived from the scene must confirm transparent photo windows and visible graphic areas. Reject fully empty/opaque overlays. Do not use a universal transparency percentage threshold for every composition.

Inspect contrasting-background composites with vision for checkerboard patterns, photographic leakage, leftover text and missing artwork. Alpha statistics alone are insufficient. Regeneration can change alignment. Local geometry/crop adjustments are allowed only when they preserve the approved concept. Material panel, silhouette or subject displacement requires targeted asset repair and revalidation; do not redefine the target scene to excuse extraction drift.

### ASSEMBLE

Build HTML deterministically from the validated scene, authoritative copy and resolved asset manifest. Use only allowlisted style fields, never unrestricted model HTML/CSS. Escape plain text and URL attributes. For production delivery, resolve stable versioned media URLs before final assembly/review. Local prototype asset resolution follows the explicit Phase A exception. If stable delivery cannot be provided, fail finalization explicitly instead of persisting expiring customer designs.

### RENDER

Render exact HTML with actual editor styles/fonts; wait for document.fonts.ready and successful image decode. Capture canvas at native size and a 360px-wide scaled feed view. Check layer bounds, unique IDs, loaded images/fonts and actual wrapped text dimensions. Do not conceal overflow with clipping. Missing required fonts/images are blocking errors.

### REVIEW

Send actual concept pixels, current native/feed render pixels, authoritative copy and asset-validation evidence to vision. Evaluate impact, hierarchy, readability, factual accuracy, collisions, duplication, separation and faithful reconstruction of the APPROVED target concept, regardless of designFreedom. Load the approved reference-policy review as evidence; do not apply an unconditional source-layout similarity requirement or override strict reference rules with a blanket inspiration-only instruction. If final reconstruction reveals an upstream policy defect, do not silently approve it: route a bounded concept repair, invalidate dependent outputs and re-run all downstream checks.

Require strict JSON: `{passed, blockingIssues:[{code,layerId,assetId,message}], suggestions:[]}`. Optional IDs are null. Backend approval requires passing technical checks and zero blocking issues; a model saying passed:true does not override failures.

### CORRECTION AND FINALIZE

Allow at most two correction rounds after initial render. Use validated scene patches for placement/typography and targeted asset edits for raster defects. Every changed asset or HTML version requires downstream validation, reassembly, rendering and review. Bound targeted overlay repairs to two per job, not two per task retry.

Hash exact final HTML bytes and a canonical ordered asset manifest containing asset IDs, immutable keys/versions and byte hashes. Store QA with matching concept/render identities, hashes, model identities, attempt, effective designFreedom, policyVersion, reference-policy review and documented deviations. Mark SUCCEEDED only after all final artifacts are durable and final QA matches. Otherwise persist a terminal stage/code/message error.

## 5. HTML/editor contract

Use exactly this nesting, scaled to requested canvas:

```html
<div class="image container" style="opacity:1;width:1080px;height:1080px;position:relative;overflow:hidden;">
  <div class="content" style="width:100%;height:100%;position:relative;">
    <div class="box" style="width:100%;height:100%;position:relative;">
      <!-- Direct-child layers -->
    </div>
  </div>
</div>
```

Background:

```html
<img class="background" id="background-1" src="PHOTO_URL" alt="" style="position:absolute;left:0;top:0;width:1080px;height:1080px;object-fit:cover;z-index:0;">
```

Decoration group:

```html
<div class="badges b1" id="decorations-1" data-asset-type="image" style="position:absolute;left:0;top:0;width:1080px;height:1080px;z-index:1;">
  <img src="DECORATIONS_URL" alt="" style="display:block;width:100%;height:100%;object-fit:contain;">
</div>
```

Editable text:

```html
<div class="words d1001 fxs" id="text-1001" data-role="title" style="position:absolute;left:540px;top:80px;width:480px;height:180px;font-family:Heebo,Arial;font-size:72px;font-weight:800;color:#FFFFFF;text-align:right;direction:rtl;line-height:1.12;box-sizing:border-box;z-index:10;">
  <div class="text">Editable marketing text</div>
</div>
```

Rules:
- Background, .badges and .words are direct children of .box.
- All IDs, dNUMBER and bNUMBER classes are unique; image classes continue b2, b3, etc.
- .badges contains actual images only. Text is real plain copy inside .words > .text, styling on the outer element.
- Allowed roles: businessName, businessDescriptor, title, subtitle, serviceItem, expertiseItem, benefitItem, informationItem, eventDetail, offerDetail, cta, userInformation, icon. Do not invent supporting facts to fill these roles.
- Hebrew defaults to rtl/right; separate Latin contacts use ltr. Deliberate centered layouts are allowed.
- Use configured approved font files/weights; a font name alone does not mean it is installed.
- Use contain for full-canvas overlay/logos/cutouts, cover only for intended photo crops. Do not distort images.
- CTA fill/border/radius may be on its existing .words layer. No empty text layers as shapes or extra grouping wrappers.
- No scripts, SVG, arbitrary CSS, CSS background-image, source-brand text, generated final text in images or clipping to hide text defects.
- Optional icons only if actual licensed FA6 Pro Light assets are configured. Omit unavailable optional icons. Never replace missing icons with arbitrary raster or emoji.
- Default stack: photograph z=0, overlay z=1, independent imagery above, text above its intended backing. Permit validated overlap variations when required.

## 6. Providers, deadlines and security

Implement an internal image-provider adapter that receives prompt, actual source bytes/MIME, transparent-background requirement, canvas and operationId, and returns real bytes/MIME/provider request identity. Translate to the actual provider contract after inspecting its code/documentation. These are internal interface requirements, not claims about an existing HTTP endpoint. Never assume an invented transparency flag is supported. Unsupported alpha editing returns TRANSPARENCY_UNSUPPORTED.

Keep separately configurable image, concept-analysis and review models. Do not hardcode assumed available model IDs. A text agent does not create raster images by writing HTML. Use image models for pixels, vision-capable text models for structured analysis/review, code for assembly and Chromium for rendering.

Limit workflow duration initially to 30 minutes. Bound task/provider deadlines and retries. For asynchronous providers use Step Functions Wait/poll states instead of busy-waiting inside Lambda. Persist provider operation IDs. If a timeout leaves a paid generation outcome unknown, reconcile through provider idempotency/status when supported; otherwise fail PROVIDER_OUTCOME_UNKNOWN rather than blindly paying twice.

Owner scope comes from authenticated caller context, not a model-supplied tenant ID. Authorize status/artifact access. Fetch references only from configured HTTPS origins/paths with bounded bytes and controlled redirects. Read internal generated assets via SDK using validated job-owned keys, not arbitrary S3 URLs. Render model-generated content with restricted network access and no executable scripts.

Use least-privilege IAM: API gets job/idempotency access and StartExecution for this workflow; stage gets job-scoped artifacts, configured model/provider secrets and stable-media publishing; workflow can invoke stage; application can read authorized results. Scope model profiles/destination permissions to actual configuration. Do not log secrets, full base64, signed URL query strings or routine customer contact content. Log jobId/stage/attempt/duration/error/provider request ID.

## 7. Required Bedrock Agent integration

Implement a required Bedrock Action Group adapter in broadcust-layered-design-api. Keep the core processing logic independently callable; independence does not make Bedrock integration optional for this delivery.

### 7.1 Action functions

Expose runLayeredDesign(requestJson:string) and getLayeredDesignStatus(jobId:string). requestJson serializes the public start request, INCLUDING designFreedom when supplied. Descriptions must stay under 200 characters. The trusted caller supplies a stable requestId. The agent forwards the caller's designFreedom unchanged; the service applies default 0 only if omitted. Do not have the agent choose a different value based on aesthetic preference.

### 7.2 Event decoding

Read event.function and event.parameters name/value pairs. Validate the function name, required parameters, parsed requestJson and all public request fields. Reject malformed JSON. Both Bedrock and direct paths call the same service validation/authorization logic.

### 7.3 Response envelope

```js
function respondToBedrock(event, result) {
  const functionResponse = {
    responseBody: { TEXT: { body: JSON.stringify(result) } }
  };
  if (result.success === false) functionResponse.responseState = 'FAILURE';
  return {
    messageVersion: '1.0',
    response: {
      actionGroup: event.actionGroup,
      function: event.function,
      functionResponse
    },
    sessionAttributes: event.sessionAttributes ?? {},
    promptSessionAttributes: event.promptSessionAttributes ?? {}
  };
}
```

Preserve incoming action/function names and session attributes. Never use an API Gateway statusCode/body wrapper for Bedrock events.

### 7.4 Starting a job

Return accepted:true/jobId/QUEUED after durable acceptance and workflow-start acknowledgement, wrapped as above. Do not return finished HTML or say the design is complete. Retain the effective designFreedom and policy with the job.

### 7.5 Final artifacts and direct backend retrieval

Return compact status and real artifact pointers only. Artifact paths are illustrative, not mandatory literal filenames. A single existing result.json containing HTML, layers, assets and QA is sufficient; no final/ folder, design.html, layers.json or qa.json is required solely to match an example. Never return pointers to files that do not exist.

```json
{
  "success": true,
  "jobId": "generated-job-id",
  "status": "SUCCEEDED",
  "result": {
    "resultKey": "layered-design/generated-job-id/result.json",
    "previewUrl": "https://media.example.com/preview.png",
    "designFreedom": 10,
    "htmlHash": "sha256-value",
    "assetManifestHash": "sha256-value"
  }
}
```

Use the actual persisted result key, not a guessed path. Optional previewKey/qaKey may be included only for existing S3 objects. previewKey always means an S3 key; public Cloudinary addresses belong in previewUrl. Omit unavailable fields. Do not include full HTML/base64 in Bedrock responses.

The authenticated application backend obtains the complete result through a DIRECT service call, not through the agent. This path returns the full result described in section 2.5. It does not require granting the application direct S3 bucket access. Implement authentication and ownership checks; invocation mode alone is not authorization. No backend role/account identity is assumed.

QA may be embedded in result.json or an existing review artifact. Include passed, blocking issues, suggestions, actual render-check outcomes, reviewed render identity, htmlHash, assetManifestHash, effective designFreedom, policyVersion and reference-review evidence. Missing checks cannot be marked passed.

Temporary job artifacts may expire after 30 days. Before deletion, a saved customer design's HTML, layer map, preview and ALL referenced images must be persisted to durable storage and verified. If that persistence path does not exist, exclude saved final artifacts and their dependencies from automatic deletion. A stable URL pointing to an object deleted after 30 days is not durable delivery. Do not enable a broad lifecycle rule that removes saved customer designs.

### 7.6 Asynchronous completion

The application stores accepted jobId and reads progress through its background-job mechanism. It retrieves and delivers final HTML only after SUCCEEDED, and propagates stage errors after FAILED. Do not implement a prolonged polling loop inside the agent invocation. Background completion does not automatically resume the original agent turn.

### 7.7 Direct invocation

For trusted direct tests accept `{operation:"runLayeredDesign", request:{...}}` and `{operation:"getLayeredDesignStatus", jobId:"..."}`; return the service result without the Bedrock envelope. Require an explicit operation. Resolve owner identity through the trusted deployment context, never through a model-supplied tenantId.

### 7.8 Required configuration and permissions

Deliver actual Action Group function definitions, Lambda handler/adapter, scoped Bedrock invoke permission for the configured source account/agent, trusted session-to-job ownership mapping and tests. Do not hardcode a specific agent ID in core processing. Test compact Bedrock results and full direct results separately.

> **Requester clarification on 7.5 (resolved).** The `final/` key paths above are
> illustrative, not required filenames. The service keeps its `<stage>/<attempt>`
> storage and the combined `result.json`, and returns references to artifacts it
> actually produces. See `docs/CONTRACTS.md`, "Requester decisions on 7.5".

## 8. Implementation files and configuration

```text
layered-design/
  prototype.mjs
  pipeline.mjs
  stages/
  storage/local.mjs
  storage/s3.mjs
  examples/request.json
  api.mjs
  stage.mjs
  schemas.mjs
  reference-policy.mjs
  jobs.mjs
  artifacts.mjs
  assemble-html.mjs
  render.mjs
  validate-assets.mjs
  providers/image.mjs
  providers/vision.mjs
  prompts/analyze-reference.txt
  prompts/concept.txt
  prompts/review-concept.txt
  prompts/extract-scene.txt
  prompts/clean-photo.txt
  prompts/decoration-overlay.txt
  prompts/review.txt
  workflow.asl.json
  Dockerfile
  package.json
  tests/
  README.md
```

Deliver prototype.mjs, pipeline.mjs, shared stages, local storage, schemas, real provider integrations, prompts, assembler and renderer first. Add production entry points, storage adapters, workflow and IaC in Phase C. Document the local command, required configuration, artifact paths and acceptance status in README.md.

Provide complete prompts, validators, implementations and IaC for the final delivery. Configure job table, artifact bucket, workflow ARN, authorized image origins, image provider, vision model IDs, font/style paths and stable-media adapter. Validate required configuration at startup/preflight. Credentials belong in the deployment's secret mechanism, never source code.

Caller integration must store accepted jobId, read status with bounded backoff, deliver the completed HTML/result only on SUCCEEDED, and expose stage errors on FAILED. No prose parsing to infer completion. The standalone service does not assume an existing client's result contract; implement an explicit adapter if needed.

## 9. Acceptance tests and required deliverables

Apply the phase gates in section 0. First deliver and evaluate one real local design, then editor compatibility and variation, then production acceptance. The numbered start/status tests below apply to the production service; local prototype runs must not fabricate asynchronous acceptance or cloud deployment evidence.

Unit tests must cover request validation, response shapes, required Bedrock envelope, idempotency/conflicts, ownership, scene validation, escaped HTML, unique layer classes, independent overlay, opaque/empty alpha rejection, final hash invalidation, exhausted repair counts and proper failures.

Explicitly enabled integration tests must:
1. Start a real job and receive jobId promptly.
2. Obtain progress then terminal status through the public contract.
3. Generate a real concept, clean photograph, true-alpha decoration image and editable HTML.
4. Hide/move/replace decorations without modifying photograph or text.
5. Verify required text/contact accuracy, no baked final words, loaded assets/fonts, no text overflow, native/feed render and matching final hashes.
6. Exercise unsupported transparency, image timeout, invalid vision JSON, renderer failure and unavailable media delivery. No fake success or unreviewed fallback.

A useful acceptance brief is an Italy winter travel offer: Christmas/Hanukkah, Tuscany/Umbria/Southern Italy, flights/car/hotels/custom itinerary/personal service, with supplied business contacts and no invented price/date. Test Hebrew RTL as well as Latin contacts. Source-template adherence is evaluated according to the effective designFreedom policy; pixel identity is never promised. Final HTML must faithfully reconstruct the approved concept at every setting.

Additional required designFreedom tests:
- Omitted field defaults to 0; explicit 0 remains 0; explicit 50 remains 50. Omitted and explicit 0 normalize to the same canonical request hash. Values 0, 1, 25, 26, 50, 51, 75, 76 and 100 map to the correct modes. Reject -1, 101, null, "10", 10.5 and booleans.
- Bedrock and direct calls preserve the same effective value. Changing it under an existing requestId yields an idempotency conflict.
- Prompt generation includes explicit preservation/change rules; no low-freedom prompt unconditionally requests a new layout.
- Use the SAME brief/reference in separately enabled integration jobs at 0, 25, 50, 75 and 100. Record policy evidence and documented exceptions. Show close preservation at low settings and permission for broader recomposition at high settings; do not claim statistically calibrated percentages or require random outputs to differ monotonically.
- A strict-mode concept with unjustified anchor/panel displacement fails reference review; justified brand/contact replacements do not fail.
- A final HTML render that drifts from the approved concept fails even at 100. Regenerating the concept invalidates its scene/assets/render/QA.
- Test bounded concept-repair exhaustion, REFERENCE_CONSTRAINT_CONFLICT, combined result.json retrieval, real artifact pointers, previewKey/previewUrl distinction, and saved-design retention beyond temporary job cleanup.

Deliver complete Lambda code, IaC, workflow, provider integrations, full prompt files, build/deploy/configuration commands, example payloads, test outputs and a precise list of unavailable dependencies. Do not deploy to production automatically. Do not call implementation complete when only the gateway works or stages are mocked. Mock tests alone do not establish visual quality, true transparency or end-to-end readiness.
