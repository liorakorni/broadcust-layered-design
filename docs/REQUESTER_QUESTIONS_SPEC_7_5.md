# Questions for the requester: spec section 7.5 final artifacts

**Subject:** LayeredDesign spec 7.5: are the `final/` artifact paths literal?

Hi,

I'm implementing the Bedrock integration (section 7) and need to confirm one point in 7.5 before I build it.

The example status response points to four objects:

```
layered-design/<jobId>/final/design.html
layered-design/<jobId>/final/preview.png
layered-design/<jobId>/final/layers.json
layered-design/<jobId>/final/qa.json
```

Today the service doesn't produce these.

- It stores pipeline artifacts under `layered-design/<jobId>/<stage>/<attempt>/<name>`.
- At the end it writes one `result.json` that holds the HTML, the layer map and the asset list together.
- There is no `final/` folder.
- `layers.json` and `qa.json` don't exist as separate files.

## 1. Are those four paths literal, or just an illustration?

- [ ] **(a) Literal.** I'll write a stable copy at exactly those names when a job succeeds, so your backend can rely on them.
- [ ] **(b) Illustrative.** The point is "return pointers, not the HTML", and I can return whatever keys we already have.

My default is (a), because it's the only reading where the keys in the example point to real files.

## 2. How does your backend read these files?

- [ ] **(a) Directly from our S3 bucket.** The bucket is private, so we'd need to grant your backend's role read access. Please tell me the role or account: `______`
- [ ] **(b) Through the status call.** That returns the full HTML in the response, which the agent response must not do, so your backend would need to call us directly rather than through the agent.

## 3. What should `previewKey` be?

- [ ] **(a) The S3 key** of the rendered image. It needs AWS credentials to fetch.
- [ ] **(b) The public Cloudinary URL** we already publish for the preview. It works without credentials.

## 4. How long do you need the final files to stay available?

We currently delete job files after 30 days.

- [ ] 30 days is enough
- [ ] Longer: `______` days

## 5. What should `qa.json` contain?

I plan to use our review verdict (approved or not, blocking issues, suggestions), which already includes the render checks.

- [ ] That's fine
- [ ] A different format is expected: `______`

---

If you'd rather answer in one line, "1a, 2a with role X, 3a, 30 days is fine, 5 as proposed" is enough for me to proceed.

Thanks,
[name]

---

## Why each question matters (internal, delete before sending)

- **Q1** decides whether the design changes. If (b), Step 1 of the plan shrinks, but `layers.json` and `qa.json` still have to be written somewhere because nothing produces them as standalone objects today.
- **Q2** and **Q3** are where a wrong guess leaves the backend with something it can't fetch. If only one question gets answered, make it Q2.
- **Q4** decides whether the existing 30-day lifecycle rule in `serverless.yml` needs a separate, longer rule for the `final/` prefix.
- **Q5** decides whether `qa.json` is just the existing `review.json` verdict or needs a new shape.

---

## Answers

1. **Paths: (b) illustrative.** The `final/` paths were examples. Keep the stage/attempt structure and the combined `result.json`; return a `resultKey`. No `htmlKey`, `layersKey` or `qaKey` unless those separate objects exist. QA may be embedded in `result.json` or referenced through an existing review artifact.
2. **Backend access: (b) authenticated direct service call.** Two modes: a compact Bedrock response without full HTML, and an authenticated direct call returning the complete result (HTML, layers, assets). No backend IAM role ARN has been supplied. Authentication and job ownership checks stay mandatory.
3. **Preview: key and URL are distinct.** `previewKey` is an actual S3 object key; the Cloudinary URL is returned as `previewUrl`. Omit unavailable fields.
4. **Retention: 30 days for temporary artifacts.** Saved customer designs must remain usable beyond that. Persist the final HTML, layer map, preview and every asset the HTML references in durable storage; if that is not implemented, exclude saved designs and their dependencies from automatic deletion.
5. **QA: the proposed verdict, with version identification.** Include `passed`, blocking issues and suggestions, render-check results, the reviewed render's identifier, `htmlHash` and `assetManifestHash`. Approval requires passing technical checks and zero blocking issues. Missing checks are reported explicitly, never treated as passing.

Direction: return references to artifacts the service actually produces; do not create duplicate files to match the example paths.

### How each was implemented

See `docs/CONTRACTS.md`, "Requester decisions on 7.5" and "Retention tags". Retention uses the "exclude from automatic deletion" fallback; a durable copy is not built (`docs/GAPS.md` entry 10).
