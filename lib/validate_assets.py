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
from lib import schemas
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

    if not scene.get("overlaySamples"):
        raise InvalidTransparency(
            "The decoration overlay does not match the expected layout: "
            "the scene declared no overlay sample regions to verify"
        )
    failures = sample_failures(rgba, scene, canvas)
    if failures:
        error = InvalidTransparency(
            "The decoration overlay does not match the expected layout: "
            + "; ".join(f["message"] for f in failures)
        )
        # What the repair is told: located, actionable, not region numbers.
        error.defect = "\n".join(describe_failures(failures, canvas))
        held = _components_covering(failures, scene)
        if held:
            # The overlay is painting an area that the layout draws itself, so
            # the photograph cannot show through there. If the bounded repair
            # cannot separate it, the element can be given back to the overlay
            # instead - unless it is one of the components that must stay
            # editable, which the caller decides.
            error.surrender = {"owner": "overlay", "components": held}
        raise error
    return stats


def _components_covering(failures, scene):
    """Editable components whose box holds a failing transparent sample.

    Measured live: the layout owned the readability gradient and the overlay kept
    painting it, so a region declared see-through came back 14% transparent and
    two paid overlay repairs made no difference.
    """
    held = {}
    for component in editable_component_boxes(scene):
        for failure in failures:
            if failure["kind"] != "transparent":
                continue
            box = {"x": failure["box"][0], "y": failure["box"][1],
                   "width": failure["box"][2] - failure["box"][0],
                   "height": failure["box"][3] - failure["box"][1]}
            if _covers(component["box"], box):
                held[component["layerId"]] = {**component, "sample": failure["index"]}
    return list(held.values())


#: How much of a failing sample a component must cover before the component is
#: read as the thing the overlay is still painting there.
SAMPLE_COVERAGE = 0.8


def _covers(outer, inner):
    """Does `outer` contain at least SAMPLE_COVERAGE of `inner`'s area?"""
    width = min(outer["x"] + outer["width"], inner["x"] + inner["width"]) \
        - max(outer["x"], inner["x"])
    height = min(outer["y"] + outer["height"], inner["y"] + inner["height"]) \
        - max(outer["y"], inner["y"])
    area = max(1, inner["width"] * inner["height"])
    return width > 0 and height > 0 and (width * height) / area >= SAMPLE_COVERAGE


#: Mean alpha, as a fraction of fully opaque, above which a component's own box
#: still contains artwork in the overlay. Low: anything beyond a faint edge means
#: the element is still painted there, and a duplicate the customer cannot move
#: is worse than a soft edge that is not.
BAKED_ALPHA = 0.1

#: Only the middle of a component's box is measured. Its edge legitimately
#: touches neighbouring artwork, and a model-reported box is approximate.
COMPONENT_SAMPLE = 0.7


def editable_component_boxes(scene):
    """Boxes of the components the layout draws or places itself.

    Taken from the decomposition plan, so one list drives extraction, assembly
    and these checks: a component classified editable must not also be pixels in
    the overlay or the photograph.
    """
    layers = {
        layer["id"]: layer
        for layer in list(scene.get("shapeLayers") or [])
        + list(scene.get("imageLayers") or [])
    }
    out = []
    for component in scene.get("components") or []:
        if component["kind"] not in ("editableShape", "rasterIcon"):
            continue
        layer = layers.get(component["layerId"])
        if layer is None:
            continue
        out.append(
            {
                "componentId": component["id"],
                "layerId": layer["id"],
                "kind": component["kind"],
                "shape": layer.get("kind"),
                "color": layer.get("color"),
                "radius": int(layer.get("radius") or 0),
                "box": {k: int(layer[k]) for k in ("x", "y", "width", "height")},
            }
        )
    return out


def erase_component_areas(data, components, canvas):
    """Clear the named components' boxes from the overlay, in code.

    The last resort after the bounded overlay repair: the model keeps leaving a
    panel or a circle in the artwork, and the layout draws the same thing. Here
    the duplicate is removed arithmetically instead - the box becomes genuinely
    transparent, which is what the generated overlay was asked for.

    This is safe because CLEAN_PHOTO repaints the photograph under exactly these
    boxes and the layout then draws its shape over them: the pixels cleared here
    are pixels the finished design covers again. Only the component's own box is
    touched, and with a small inset so a neighbouring edge is not clipped.

    Returns the new PNG bytes.
    """
    from PIL import Image

    rgba, _ = load_rgba(data)
    rgba = normalize_alpha(rgba)
    scale_x = rgba.width / canvas["width"]
    scale_y = rgba.height / canvas["height"]
    cleared = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    for component in components:
        box = component["box"]
        left = int(round(box["x"] * scale_x))
        top = int(round(box["y"] * scale_y))
        right = int(round((box["x"] + box["width"]) * scale_x))
        bottom = int(round((box["y"] + box["height"]) * scale_y))
        if right <= left or bottom <= top:
            continue
        rgba.paste(cleared.crop((left, top, right, bottom)), (left, top))
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    return buf.getvalue()


def mandatory_component_ids(scene):
    """Components whose editability is not negotiable.

    The acceptance list: the call-to-action background, each contact circle and
    each contact pictogram. If one of these cannot be separated the design has
    not met the requirement, and that is a failure rather than a warning.

    Everything else the extractor recognised - a decorative chevron, a band, a
    row of dots - is a bonus. When the overlay repair cannot remove it after its
    bounded attempts, the honest outcome is to leave it in the artwork and say
    so, not to throw away a finished design over a chevron.
    """
    texts = {layer["id"]: layer for layer in scene.get("textLayers") or []}
    required = set()
    for component in scene.get("components") or []:
        if component["kind"] == "rasterIcon":
            required.add(component["id"])
            continue
        if component["kind"] != "editableShape":
            continue
        paired = texts.get(component.get("pairsWith") or "")
        if paired and (paired.get("role") == "cta"
                       or str(paired.get("contentKey", "")).startswith(
                           schemas.CONTACT_KEY_PREFIX)):
            required.add(component["id"])
    return required


def check_no_baked_components(data, scene, canvas):
    """The overlay must not still contain what the layout draws itself.

    This is the duplicate defect: a CTA background or a contact circle both drawn
    in CSS and left in the artwork. Nothing looks wrong until the customer moves
    or hides the editable one and the baked copy is still there.

    Raises InvalidTransparency naming each component, so the existing bounded
    overlay repair is told exactly what to remove. Returns the components it
    measured.
    """
    components = editable_component_boxes(scene)
    if not components:
        return []

    rgba, had_alpha = load_rgba(data)
    if not had_alpha:
        return []                   # validate_overlay reports the real fault
    rgba = normalize_alpha(rgba)
    rgba = resample_to_canvas(rgba, canvas)

    failures = []
    for component in components:
        box = _central(component["box"], canvas, COMPONENT_SAMPLE)
        if box is None:
            continue
        alpha = rgba.crop(box).getchannel("A")
        histogram = alpha.histogram()
        pixels = sum(histogram) or 1
        mean = sum(i * count for i, count in enumerate(histogram)) / pixels / 255
        if mean > BAKED_ALPHA:
            failures.append({**component, "meanAlpha": mean, "measured": box})

    if failures:
        error = InvalidTransparency(
            "The decoration overlay still contains elements the layout draws "
            "itself: "
            + "; ".join(
                f"{f['layerId']} ({f['meanAlpha']:.0%} opaque)" for f in failures
            )
            + ". Drawn twice, they would duplicate when one is moved."
        )
        error.defect = "\n".join(
            f"the area at x={f['box']['x']}, y={f['box']['y']} "
            f"({f['box']['width']}x{f['box']['height']} px on the "
            f"{canvas['width']}x{canvas['height']} canvas) holds a "
            f"{f['shape'] or 'graphic'} that the layout draws in code; remove it "
            "completely and leave genuine transparency there, keeping the artwork "
            "around it untouched."
            for f in failures
        )
        error.surrender = {"owner": "overlay", "components": failures}
        raise error
    return components


def check_component_area_repainted(data, scene, canvas):
    """The photograph must not still show a component that was removed from it.

    CLEAN_PHOTO repaints under a mask that covers each opaque component's box. If
    the panel's colour is still the dominant colour there, the repaint did not
    happen and the photograph carries a second copy of the button.

    Raises InvalidTransparency - the class the bounded repair cycle branches on -
    which regenerates the overlay and then repaints the photograph around it.
    """
    from lib import concept_sample

    opaque = [
        component
        for component in editable_component_boxes(scene)
        if component["color"] and component["shape"] != "gradient"
    ]
    if not opaque:
        return []

    failures = []
    for component in opaque:
        # Read where only the fill is (a ring for a circle, an edge band for a
        # panel), so a pictogram or the words in the middle neither hide a baked
        # copy nor read as a different colour.
        shape = {"kind": component["shape"], "radius": component.get("radius", 0),
                 **component["box"]}
        sample = concept_sample.fill_color(data, shape, canvas)
        if not sample or concept_sample.differs(component["color"], sample["color"]):
            continue
        # The colour matching is not enough on its own: a dark circle over dark
        # photography matches, and failing the job for that costs a finished
        # design. A shape still painted here is flat; repainted photography is
        # not, so how uniform the sample is decides.
        if sample["spread"] > concept_sample.FLAT_SPREAD:
            continue
        failures.append({**component, "sampled": sample["color"],
                         "spread": sample["spread"]})

    if failures:
        error = InvalidTransparency(
            "The cleaned photograph still shows elements the layout draws "
            "itself: "
            + "; ".join(f"{f['layerId']} ({f['sampled']})" for f in failures)
            + ". The area was not repainted, so the design would carry two copies."
        )
        error.defect = "\n".join(
            f"the area at x={f['box']['x']}, y={f['box']['y']} "
            f"({f['box']['width']}x{f['box']['height']} px) is still filled with "
            f"{f['color']}; that {f['shape'] or 'graphic'} is drawn by the layout. "
            "Reconstruct the photograph behind it instead."
            for f in failures
        )
        error.surrender = {"owner": "photo", "components": failures}
        raise error
    return opaque


def _central(box, canvas, fraction):
    """The middle `fraction` of a clipped box, or None when too small to measure."""
    clipped = _clip(box, canvas)
    if clipped is None:
        return None
    left, top, right, bottom = clipped
    inset_x = (right - left) * (1 - fraction) / 2
    inset_y = (bottom - top) * (1 - fraction) / 2
    middle = (
        int(round(left + inset_x)),
        int(round(top + inset_y)),
        int(round(right - inset_x)),
        int(round(bottom - inset_y)),
    )
    if middle[2] - middle[0] < 2 or middle[3] - middle[1] < 2:
        return None
    return middle


def check_sample_regions(rgba, scene, canvas):
    """Verify the regions EXTRACT_SCENE declared transparent or opaque.

    This is what distinguishes a good overlay from a bad one. A global
    transparency percentage cannot: the right fraction depends entirely on how
    dense the design is, so a threshold that passes a sparse layout would also
    pass an overlay with the photograph baked into it.

    Returns one message per failing region. `sample_failures` returns the same
    findings with their locations, which is what a repair needs.
    """
    samples = scene.get("overlaySamples") or []
    if not samples:
        return ["the scene declared no overlay sample regions to verify"]
    return [f["message"] for f in sample_failures(rgba, scene, canvas)]


def sample_failures(rgba, scene, canvas):
    """Measure every declared sample region; return the ones that disagree.

    A region "agrees" when at least REGION_AGREEMENT of its pixels are
    transparent (alpha below TRANSPARENT_THRESHOLD) for a transparent region, or
    opaque (alpha at or above OPAQUE_THRESHOLD) for an opaque one. The
    denominator is every pixel of the region after the overlay has been
    resampled to the canvas, so "74% transparent" means 74% of those pixels.
    """
    failures = []
    owned = [c["box"] for c in editable_component_boxes(scene)]
    for index, sample in enumerate(scene.get("overlaySamples") or []):
        box = _clip(sample, canvas)
        if box is None:
            continue
        if sample["kind"] == "opaque" and any(_overlaps(sample, o) for o in owned):
            # The sample was taken from the concept, where that panel or circle
            # was still part of the picture. The layout draws it now, so the
            # overlay is right to be transparent there and this sample is no
            # longer evidence of anything. (Measured: the extractor samples the
            # call-to-action background as solid artwork, and the overlay that
            # correctly removed it was then rejected for removing it.)
            continue
        histogram = rgba.crop(box).getchannel("A").histogram()
        total = sum(histogram) or 1
        if sample["kind"] == "transparent":
            agree = sum(histogram[:TRANSPARENT_THRESHOLD]) / total
            message = (
                f"region {index} should show the photograph through but is "
                f"only {agree:.0%} transparent"
            )
        else:
            agree = sum(histogram[OPAQUE_THRESHOLD:]) / total
            message = (
                f"region {index} should be solid artwork but is only "
                f"{agree:.0%} opaque"
            )
        if agree < REGION_AGREEMENT:
            failures.append(
                {
                    "index": index,
                    "kind": sample["kind"],
                    "box": box,
                    "agree": agree,
                    "message": message,
                }
            )
    return failures


def _overlaps(one, other):
    """Do two boxes share any pixel?"""
    return (
        int(one["x"]) < int(other["x"]) + int(other["width"])
        and int(other["x"]) < int(one["x"]) + int(one["width"])
        and int(one["y"]) < int(other["y"]) + int(other["height"])
        and int(other["y"]) < int(one["y"]) + int(one["height"])
    )


def describe_failures(failures, canvas):
    """Turn region failures into instructions an image model can act on.

    Region numbers mean nothing to a model; where the area is and what it should
    be does. Coordinates are canvas pixels from the top-left.
    """
    lines = []
    for f in failures:
        left, top, right, bottom = f["box"]
        where = (
            f"the area at x={left}, y={top} ({right - left}x{bottom - top} px "
            f"on the {canvas['width']}x{canvas['height']} canvas)"
        )
        if f["kind"] == "transparent":
            lines.append(
                f"{where} must be fully transparent so the photograph shows "
                f"through, but only {f['agree']:.0%} of it is. Remove whatever "
                "fills it, including any dark or coloured backing rectangle."
            )
        else:
            lines.append(
                f"{where} belongs to a solid graphic panel and must be fully "
                f"opaque, but only {f['agree']:.0%} of it is. Keep that panel "
                "whole and opaque."
            )
    return lines


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
