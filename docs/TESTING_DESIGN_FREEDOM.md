# Testing `designFreedom`

A guide for the requester. It covers how to exercise the new `designFreedom`
setting on the deployed `dev` service, what a good result looks like, and what
failures are expected.

For field reference see [USAGE.md](USAGE.md); for the design see
[SPEC.md](SPEC.md) section 2.7.

## What you are testing

`designFreedom` (integer 0-100, default 0) controls how closely the new design
must follow the reference image. The service turns it into written constraints,
analyses the reference, generates a concept, reviews the concept against those
constraints, and only then separates layers and builds the HTML.

| Value | Mode | Expectation |
|---|---|---|
| 0 | `strict-adherence` | Same composition as the reference. |
| 1-25 | `close-adaptation` | Same recognizable layout, local adjustments. |
| 26-50 | `guided-adaptation` | Selected qualities kept, regions may be reorganized. |
| 51-75 | `broad-inspiration` | Major restructuring allowed. |
| 76-100 | `free-composition` | New structure allowed. |

Two relationships are judged separately:

1. **Reference to concept**: depends on `designFreedom`.
2. **Approved concept to final HTML**: must be faithful at **every** value,
   including 100.

## Prerequisites

You need three things.

| Need | Detail |
|---|---|
| AWS access | An IAM principal in account `816069163425`, region `us-east-1`, allowed `lambda:InvokeFunction` on `broadcust-layered-design-dev-api`. **No role is granted this today** (GAPS entry 9), so ask the service owner to add yours or to run the tests with you. |
| A reference image | A real image on an authorized origin (`https://res.cloudinary.com/...`). Pick one that looks like a marketing design and fits your brief. A plain photograph makes strict modes fail (see "Expected failures"). |
| Time and budget | Each job makes paid OpenAI image and vision calls and takes about 4 to 10 minutes. A job with concept repairs costs more. |

**You do not need `bedrock_agent_arn`, the artifact bucket, or a Bedrock agent**
for any test below. See "Do I need a Bedrock agent?" at the end.

## Test 1: start a job

Save as `request.json`. Change `referenceImageUrl` to your own image.

```json
{
  "operation": "runLayeredDesign",
  "trustedCallerContext": { "ownerScope": "requester-test" },
  "request": {
    "schemaVersion": "1.0",
    "requestId": "df-test-0-001",
    "brief": {
      "post": "חופשת חורף באיטליה - חג המולד וחנוכה בטוסקנה. טיסות, רכב, מלונות ושירות אישי.",
      "instructions": "Warm, premium, winter light. Text-only branding: no logo or emblem.",
      "requiredTexts": ["חופשת חורף באיטליה"],
      "business": {
        "name": "Dolce Travel",
        "phone": "03-1234567",
        "email": "info@dolce.example",
        "website": null
      }
    },
    "canvas": { "width": 1080, "height": 1080 },
    "referenceImageUrl": "https://res.cloudinary.com/<your-cloud>/image/upload/<your-reference>.png",
    "brand": { "colors": ["#0B2545"], "fontFamilies": ["Heebo"] },
    "suppliedAssets": [],
    "designFreedom": 0
  }
}
```

Invoke it:

```bash
aws lambda invoke --function-name broadcust-layered-design-dev-api \
  --region us-east-1 --cli-binary-format raw-in-base64-out \
  --payload file://request.json start.json && cat start.json
```

Expected: `{"success": true, "accepted": true, "jobId": "…", "status": "QUEUED"}`.
This only means the job was accepted, not that a design exists.

`ownerScope` is the identity that owns the job. Use the **same** value on every
later call, or the job will look like it does not exist.

## Test 2: poll until finished

```bash
JOB=<jobId from start.json>
cat > status.json <<EOF
{"operation":"getLayeredDesignStatus","jobId":"$JOB",
 "trustedCallerContext":{"ownerScope":"requester-test"}}
EOF
while true; do
  aws lambda invoke --function-name broadcust-layered-design-dev-api \
    --region us-east-1 --cli-binary-format raw-in-base64-out \
    --payload file://status.json out.json >/dev/null
  python3 -c "import json;d=json.load(open('out.json'));print(d.get('status'),d.get('stage'))"
  grep -qE '"status": ?"(SUCCEEDED|FAILED)"' out.json && break
  sleep 15
done
```

The `stage` is real progress, not a percentage. A normal run passes through:

`PREFLIGHT, PREPARE_COPY, ANALYZE_REFERENCE, CONCEPT, REVIEW_CONCEPT, EXTRACT_SCENE, CLEAN_PHOTO, DECORATION_OVERLAY, VALIDATE_ASSETS, ASSEMBLE, RENDER, REVIEW`

If the concept review rejects the concept you will see `CONCEPT` and
`REVIEW_CONCEPT` repeat, at most twice. That is the concept-repair loop working.

## Test 3: what to check in a SUCCEEDED result

`out.json` is the full result. Check each of these:

| Check | Expect |
|---|---|
| `result.designFreedom` | The value you sent (0 when omitted). |
| `result.policyVersion` | `"1.0"` |
| `result.qa.designFreedom` | Same value as above. |
| `result.qa.conceptReview.mode` | The band for your value (table above). |
| `result.qa.conceptReview.passed` | `true` |
| `result.qa.conceptReview.documentedExceptions` | The adaptations the reviewer accepted, for example "business name replaced". |
| `result.qa.conceptRounds` | `0` to `2`: how many concept repairs were needed. |
| `result.qa.passed` | `true`, with every entry in `qa.renderChecks` `passed`. |
| `result.previewUrl` | Opens a PNG of the design. Compare it with your reference. |
| `result.html`, `result.layerMap`, `result.assets` | Present. |

Open `previewUrl` and the reference side by side. Judge the first relationship by
eye against the table: strict and close should look like the reference with new
content; 100 may look entirely different. Judge the second one too: the HTML must
look like the **approved concept**, not like a generic layout.

## Test 4: the value matrix

Run the **same brief and reference** at 0, 25, 50, 75 and 100, with a distinct
`requestId` each time (`df-test-0-001`, `df-test-25-001`, and so on). For each,
record the mode, `conceptRounds`, `documentedExceptions`, the preview, and your
own pass/fail reason.

Do not expect the outputs to get steadily more different as the number rises. The
setting is a qualitative band, outputs are generated, and neighbouring values
share a band (1 and 25 behave the same way by design). Also do not expect a result
to be "N% different"; there is no such measure.

## Test 5: validation and idempotency

Rejected values answer immediately and cost nothing, because the request is
refused before any paid work. Accepted values start a real, paid job, so use them
sparingly. Change only `designFreedom` in `request.json` and use a fresh
`requestId` for each case.

| Send | Expect |
|---|---|
| field omitted | accepted; result reports `0` |
| `0` | accepted, same job as omitted if the `requestId` and content match |
| `-1`, `101` | `success: false`, `INVALID_REQUEST` |
| `null`, `"10"`, `10.5`, `true`, `false` | `success: false`, `INVALID_REQUEST` |

Idempotency: start a job with `designFreedom: 10`, then send the same `requestId`
with `designFreedom: 60`. Expect `IDEMPOTENCY_CONFLICT`. Sending the identical
request again returns the original `jobId` and starts no second paid run.

## Test 6: the Bedrock response shape, without a Bedrock agent

The same Lambda answers Bedrock-shaped events. You can send one yourself to see
exactly what an agent would receive. Use a finished job's `jobId`.

```bash
cat > agent_status.json <<EOF
{
  "messageVersion": "1.0",
  "actionGroup": "LayeredDesign",
  "function": "getLayeredDesignStatus",
  "parameters": [{"name": "jobId", "type": "string", "value": "$JOB"}],
  "sessionAttributes": {"ownerScope": "requester-test"},
  "promptSessionAttributes": {}
}
EOF
aws lambda invoke --function-name broadcust-layered-design-dev-api \
  --region us-east-1 --cli-binary-format raw-in-base64-out \
  --payload file://agent_status.json agent_out.json && cat agent_out.json
```

Expect the envelope `response.functionResponse.responseBody.TEXT.body`, a JSON
string. Parsed, `result` contains **only**:

`resultKey`, `previewKey`, `previewUrl`, `qaKey`, `designFreedom`, `htmlHash`,
`assetManifestHash`

- `designFreedom` must be present, **including when it is 0**.
- There must be no HTML, layer map, asset list, or `htmlKey`.
- A different `ownerScope` in `sessionAttributes` must answer `JOB_NOT_FOUND` with
  `responseState: "FAILURE"`.
- Omitting `ownerScope` entirely must answer `UNAUTHORIZED`.

To start a job through this path, use `function: "runLayeredDesign"` with one
parameter `requestJson` whose value is the `request` object from `request.json`,
serialized as a string. `designFreedom` goes inside it.

## Expected failures (these are not necessarily bugs)

A job ending `FAILED` is a valid outcome. Read `error.stage` and `error.code`.

| Code and stage | Meaning | What to do |
|---|---|---|
| `REFERENCE_ADHERENCE_FAILED`, `REVIEW_CONCEPT` | The concept still broke the policy after 2 concept repairs. Common at 0 to 25 with a reference that does not resemble the brief (for example a nature photo). | Use a closer reference, or raise `designFreedom` yourself. The service never raises it. |
| `REFERENCE_CONSTRAINT_CONFLICT`, `REVIEW_CONCEPT` | Required text cannot fit the preserved layout. | Shorten copy, change reference, or raise `designFreedom`. |
| `INVALID_TRANSPARENCY`, `VALIDATE_ASSETS` | Layer separation failed after 2 overlay repairs, including when the separated layers do not reconstruct the approved concept (for example badges lost). Known limit, GAPS entry 7. | Retry with a new `requestId`; report if it repeats. |
| `CORRECTION_EXHAUSTED`, `REVIEW` | The final HTML still differed from the approved concept after 2 correction rounds. | Retry; report the message. |
| `INVALID_SCENE`, `PROVIDER_*` | Transient model or provider trouble. | Retry with a new `requestId`. |

A test counts as a **feature failure** only if:

- the value reported in the result differs from what was sent,
- a rejected value in Test 5 is accepted (or the reverse),
- the compact response drops `designFreedom: 0`,
- a job is stuck `RUNNING` for more than about 30 minutes (the workflow timeout),
- a strict-mode concept visibly ignores the reference and is approved anyway, or
- the final HTML drifts from the approved concept at any value and is still
  approved.

Report each with the `jobId`, `designFreedom`, the preview URL and the reference.

## Do I need a Bedrock agent, and `bedrock_agent_arn`?

**No, not for any test above.**

- Direct invocation (Tests 1-5) runs the whole pipeline with no agent.
- Test 6 sends a Bedrock-shaped event straight to the Lambda, so the compact
  response and ownership rules are verified without an agent.

`bedrock_agent_arn` only matters when a **real** Bedrock agent must call the
Lambda. It is the agent's ARN in `config.<stage>.json`. On deploy it creates a
Lambda permission letting `bedrock.amazonaws.com` invoke the function, limited to
this account and that one agent. Until it is set, no permission exists and a real
agent would be refused. The agent also needs the two functions from
[`bedrock/action-group.json`](../bedrock/action-group.json) added to its Action
Group.

For a full agent run, you additionally need:

1. the agent ARN supplied (`bedrock_agent_arn`) and the service redeployed,
2. the Action Group configured with those two functions, and
3. the application backend that calls `InvokeAgent` to set the `ownerScope`
   session attribute. **`broadcust-bedrock-api` does not do this yet**; without it
   every agent call fails with `UNAUTHORIZED`, by design.

The agent must forward the caller's `designFreedom` unchanged inside
`requestJson`, and must not pick a value itself.
