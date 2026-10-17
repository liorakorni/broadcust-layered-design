"""Render the assembled HTML with Chromium and verify what actually appeared.

Rendering is the only way to know whether the design works. A scene can be
geometrically valid and still produce text that overflows its box, a font that
silently fell back, or an image that failed to load - none of which raise an
error anywhere upstream.

Two measured landmines from docs/PROBE_RESULTS.md are encoded here:

* Fonts are installed into the system font path and resolved through fontconfig,
  so `document.fonts` is EMPTY - it only lists @font-face rules. Font presence
  must be checked with `document.fonts.check('<weight> <size>px <family>')`.
  Iterating `document.fonts` reports nothing and an @font-face rule using
  `src: local(...)` reports status "error" even when the family works perfectly.
* PLAYWRIGHT_BROWSERS_PATH must point at /opt/playwright; Playwright's default
  under $HOME is not writable in Lambda.
"""

from __future__ import annotations

import conf
from lib.errors import FontMissing, RenderFailed

#: Chromium needs these in Lambda: no sandbox (no user namespaces) and /dev/shm
#: is only 64MB, which crashes the renderer on large pages.
#:
#: --single-process and --no-zygote matter as much as the sandbox flags. Lambda
#: lets the browser process start but the forked zygote cannot bring up a
#: renderer, so launch() succeeds and the first new_page() reports "Target
#: crashed" with no browser log to explain it. Collapsing everything into one
#: process avoids the fork entirely. --disable-software-rasterizer stops
#: Chromium from retrying a GPU process it can never start.
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--single-process",
    "--no-zygote",
    "--font-render-hinting=none",
]

_PAGE_TIMEOUT_MS = 30000


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RenderFailed(
            "Playwright is not installed in this container."
        ) from exc
    return sync_playwright


def check_available():
    """Cheap PREFLIGHT probe: launch Chromium and confirm the fonts resolve."""
    with _playwright()() as p:
        browser = p.chromium.launch(args=_LAUNCH_ARGS)
        try:
            page = browser.new_page()
            page.set_content("<html><body></body></html>")
            missing = _missing_fonts(page, conf.APPROVED_FONT_FAMILIES)
            if missing:
                raise FontMissing(
                    "Fonts are not installed in the renderer: " + ", ".join(missing)
                )
        finally:
            browser.close()
    return True


def render(html, canvas, scene):
    """Render and inspect. Returns `{native, feed, checks}` with PNG bytes.

    `native` is the full-size render; `feed` is the same design at 360px wide,
    which is where small text becomes illegible in a social feed (SPEC section 4).
    """
    document = _document(html, canvas)
    families = {layer["fontFamily"] for layer in scene["textLayers"]}

    with _playwright()() as p:
        browser = p.chromium.launch(args=_LAUNCH_ARGS)
        try:
            page = browser.new_page(
                viewport={"width": canvas["width"], "height": canvas["height"]},
                device_scale_factor=1,
            )
            page.set_default_timeout(_PAGE_TIMEOUT_MS)
            page.set_content(document, wait_until="load")

            _wait_for_assets(page)

            missing = _missing_fonts(page, families)
            if missing:
                raise FontMissing(
                    "The design uses fonts the renderer does not have: "
                    + ", ".join(sorted(missing))
                )

            checks = _inspect(page, canvas)
            native = page.screenshot(type="png")
            feed = _feed_capture(page, canvas)
        except RenderFailed:
            raise
        except FontMissing:
            raise
        except Exception as exc:
            raise RenderFailed(f"The design could not be rendered: {exc}") from exc
        finally:
            browser.close()

    return {"native": native, "feed": feed, "checks": checks}


#: How far a layer's font may be reduced to make it fit, as a fraction of the
#: size the scene asked for. The defect being absorbed is small - the model
#: estimates box widths from a concept drawn in a different typeface, and is
#: typically out by under ten percent - but a box is only one line tall, so a
#: slight overrun wraps and doubles the height. A modest reduction therefore
#: fixes most of it. Text that still does not fit at this floor is left alone
#: and reported: shrinking further would trade an overflow REVIEW can see for
#: illegibility at the 360px feed size, which it cannot.
_MIN_FIT_RATIO = 0.75


def fit_text_layers(html, canvas):
    """Shrink text that overflows its box, and report what was changed.

    Only a browser holding the real font files can answer how wide a string
    actually sets, which is why this measures rather than estimates. Each layer
    is searched for the largest size that fits, so a layer that needs one pixel
    loses one pixel.
    """
    document = _document(html, canvas)

    with _playwright()() as p:
        browser = p.chromium.launch(args=_LAUNCH_ARGS)
        try:
            page = browser.new_page(
                viewport={"width": canvas["width"], "height": canvas["height"]},
                device_scale_factor=1,
            )
            page.set_default_timeout(_PAGE_TIMEOUT_MS)
            # Images are irrelevant to text metrics and would only add a network
            # wait, but fonts are not: measuring before Heebo resolves would size
            # every layer against a fallback face.
            page.set_content(document, wait_until="domcontentloaded")
            page.evaluate("() => document.fonts.ready")
            fitted = page.evaluate(_FIT_SCRIPT, {"minRatio": _MIN_FIT_RATIO})
        except Exception as exc:
            raise RenderFailed(f"The design could not be measured: {exc}") from exc
        finally:
            browser.close()

    return fitted


#: Returns one entry per layer it changed, keyed by element id.
_FIT_SCRIPT = """(options) => {
    const changed = {};

    for (const el of document.querySelectorAll('.words')) {
        const inner = el.querySelector('.text');
        if (!inner || !inner.textContent.trim()) continue;

        const fits = () => el.scrollHeight <= el.clientHeight + 1
                        && el.scrollWidth <= el.clientWidth + 1;
        if (fits()) continue;

        const original = Math.round(parseFloat(getComputedStyle(el).fontSize));
        const floor = Math.max(1, Math.floor(original * options.minRatio));

        // Largest size that fits, rather than the first one found: stepping down
        // would stop at whatever size happened to clear the box.
        let low = floor, high = original - 1, best = null;
        while (low <= high) {
            const mid = Math.floor((low + high) / 2);
            el.style.fontSize = mid + 'px';
            if (fits()) { best = mid; low = mid + 1; } else { high = mid - 1; }
        }

        const applied = best === null ? floor : best;
        el.style.fontSize = applied + 'px';
        changed[el.id] = {original: original, fitted: applied, fits: best !== null};
    }

    return changed;
}"""


def _document(html, canvas):
    """Wrap the design fragment in a minimal page.

    No external stylesheet and no @font-face: families resolve through
    fontconfig, and a web font request would be a network call Lambda may not be
    able to make.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:#fff;}"
        f"body{{width:{canvas['width']}px;height:{canvas['height']}px;}}</style>"
        f"</head><body>{html}</body></html>"
    )


def _wait_for_assets(page):
    """Block until every image has actually decoded.

    `load` fires before images finish decoding in some cases, and a screenshot
    taken then captures empty boxes where the photograph should be.
    """
    page.wait_for_function(
        """() => Array.from(document.images).every(
            img => img.complete && img.naturalWidth > 0
        )""",
        timeout=_PAGE_TIMEOUT_MS,
    )
    page.evaluate("() => document.fonts.ready")


def _missing_fonts(page, families):
    """Report families Chromium cannot resolve.

    Uses fonts.check rather than iterating document.fonts. See the module
    docstring: iteration returns nothing for system-installed fonts.
    """
    return page.evaluate(
        """(families) => families.filter(
            f => !document.fonts.check(`400 16px "${f}"`)
        )""",
        list(families),
    )


def _inspect(page, canvas):
    """Measure what rendered: overflow, out-of-bounds boxes, duplicate IDs.

    Overflow is the failure that matters most. Hebrew text is wider than the
    model predicts more often than not, and overflowing text renders perfectly
    happily - it just runs outside its box and over the next layer.
    """
    return page.evaluate(
        """(canvas) => {
            const problems = [];
            const seen = new Set();

            for (const el of document.querySelectorAll('[id]')) {
                if (seen.has(el.id)) {
                    problems.push({code: 'DUPLICATE_ID', layerId: el.id,
                        message: `Duplicate element id ${el.id}`});
                }
                seen.add(el.id);
            }

            for (const el of document.querySelectorAll('.words')) {
                const inner = el.querySelector('.text');
                if (!inner) continue;
                // scrollHeight exceeding clientHeight is text that did not fit.
                if (el.scrollHeight > el.clientHeight + 1) {
                    problems.push({code: 'TEXT_OVERFLOW', layerId: el.id,
                        message: `Text in ${el.id} is ${el.scrollHeight}px tall `
                            + `in a ${el.clientHeight}px box`});
                }
                if (el.scrollWidth > el.clientWidth + 1) {
                    problems.push({code: 'TEXT_OVERFLOW', layerId: el.id,
                        message: `Text in ${el.id} is ${el.scrollWidth}px wide `
                            + `in a ${el.clientWidth}px box`});
                }
                if (!inner.textContent.trim()) {
                    problems.push({code: 'EMPTY_TEXT', layerId: el.id,
                        message: `Layer ${el.id} rendered no text`});
                }
            }

            for (const el of document.querySelectorAll('.words, .badges, .background')) {
                const r = el.getBoundingClientRect();
                if (r.left < -1 || r.top < -1
                    || r.right > canvas.width + 1
                    || r.bottom > canvas.height + 1) {
                    problems.push({code: 'OUT_OF_BOUNDS', layerId: el.id || null,
                        message: `${el.id || el.className} extends outside the canvas`});
                }
            }

            return {
                problems,
                textLayers: document.querySelectorAll('.words').length,
                images: document.images.length,
            };
        }""",
        canvas,
    )


def _feed_capture(page, canvas):
    """Capture the design at feed width.

    Scaled with a CSS transform rather than by resizing the viewport, so the
    layout is identical to the native render and only the display size differs -
    which is exactly what a social feed does.
    """
    scale = conf.FEED_RENDER_WIDTH / canvas["width"]
    height = max(1, round(canvas["height"] * scale))
    page.set_viewport_size({"width": conf.FEED_RENDER_WIDTH, "height": height})
    page.evaluate(
        """(scale) => {
            const root = document.querySelector('.image.container');
            root.style.transformOrigin = 'top left';
            root.style.transform = `scale(${scale})`;
            document.body.style.width = '100%';
            document.body.style.height = '100%';
        }""",
        scale,
    )
    shot = page.screenshot(type="png")
    page.evaluate(
        """() => {
            const root = document.querySelector('.image.container');
            root.style.transform = '';
        }"""
    )
    return shot
