# Phase 1 probe results

Measured capability findings. `lib/validate_assets.py` is calibrated from the numbers
here, not from assumed thresholds. Re-run the probes and update this file if a model ID
changes.

Reproduce with:

```bash
.venv/bin/python scripts/probe_image.py
.venv/bin/python scripts/probe_vision.py
```

---

## Image model — `gpt-image-2.5-sunburst`

Run 2026-10-17. Three calls at `size=1024x1024`, `quality=low`, `output_format=png`,
`output_compression` omitted (invalid for PNG).

**Result: PASS.** The edit path returns a genuine alpha channel, so `DECORATION_OVERLAY`
is implementable.

| Step | Mirrors | Mode | alpha min/max | % transparent (<16) | % opaque (>=250) | % partial |
|---|---|---|---|---|---|---|
| generate, `background="opaque"` | `CONCEPT` | **RGB** | 255 / 255 | 0.0 | 100.0 | 0.0 |
| edit, `background="transparent"` | `DECORATION_OVERLAY` | **RGBA** | 0 / **254** | 85.98 | 12.26 | 1.76 |
| generate, `background="transparent"` | transparent asset | **RGBA** | 0 / **254** | 83.04 | 16.10 | 0.86 |

### Calibration finding: opaque pixels never reach alpha 255

This is the single most important number here.

```
pct_opaque_eq255   = 0.0      <- no pixel reaches 255, in either transparent output
pct_opaque_gte250  = 12.26
alpha_max          = 254
most common opaque alpha values: 253, 254, 252, 251, 250
```

The previously reported 252-254 artifact is real and reproducible. Consequences:

- **Treat `alpha >= 250` as opaque.** A naive `alpha == 255` test sees *zero* opaque
  pixels and rejects every valid overlay — a total false negative on the one asset the
  service exists to produce.
- Normalize alpha before compositing, or repeated compositing will accumulate haze.
- Do not apply a single global transparency percentage as a pass/fail. The transparent
  fraction here ranges 83-86% for a full-canvas overlay but would be far lower for a
  dense design. SPEC's `VALIDATE_ASSETS` requirement to sample scene-derived regions is
  what actually distinguishes a good overlay from a bad one.

`generate` with `background="opaque"` returns **RGB with no alpha channel at all**, which
is the correct and expected shape for a concept.

### Visual verification

`scripts/probe_out/contact_sheet.png` composites the overlay over magenta, white and
black. The overlay renders identically on all three: the photographic scene is fully
removed, the badge, service panels and CTA shape are retained with clean edges, and there
is no checkerboard and no photographic leakage.

Two behaviours worth recording:

- The concept contained baked marketing text including an **invented price**. Expected
  and harmless — the concept is an internal layout guide, never the deliverable. It is
  also a concrete demonstration of why SPEC requires the copy manifest to be the only
  source of truth for final text, and why `CLEAN_PHOTO` and `DECORATION_OVERLAY` must
  both strip words.
- The overlay correctly kept graphic panels while dropping their text, which is the
  separation the HTML contract depends on.

### Generation sizes

Only `1024x1024`, `1024x1536` and `1536x1024` are available. A 1080x1080 canvas is
generated at `1024x1024` and resampled to target in exactly one place
(`lib/validate_assets.py`).

---

## Vision model — `gpt-6-astra`

Run 2026-10-17. Two calls against the real concept and overlay from the image probe.

**Result: PASS.** All three required capabilities are supported.

| Capability | Needed by | Result |
|---|---|---|
| `response_format` `json_schema` with `strict: true` | `EXTRACT_SCENE`, `REVIEW` | Schema enforced; 14 text layers returned, all conforming |
| `detail="original"` | `EXTRACT_SCENE` | Accepted; coordinates verified correct (below) |
| Three images in one call | `REVIEW` | All three described distinctly |

### Coordinate accuracy is verified, not assumed

The model reported `canvas: {width: 1024, height: 1024}`, matching the image's true
dimensions. Drawing the returned boxes back onto the concept
(`scripts/probe_out/coord_check.png`) shows them wrapping their text tightly:

| Layer | Text | Box (x, y, w, h) |
|---|---|---|
| `intro` | DISCOVER A BRIGHTER WINTER | 240, 69, 360, 51 |
| `destination` | ITALY | 204, 153, 424, 101 |
| `tagline` | TIME TRAVELS DIFFERENTLY HERE | 170, 283, 491, 18 |
| `themes` | ART / FOOD / BREATHTAKING PLACES | 193, 332, 445, 15 |

This is the empirical basis for invariant 4. Without `detail="original"` these
coordinates would describe a resized frame and every layer would be misplaced with no
error raised.

### Chromium in the Lambda container

**Result: PASS.** `scripts/probe_render.py` run inside the built image produced
`scripts/probe_out/render_probe.png`: Hebrew RTL text ("חורף באיטליה") in Heebo 800
weight, gold on navy, correctly right-aligned.

```
PLAYWRIGHT_BROWSERS_PATH = /opt/playwright
document.fonts.status = loaded
document.fonts.check('800 48px Heebo') = True
screenshot 400x400, 210 distinct colors   <- not blank
```

Four things this build had to get right, each a silent failure otherwise:

- **Corporate TLS interception.** This network runs a Cato Networks proxy that re-signs
  every certificate, so `dnf`, `pip` and the Playwright downloader all failed to verify
  upstream CDNs from inside the build (`self-signed certificate in certificate chain`).
  The Dockerfile trusts any root CA dropped into `assets/ca-certificates/*.crt`. The
  directory is tracked, the certificates are not — they are machine-specific, and with
  none present the step is a no-op.
- **Package manager.** The Lambda Python base is Amazon Linux 2023, so
  `playwright install --with-deps` is useless (it only knows apt). The Chromium shared
  libraries are installed explicitly with `dnf`.
- **Browser path.** `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright` is set for both build and
  run. Playwright's default under `$HOME` is not writable in Lambda.
- **Fonts need no `@font-face`.** Heebo is installed into the system font path and
  registered with `fc-cache`; Chromium resolves it through fontconfig. An `@font-face`
  rule using `src: local('Heebo')` reports `status: "error"` even though the family is
  perfectly usable, so `lib/render.py` must gate on
  `document.fonts.check('<weight> <size>px <family>')` and **not** on iterating
  `document.fonts`, which only lists `@font-face` rules and is empty for system fonts.

**Image size: 3.04 GB.** Within Lambda's 10 GB limit but large; expect slow cold starts.
`playwright install chromium` pulls both Chromium and the headless shell. Trimming to
just the headless shell is an available optimisation if cold starts become a problem.

### Vision alone cannot confirm transparency

In the three-image call the model volunteered:

> "Actual alpha transparency cannot be confirmed from the displayed preview alone."

That independently corroborates SPEC's `VALIDATE_ASSETS` requirement: alpha statistics
alone are insufficient *and* vision alone is insufficient. `lib/validate_assets.py` must
do both — measure the channel with Pillow **and** inspect contrasting-background
composites with vision.
