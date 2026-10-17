"""Asset validation: prove the separation actually worked.

Two independent checks, because each alone is insufficient (docs/PROBE_RESULTS.md):

* Pillow measures the alpha channel at the regions EXTRACT_SCENE said must be
  transparent or opaque. Alpha statistics prove the channel exists and is used,
  but not that the right *content* is in it.
* Vision inspects the overlay composited onto contrasting backgrounds. That
  catches a photograph baked into the decoration layer, which no alpha histogram
  would reveal. During the probe the model itself volunteered that it could not
  confirm real alpha from a preview, which is exactly why both run.

CALIBRATION, measured not assumed: the image model never emits alpha 255. Its
opaque pixels top out at 254 with a mode around 252-254. A naive `alpha == 255`
test therefore sees ZERO opaque pixels and rejects every valid overlay. The
threshold below is the reason this module works at all.
"""

from __future__ import annotations

import io

import conf
from lib.errors import AssetInvalid, InvalidTransparency

#: Measured: alpha never reaches 255 even on fully opaque artwork.
OPAQUE_THRESHOLD = 250

#: Below this a pixel reads as showing the photograph through.
TRANSPARENT_THRESHOLD = 16

#: Fraction of a sample region that must agree with its declared kind. Not 100%:
#: a region clipped from a model-reported box legitimately catches a soft edge.
REGION_AGREEMENT = 0.80

#: A layer with no partially transparent pixels at all has hard-keyed edges,
#: which composite with visible jaggies over the photograph.
MIN_PARTIAL_FRACTION = 0.0005


def load_rgba(data):
    """Decode to RGBA, normalizing the alpha ceiling.

    Normalization matters beyond tidiness: compositing an overlay whose opaque
    pixels sit at 252 leaves a faint haze, and the haze accumulates every time
    the layer is composited.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            had_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
            rgba = img.convert("RGBA")
    except Exception as exc:
        raise AssetInvalid("The asset is not a decodable image.") from exc
    return rgba, had_alpha


def normalize_alpha(rgba):
    """Lift near-opaque alpha to a true 255 without touching real translucency."""
    alpha = rgba.getchannel("A").point(
        lambda a: 255 if a >= OPAQUE_THRESHOLD else a
    )
    rgba.putalpha(alpha)
    return rgba


def alpha_stats(rgba):
    """Summarize the alpha channel as fractions of total pixels."""
    histogram = rgba.getchannel("A").histogram()
    total = sum(histogram) or 1
    transparent = sum(histogram[:TRANSPARENT_THRESHOLD])
    opaque = sum(histogram[OPAQUE_THRESHOLD:])
    return {
        "transparent": transparent / total,
        "opaque": opaque / total,
        "partial": (total - transparent - opaque) / total,
        "alphaMax": max(i for i, c in enumerate(histogram) if c) if total else 0,
    }


def validate_overlay(data, scene, canvas):
    """Check the decoration overlay. Raises InvalidTransparency on failure.

    Returns the measured statistics so FINALIZE can record what was observed
    rather than only that the check passed.
    """
    rgba, had_alpha = load_rgba(data)
    if not had_alpha:
        raise InvalidTransparency(
            "The decoration overlay has no alpha channel; it is a flat image, "
            "so the photograph cannot show through it."
        )

    rgba = normalize_alpha(rgba)
    rgba = resample_to_canvas(rgba, canvas)
    stats = alpha_stats(rgba)

    if stats["transparent"] < 0.01:
        raise InvalidTransparency(
            "The decoration overlay is effectively opaque across the canvas "
            f"({stats['transparent']:.1%} transparent); it would hide the photograph."
        )
    if stats["opaque"] < 0.001:
        raise InvalidTransparency(
            "The decoration overlay is effectively empty; no opaque artwork was "
            "found."
        )
    if stats["partial"] < MIN_PARTIAL_FRACTION:
        raise InvalidTransparency(
            "The decoration overlay has no soft edges, so it was hard-keyed "
            "rather than generated with real transparency."
        )

    failures = check_sample_regions(rgba, scene, canvas)
    if failures:
        raise InvalidTransparency(
            "The decoration overlay does not match the expected layout: "
            + "; ".join(failures)
        )
    return stats


def check_sample_regions(rgba, scene, canvas):
    """Verify the regions EXTRACT_SCENE declared transparent or opaque.

    This is what distinguishes a good overlay from a bad one. A global
    transparency percentage cannot: the right fraction depends entirely on how
    dense the design is, so a threshold that passes a sparse layout would also
    pass an overlay with the photograph baked into it.
    """
    samples = scene.get("overlaySamples") or []
    if not samples:
        return ["the scene declared no overlay sample regions to verify"]

    failures = []
    for index, sample in enumerate(samples):
        box = _clip(sample, canvas)
        if box is None:
            continue
        region = rgba.crop(box).getchannel("A")
        histogram = region.histogram()
        total = sum(histogram) or 1
        if sample["kind"] == "transparent":
            agree = sum(histogram[:TRANSPARENT_THRESHOLD]) / total
            if agree < REGION_AGREEMENT:
                failures.append(
                    f"region {index} should show the photograph through but is "
                    f"only {agree:.0%} transparent"
                )
        else:
            agree = sum(histogram[OPAQUE_THRESHOLD:]) / total
            if agree < REGION_AGREEMENT:
                failures.append(
                    f"region {index} should be solid artwork but is only "
                    f"{agree:.0%} opaque"
                )
    return failures


def validate_background(data, canvas):
    """Check the cleaned photograph.

    A background that arrives with meaningful transparency means CLEAN_PHOTO
    removed the graphics but did not reconstruct the scene behind them, leaving
    panel-shaped holes (SPEC section 4).
    """
    rgba, had_alpha = load_rgba(data)
    if had_alpha:
        stats = alpha_stats(rgba)
        if stats["transparent"] > 0.005:
            raise AssetInvalid(
                "The cleaned photograph has transparent areas "
                f"({stats['transparent']:.1%}); the scene behind the removed "
                "graphics was not reconstructed."
            )
    rgb = resample_to_canvas(rgba, canvas).convert("RGB")
    _check_not_blank(rgb)
    return {"width": rgb.width, "height": rgb.height}


def resample_to_canvas(img, canvas):
    """Resize to the target canvas.

    The provider renders only a fixed set of sizes, so a requested canvas is
    generated at the nearest aspect and resampled here - in exactly one place, so
    the background and the overlay can never be mapped differently and drift out
    of alignment.
    """
    from PIL import Image

    target = (canvas["width"], canvas["height"])
    if img.size == target:
        return img
    return img.resize(target, Image.LANCZOS)


def _check_not_blank(rgb):
    """Reject a flat or near-flat image.

    A provider that fails softly returns a plausible solid colour rather than an
    error, and a solid background renders without complaint.
    """
    colors = rgb.getcolors(maxcolors=4096)
    if colors is not None and len(colors) < 8:
        raise AssetInvalid(
            "The cleaned photograph is a near-solid colour, not a photographic "
            "scene."
        )


def _clip(box, canvas):
    """Clamp a model-reported box to the canvas, or None if it is degenerate."""
    left = max(0, int(box["x"]))
    top = max(0, int(box["y"]))
    right = min(canvas["width"], left + int(box["width"]))
    bottom = min(canvas["height"], top + int(box["height"]))
    if right - left < 2 or bottom - top < 2:
        return None
    return (left, top, right, bottom)
