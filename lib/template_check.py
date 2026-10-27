"""Deterministic structural check of a template (Design Base) against its concept.

The template is an extraction of the concept, so wherever it is opaque it should look
like the concept does at that place. Opaque template pixels whose colour matches
NOTHING in the concept within the contour tolerance are artwork the concept does not
have: an added border, a continued frame, a panel that was never there. That is a
fact about pixels, measured in code, with no model call, and cheap enough to run
before the first HTML exists.

What it cannot see, and does not claim to: artwork the template LOST (a transparent
window where the concept has an ornament) and whether a plain dark region the concept
also has is a panel or shading over the photograph. Those stay with the composite
review, which judges the assembled design.

Findings come in two severities:

    major  the invariants of lib/template_policy.py: added artwork spanning
           MAJOR_SPAN of the canvas side, or larger than MAJOR_AREA. Same in every
           flexibility profile - it is why the check exists.
    minor  local variation beyond what the persisted profile allows (texture, soft
           edges, scattered additions, translucent veil). Warnings, never blocking.

Boxes of text and of everything the layout draws itself are excluded: those pixels are
supposed to differ (the template is asked to continue the surrounding artwork under
them), and the template never owned them.
"""

from __future__ import annotations

import io
import logging
from collections import deque

from PIL import Image, ImageChops, ImageFilter

from lib import coords, template_policy

log = logging.getLogger("layered-design.template-check")

CHECK_VERSION = "template-check-1"

#: Working resolution. The check is about regions, not pixels.
WORK = 256
#: Alpha at or above this is "opaque template"; below 16 is "transparent".
OPAQUE_ALPHA = 200
SOFT_LOW, SOFT_HIGH = 16, 239
#: Largest channel difference at which two colours still "match".
COLOUR_MISMATCH = 60
#: Mismatch fragments this close (working px) are one region.
BRIDGE = 31
#: Boxes of excluded components are grown by this many working pixels.
EXCLUDE_GROW = 3
#: A concept pixel within this colour distance of a text layer's ink is glyph, not artwork.
INK_MATCH = 90
INK_GROW = 5

CODE_ADDED = "TEMPLATE_ADDED_STRUCTURE"
CODE_EXTENT = "TEMPLATE_FILL_EXTENT"
CODE_CONTOUR = "TEMPLATE_CONTOUR_DRIFT"
CODE_TEXTURE = "TEMPLATE_TEXTURE_DRIFT"
CODE_FEATHER = "TEMPLATE_FEATHERING"
CODE_VEIL = "TEMPLATE_TRANSLUCENT_VEIL"


def compare(concept_bytes, template_bytes, scene, tolerance=None):
    """Measure `template_bytes` against `concept_bytes` under `tolerance`.

    `tolerance` is the persisted document from template_policy.resolve (None means the
    default profile, for a job accepted before the setting existed).
    Returns {checked, version, tolerance, measured, regions, findings, major}.
    """
    tolerance = tolerance or template_policy.resolve(None)
    tol = tolerance["tolerances"]
    invariants = tolerance["invariants"]

    maps = _maps(concept_bytes, template_bytes, scene, tolerance)
    concept, rgb, alpha = maps["concept"], maps["rgb"], maps["alpha"]
    judged, added, reach = maps["judged"], maps["added"], maps["reach"]
    differs_profile = maps["differsProfile"]

    area = float(WORK * WORK)
    regions = _regions(added, rgb, area)
    judged_pixels = _count(judged)

    findings = []
    scattered = 0.0
    for region in regions:
        is_major = (region["span"] >= invariants["majorSpan"]
                    or region["area"] >= invariants["majorArea"])
        if is_major:
            findings.append(_added_finding(region))
        else:
            scattered += region["area"]
    if scattered > tol["fillExtent"]:
        findings.append({
            "code": CODE_EXTENT, "severity": "minor",
            "message": f"Small areas of artwork the concept does not have add up to "
                       f"{scattered:.1%} of the canvas (profile allows "
                       f"{tol['fillExtent']:.1%}).",
            "area": round(scattered, 4),
        })
    drift = _count(ImageChops.multiply(differs_profile, ImageChops.invert(added))) / area
    if drift > tol["fillExtent"] * 2:
        findings.append({
            "code": CODE_CONTOUR, "severity": "minor",
            "message": f"Contours differ from the concept's by more than the profile's "
                       f"{tol['contourShift']:.1%} of the canvas over {drift:.1%} of it.",
            "area": round(drift, 4),
        })
    mismatch = added

    texture = _texture_drift(rgb, concept, judged, mismatch)
    if texture is not None and texture > tol["textureDelta"]:
        findings.append({
            "code": CODE_TEXTURE, "severity": "minor",
            "message": f"The template's surface differs from the concept's by "
                       f"{texture:.0%} on average (profile allows {tol['textureDelta']:.0%}).",
            "delta": round(texture, 4),
        })

    soft = _count(alpha.point(lambda a: 255 if SOFT_LOW <= a <= SOFT_HIGH else 0))
    soft_share = soft / float(max(1, judged_pixels + soft))
    if soft_share > tol["featherWidth"]:
        findings.append({
            "code": CODE_FEATHER, "severity": "minor",
            "message": f"{soft_share:.0%} of the template's pixels are soft-edged "
                       f"(profile allows {tol['featherWidth']:.0%}).",
            "share": round(soft_share, 4),
        })
    veil = soft / area
    if veil > tol["gradientVeil"] and soft_share > tol["featherWidth"] / 2:
        findings.append({
            "code": CODE_VEIL, "severity": "minor",
            "message": f"A translucent veil covers {veil:.1%} of the canvas "
                       f"(profile allows {tol['gradientVeil']:.1%}).",
            "area": round(veil, 4),
        })

    return {
        "checked": True,
        "version": CHECK_VERSION,
        "tolerance": {"value": tolerance["value"], "profile": tolerance["profile"],
                      "policyVersion": tolerance["policyVersion"]},
        "measured": {
            "opaqueShare": round(judged_pixels / area, 4),
            "addedArea": round(sum(r["area"] for r in regions), 4),
            "contourDrift": round(drift, 4),
            "textureDelta": None if texture is None else round(texture, 4),
            "softShare": round(soft_share, 4),
            "contourReachPx": reach,
            "workingSize": WORK,
        },
        "regions": regions,
        "findings": findings,
        "major": any(f["severity"] == "major" for f in findings),
    }



def _maps(concept_bytes, template_bytes, scene, tolerance):
    """The per-pixel maps the check and the photograph mask both read."""
    tol = tolerance["tolerances"]
    invariants = tolerance["invariants"]

    concept = _load_rgb(concept_bytes)
    template = _load_rgba(template_bytes)
    alpha = template.getchannel("A")
    rgb = template.convert("RGB")

    excluded = _excluded_mask(scene, concept)
    opaque = alpha.point(lambda a: 255 if a >= OPAQUE_ALPHA else 0)
    judged = ImageChops.multiply(opaque, ImageChops.invert(excluded))

    reach = max(1, round(tol["contourShift"] * WORK))
    major_reach = max(reach, round(invariants.get("majorReach", template_policy.MAJOR_REACH) * WORK))
    # Two maps. Beyond the PROFILE's reach a pixel is a local contour difference (minor,
    # and only when the profile is tighter than the variation). Beyond the fixed
    # MAJOR reach it is artwork the concept does not have at all.
    # The concept as a reference for "is there artwork like this nearby": the pixels of
    # text and layout-owned shapes are painted a colour no artwork has, so a yellow
    # headline or button next to an added yellow border cannot vouch for it.
    reference = Image.composite(Image.new("RGB", concept.size, (255, 0, 255)),
                                concept, excluded)
    near_profile = _nearest_difference(rgb, reference, reach)
    near_major = near_profile if major_reach == reach else \
        _nearest_difference(rgb, reference, major_reach)
    differs_profile = ImageChops.multiply(
        near_profile.point(lambda d: 255 if d > COLOUR_MISMATCH else 0), judged)
    added = ImageChops.multiply(
        near_major.point(lambda d: 255 if d > COLOUR_MISMATCH else 0), judged)
    return {"concept": concept, "rgb": rgb, "alpha": alpha, "judged": judged,
            "added": added, "reach": reach, "differsProfile": differs_profile}


def added_artwork_mask(concept_bytes, template_bytes, scene, tolerance=None, size=None, grow=7):
    """An L mask, 255 where the template has artwork the concept does not.

    Used to stop CLEAN_PHOTO treating a defective template's extra artwork as ground to
    repaint: the photograph is cleaned only where the concept really has artwork.
    `grow` widens it so the antialiased edge of an added stroke is covered too.
    """
    tolerance = tolerance or template_policy.resolve(None)
    added = _maps(concept_bytes, template_bytes, scene, tolerance)["added"]
    added = added.filter(ImageFilter.MaxFilter(grow))
    return added.resize(size, Image.BILINEAR).point(lambda v: 255 if v >= 64 else 0) if size else added

def _added_finding(region):
    box = region["box"]
    return {
        "code": CODE_ADDED, "severity": "major",
        "message": (
            f"The template has artwork of colour {region['color']} that the approved "
            f"concept does not have, spanning {region['span']:.0%} of the canvas "
            f"(x {box['x']:.0%}-{box['x'] + box['width']:.0%}, y {box['y']:.0%}-"
            f"{box['y'] + box['height']:.0%}). Remove it; do not add borders, "
            "contours or ornaments, and do not join separate ornaments."),
        "region": box, "span": region["span"], "area": region["area"],
        "color": region["color"],
    }


def _load_rgb(data):
    with Image.open(io.BytesIO(data)) as opened:
        return opened.convert("RGB").resize((WORK, WORK), Image.BILINEAR)


def _load_rgba(data):
    with Image.open(io.BytesIO(data)) as opened:
        return opened.convert("RGBA").resize((WORK, WORK), Image.BILINEAR)


def _excluded_mask(scene, concept):
    """255 where the template is not judged.

    A shape or pictogram the layout draws itself is excluded by its whole box. Text is
    excluded only where the concept shows the text's own ink (its colour, grown a
    little): the template is rightly different under a glyph, but a text COLUMN is
    wide, and excluding its whole box would hide exactly the artwork this check looks
    for - the fixture's added border runs behind the right-hand column.
    """
    canvas = scene.get("canvas") or {"width": WORK, "height": WORK}
    mask = Image.new("L", (WORK, WORK), 0)
    paint = Image.new("L", (WORK, WORK), 255)

    def grown(layer):
        box = coords.canvas_to_image(layer, canvas, (WORK, WORK))
        x0, y0 = max(0, box["x"] - EXCLUDE_GROW), max(0, box["y"] - EXCLUDE_GROW)
        x1 = min(WORK, box["x"] + box["width"] + EXCLUDE_GROW)
        y1 = min(WORK, box["y"] + box["height"] + EXCLUDE_GROW)
        return x0, y0, x1, y1

    whole = [s for s in scene.get("shapeLayers") or [] if s.get("kind") != "gradient"]
    whole += [i for i in scene.get("imageLayers") or [] if i.get("type") == "image"]
    for layer in whole:
        x0, y0, x1, y1 = grown(layer)
        if x1 > x0 and y1 > y0:
            mask.paste(paint.crop((0, 0, x1 - x0, y1 - y0)), (x0, y0))

    for layer in scene.get("textLayers") or []:
        x0, y0, x1, y1 = grown(layer)
        if x1 <= x0 or y1 <= y0:
            continue
        region = concept.crop((x0, y0, x1, y1))
        ink = Image.new("L", region.size, 0)
        for colour in (layer.get("color"), layer.get("strokeColor")):
            rgb = _hex(colour)
            if rgb is None:
                continue
            diff = ImageChops.difference(region, Image.new("RGB", region.size, rgb))
            r, g, b = diff.split()
            level = ImageChops.lighter(r, ImageChops.lighter(g, b))
            ink = ImageChops.lighter(ink, level.point(lambda d: 255 if d <= INK_MATCH else 0))
        ink = ink.filter(ImageFilter.MaxFilter(INK_GROW))
        mask.paste(ImageChops.lighter(mask.crop((x0, y0, x1, y1)), ink), (x0, y0))
    return mask


def _hex(value):
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return None
    try:
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None


def _nearest_difference(template_rgb, concept, reach):
    """Per pixel: the smallest colour difference to the concept within `reach` pixels."""
    best = None
    step = 1 if reach <= 4 else 2
    for dy in range(-reach, reach + 1, step):
        for dx in range(-reach, reach + 1, step):
            shifted = ImageChops.offset(concept, dx, dy)
            diff = ImageChops.difference(template_rgb, shifted)
            r, g, b = diff.split()
            level = ImageChops.lighter(r, ImageChops.lighter(g, b))
            best = level if best is None else ImageChops.darker(best, level)
    return best


def _texture_drift(template_rgb, concept, judged, mismatch):
    agreed = ImageChops.multiply(judged, ImageChops.invert(mismatch))
    count = _count(agreed)
    if count < WORK * WORK * 0.01:
        return None
    diff = ImageChops.difference(template_rgb.convert("L"), concept.convert("L"))
    masked = ImageChops.multiply(diff, agreed.point(lambda a: 1 if a else 0))
    return sum(masked.getdata()) / 255.0 / count


def _count(mask):
    return sum(1 for v in mask.getdata() if v)


def _regions(mask, rgb, area):
    """Connected regions of the mismatch mask, largest first.

    Fragments within BRIDGE pixels of each other are one region: an added border that
    passes close to legitimate artwork is matched there and mismatched elsewhere, and
    it is the whole border that matters, not each broken piece.
    """
    width = height = WORK
    data = list(mask.getdata())
    bridged = list(mask.filter(ImageFilter.MaxFilter(BRIDGE)).getdata())
    pixels = list(rgb.getdata())
    seen = bytearray(width * height)
    regions = []
    for start in range(width * height):
        if not data[start] or seen[start]:
            continue
        queue = deque([start])
        seen[start] = 1
        count = 0
        x0 = y0 = 10 ** 9
        x1 = y1 = -1
        total = [0, 0, 0]
        while queue:
            index = queue.popleft()
            y, x = divmod(index, width)
            if data[index]:
                count += 1
                x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
                p = pixels[index]
                total[0] += p[0]
                total[1] += p[1]
                total[2] += p[2]
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1),
                           (x + 1, y + 1), (x - 1, y - 1), (x + 1, y - 1), (x - 1, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    n = ny * width + nx
                    if bridged[n] and not seen[n]:
                        seen[n] = 1
                        queue.append(n)
        frac = count / area
        if frac < template_policy.NOISE_AREA:
            continue
        colour = "#%02X%02X%02X" % tuple(round(c / count) for c in total)
        box = {"x": round(x0 / width, 4), "y": round(y0 / height, 4),
               "width": round((x1 - x0 + 1) / width, 4),
               "height": round((y1 - y0 + 1) / height, 4)}
        regions.append({
            "area": round(frac, 4), "box": box, "color": colour,
            "span": round(max(box["width"], box["height"]), 4),
        })
    regions.sort(key=lambda r: -r["area"])
    return regions
