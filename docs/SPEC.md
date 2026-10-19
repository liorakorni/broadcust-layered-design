# Cursor implementation instructions: standalone LayeredDesign service

## 1. Purpose and responsibility

Implement an AWS service named `LayeredDesign` that receives a marketing brief, an inspiration image, canvas dimensions, optional branding and supplied assets. It produces a polished marketing design as editable HTML with independently replaceable visual layers.

This is a standalone specification. The implementation must not depend on previous conversations, undocumented Lambda functions, an existing agent, or older software versions. If integrating into an existing repository, inspect its actual interfaces and implement explicit adapters. Do not assume interfaces exist merely because this document describes them.

The service owns the complete process: visual concept creation, concept analysis, image separation, editable text placement, deterministic HTML assembly, browser rendering, visual review and bounded correction.

The inspiration image guides visual quality, hierarchy, atmosphere, depth and typography. It is not a locked template. The supplied brief, audience and brand determine the new composition. Do not force target content into the source image's boxes, section count or proportions.

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
- Default schemaVersion to "1.0" and canvas to 1080x1080.
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

The authenticated application status interface may return the full result above. Compact transports such as Bedrock action responses return pointers instead of full HTML; the application fetches the full result through the authenticated service. Store full content in S3, not DynamoDB.

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

## 3. AWS architecture

Implement these resources:

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

PREFLIGHT -> PREPARE_COPY -> CONCEPT -> EXTRACT_SCENE -> CLEAN_PHOTO -> DECORATION_OVERLAY -> INDEPENDENT_ASSETS -> VALIDATE_ASSETS -> ASSEMBLE -> RENDER -> REVIEW -> FINALIZE.

Dispatch the stage Lambda using `{jobId, stage, attempt}`; independent asset tasks also receive assetId. A bounded Map may process at most three independent generated assets, with concurrency at most two.

Each stage loads authorized inputs, performs its operation, validates its output, stores immutable output artifacts and returns small pointers. Persist output before advancing. Reuse completed work when input hashes match. No stub may return successful completion.

### PREFLIGHT

Validate request, authorized asset origins, configured image editing, true-alpha capability, vision model, renderer, fonts and stable media publishing. Ordinary preflight does not perform paid image generation; explicit integration tests establish actual provider capabilities. Reject missing essential capabilities clearly.

### PREPARE_COPY

Create a concise authoritative copy manifest grounded in the brief, with stable content keys. Preserve requiredTexts and supplied factual details. Keep phone/email as distinct items. Generated concept text or OCR is never the source of truth. Do not add services, prices or claims to fill space.

### CONCEPT

Send actual inspiration pixels, brief, approved copy, brand, supplied assets and canvas to the image creation/edit model. Request a fresh premium marketing composition inspired by relevant source visual qualities. Internal concept imagery MAY include target text as a layout guide. It is not the customer deliverable. Persist the exact concept bytes as immutable input for separation.

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

Inspect contrasting-background composites with vision for checkerboard patterns, photographic leakage, leftover text and missing artwork. Alpha statistics alone are insufficient. Regeneration can change alignment: adjust validated scene geometry and crops when actual separated assets require it.

### ASSEMBLE

Build HTML deterministically from the validated scene, authoritative copy and resolved asset manifest. Use only allowlisted style fields, never unrestricted model HTML/CSS. Escape plain text and URL attributes. Resolve stable versioned media URLs before final assembly/review. If stable delivery cannot be provided, fail finalization explicitly instead of persisting expiring customer designs.

### RENDER

Render exact HTML with actual editor styles/fonts; wait for document.fonts.ready and successful image decode. Capture canvas at native size and a 360px-wide scaled feed view. Check layer bounds, unique IDs, loaded images/fonts and actual wrapped text dimensions. Do not conceal overflow with clipping. Missing required fonts/images are blocking errors.

### REVIEW

Send actual concept pixels, current native/feed render pixels, authoritative copy and asset-validation evidence to vision. Evaluate impact, hierarchy, readability, factual accuracy, collisions, duplication, separation and reconstruction of the target concept. Do not require source-template layout similarity.

Require strict JSON: `{passed, blockingIssues:[{code,layerId,assetId,message}], suggestions:[]}`. Optional IDs are null. Backend approval requires passing technical checks and zero blocking issues; a model saying passed:true does not override failures.

### CORRECTION AND FINALIZE

Allow at most two correction rounds after initial render. Use validated scene patches for placement/typography and targeted asset edits for raster defects. Every changed asset or HTML version requires downstream validation, reassembly, rendering and review. Bound targeted overlay repairs to two per job, not two per task retry.

Hash exact final HTML bytes and a canonical ordered asset manifest containing asset IDs, immutable keys/versions and byte hashes. Store QA with matching concept/render identities, hashes, model identities and attempt. Mark SUCCEEDED only after all final artifacts are durable and final QA matches. Otherwise persist a terminal stage/code/message error.

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

## 7. Required Bedrock Agent Integration

The service must support invocation by an Amazon Bedrock Agent through an Action Group. This integration is a mandatory part of the implementation.

Implement the integration in the broadcust-layered-design-api Lambda. Keep the design-processing logic separate from the Bedrock event and response handling.

### 7.1 Action Group Functions

Expose these two functions:

| Function | Required parameter | Purpose |
| --- | --- | --- |
| runLayeredDesign | requestJson: string | Start an asynchronous design job. |
| getLayeredDesignStatus | jobId: string | Retrieve job progress or completion status. |

requestJson contains the serialized start request defined in this specification.

### 7.2 Incoming Bedrock Events

Read the function name from event.function.

Read parameters from event.parameters, where each parameter contains name and value.

Example start event:

```json
{
  "messageVersion": "1.0",
  "actionGroup": "LayeredDesign",
  "function": "runLayeredDesign",
  "parameters": [
    {
      "name": "requestJson",
      "type": "string",
      "value": "{\"requestId\":\"example-001\",\"brief\":{\"post\":\"Marketing content\"},\"referenceImageUrl\":\"https://example.com/reference.png\"}"
    }
  ],
  "sessionAttributes": {},
  "promptSessionAttributes": {}
}
```

Parse requestJson as JSON and validate it against the service's start-request schema. Reject malformed JSON, missing parameters and unsupported function names.

The example URL is illustrative and must be replaced with an actual authorized image URL during testing.

### 7.3 Bedrock Response Format

Return results using this envelope:

```js
function respondToBedrock(event, result) {
  const functionResponse = {
    responseBody: {
      TEXT: {
        body: JSON.stringify(result)
      }
    }
  };

  if (result.success === false) {
    functionResponse.responseState = "FAILURE";
  }

  return {
    messageVersion: "1.0",
    response: {
      actionGroup: event.actionGroup,
      function: event.function,
      functionResponse
    },
    sessionAttributes: event.sessionAttributes ?? {},
    promptSessionAttributes:
      event.promptSessionAttributes ?? {}
  };
}
```

Preserve the incoming action-group name, function name and session attributes. Do not use an API Gateway statusCode/body response wrapper for Bedrock invocations.

### 7.4 Starting a Job

runLayeredDesign must return promptly after durable job acceptance and workflow-start acknowledgement:

```json
{
  "success": true,
  "accepted": true,
  "jobId": "generated-job-id",
  "status": "QUEUED"
}
```

Wrap this result in the Bedrock response envelope.

This response confirms acceptance only. Do not wait for design completion, return final HTML or report that the design is finished.

### 7.5 Reading Job Status

getLayeredDesignStatus receives the jobId returned by the start operation.

While processing, return the current status and stage.

On completion, return a compact result containing authorized artifact references and validation hashes:

```json
{
  "success": true,
  "jobId": "generated-job-id",
  "status": "SUCCEEDED",
  "result": {
    "htmlKey": "layered-design/generated-job-id/final/design.html",
    "previewKey": "layered-design/generated-job-id/final/preview.png",
    "layersKey": "layered-design/generated-job-id/final/layers.json",
    "qaKey": "layered-design/generated-job-id/final/qa.json",
    "htmlHash": "sha256-value",
    "assetManifestHash": "sha256-value"
  }
}
```

Do not include full HTML, Base64 images or large documents in Bedrock action responses. The authenticated application backend retrieves the final artifacts and delivers the complete result to the client.

A failed design job returns success: true with status: "FAILED" and its stage error when the status lookup itself succeeds. Invalid or unauthorized requests return success: false.

### 7.6 Asynchronous Completion

The application backend must retain the returned jobId and obtain the terminal job status through its background-job mechanism.

Do not implement a prolonged polling loop inside the agent invocation. Bedrock does not automatically resume the original agent turn when the background design job finishes.

The integration is complete only when the application can retrieve and deliver the reviewed HTML after SUCCEEDED, and report the stage error after FAILED.

### 7.7 Direct Invocation for Testing

Also support explicit direct Lambda invocations:

```json
{
  "operation": "runLayeredDesign",
  "request": {
    "requestId": "example-001",
    "brief": {
      "post": "Marketing content"
    },
    "referenceImageUrl": "https://example.com/reference.png"
  }
}
```

```json
{
  "operation": "getLayeredDesignStatus",
  "jobId": "generated-job-id"
}
```

Direct invocations return the operation result without the Bedrock envelope. Both invocation paths must use the same validation, authorization and service logic.

### 7.8 Configuration and Permissions

Provide:

- The Action Group function definitions.
- The Lambda handler and its Bedrock event adapter.
- Lambda invocation permission for bedrock.amazonaws.com, restricted to the configured AWS account and agent ARN.
- A trusted mechanism for associating agent sessions with job ownership.
- Tests for both functions, response envelopes, malformed requests and unauthorized job access.

Keep function and parameter descriptions within the target configuration's 200-character limit. Do not hardcode a particular agent ID into the core service logic.

> **Requester clarification on 7.5 (resolved).** The `final/` key paths above are
> illustrative, not required filenames. The service keeps its `<stage>/<attempt>`
> storage and the combined `result.json`, and returns references to artifacts it
> actually produces. See `docs/CONTRACTS.md`, "Requester decisions on 7.5".

## 8. Implementation files and configuration

```text
layered-design/
  api.mjs
  stage.mjs
  schemas.mjs
  jobs.mjs
  artifacts.mjs
  assemble-html.mjs
  render.mjs
  validate-assets.mjs
  providers/image.mjs
  providers/vision.mjs
  prompts/concept.txt
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

Provide complete prompts, validators, implementations and IaC. Configure job table, artifact bucket, workflow ARN, authorized image origins, image provider, vision model IDs, font/style paths and stable-media adapter. Validate required configuration at startup/preflight. Credentials belong in the deployment's secret mechanism, never source code.

Caller integration must store accepted jobId, read status with bounded backoff, deliver the completed HTML/result only on SUCCEEDED, and expose stage errors on FAILED. No prose parsing to infer completion. The standalone service does not assume an existing client's result contract; implement an explicit adapter if needed.

## 9. Acceptance tests and required deliverables

Unit tests must cover request validation, response shapes, optional Bedrock envelope, idempotency/conflicts, ownership, scene validation, escaped HTML, unique layer classes, independent overlay, opaque/empty alpha rejection, final hash invalidation, exhausted repair counts and proper failures.

Explicitly enabled integration tests must:
1. Start a real job and receive jobId promptly.
2. Obtain progress then terminal status through the public contract.
3. Generate a real concept, clean photograph, true-alpha decoration image and editable HTML.
4. Hide/move/replace decorations without modifying photograph or text.
5. Verify required text/contact accuracy, no baked final words, loaded assets/fonts, no text overflow, native/feed render and matching final hashes.
6. Exercise unsupported transparency, image timeout, invalid vision JSON, renderer failure and unavailable media delivery. No fake success or unreviewed fallback.

A useful acceptance brief is an Italy winter travel offer: Christmas/Hanukkah, Tuscany/Umbria/Southern Italy, flights/car/hotels/custom itinerary/personal service, with supplied business contacts and no invented price/date. Test Hebrew RTL as well as Latin contacts. Exact source-template resemblance is not the acceptance criterion.

Deliver complete Lambda code, IaC, workflow, provider integrations, full prompt files, build/deploy/configuration commands, example payloads, test outputs and a precise list of unavailable dependencies. Do not deploy to production automatically. Do not call implementation complete when only the gateway works or stages are mocked. Mock tests alone do not establish visual quality, true transparency or end-to-end readiness.
