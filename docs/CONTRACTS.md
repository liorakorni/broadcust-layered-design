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
| `CLEAN_PHOTO` | `photo.png` | Background photograph, no text or graphics |
| `DECORATION_OVERLAY` | `overlay.png` | RGBA decoration layer |
| `INDEPENDENT_ASSETS` | `assets/<assetId>.png` | One independent asset |
| `VALIDATE_ASSETS` | `validation.json` | Per-asset measurements and verdicts |
| `ASSEMBLE` | `design.html` | Exact final HTML bytes |
| `RENDER` | `native.png`, `feed.png` | Native-size and 360px-wide renders |
| `REVIEW` | `review.json` | Strict-JSON review verdict |
| `FINALIZE` | `result.json` | Completed result per SPEC section 2.5 |

## 4. Error shape

Identical for API rejections, stage failures and failed-job status:

```json
{
  "stage": "DECORATION_OVERLAY",
  "code": "INVALID_TRANSPARENCY",
  "message": "The decoration image is fully opaque",
  "retryable": false
}
```

Stage names come from `lib.errors.Stage`, codes from `lib.errors.Code`. `message` is
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
