"""Check the typography that actually rendered, against the concept.

Font family, weight, size, tracking, line height, stroke width and box size are one
problem: change any of them and the lettering the customer approved changes with it.
Outlined headlines are where that bites hardest - a stroke that is a little too heavy
for its size closes the counters and merges the letters until the "hollow" headline
is a solid blob, and a font that quietly fell back to another face has different
widths, so the headline's footprint and the hierarchy it anchors both move.

Everything here is measured on the real render, in code. It never changes the design:
it reports what is wrong, and a defect that cannot be fixed within the bounded repair
is delivered with the finding attached, never hidden by switching the headline to solid
text.
"""

from __future__ import annotations

import io

from PIL import Image

import conf
from lib import alignment

#: An outline whose rendered ink fills more than this share of its own rectangle,
#: and well above the concept's own share, has lost its hollow interior.
FILLED_COVERAGE = 0.45
FILLED_RATIO = 1.6
#: Rendered width or height within this ratio of the concept's is the same footprint.
FOOTPRINT = (0.8, 1.25)
#: Below this many pixels at feed width a stroke is a hairline that vanishes.
FEED_MIN_STROKE_PX = 1.0

WARNING = "warning"
BLOCKING = "blocking"


def check(scene, concept_bytes, native_bytes, canvas, *, platform_fonts=None, feed_width=360):
    """Problems measured from the render, as render-problem dicts."""
    from lib import assemble_html

    element_of = assemble_html.element_ids(scene)
    problems = []
    problems.extend(_fonts(scene, platform_fonts, element_of))
    problems.extend(_feed_strokes(scene, canvas, feed_width, element_of))
    if concept_bytes and native_bytes:
        problems.extend(_outlines(scene, concept_bytes, native_bytes, canvas, element_of))
    return problems


def _problem(code, severity, element, message, **measured):
    return {"code": code, "severity": severity, "layerId": element,
            "message": message, "measured": measured}


def _fonts(scene, platform_fonts, element_of):
    """A face other than the one requested drew some of the copy."""
    if not platform_fonts:
        return []
    out = []
    for layer in scene.get("textLayers") or []:
        used = platform_fonts.get(element_of[layer["id"]])
        if not used:
            continue
        wanted = layer["fontFamily"].strip().lower()
        strays = [f for f in used
                  if f.get("glyphCount", 0) > 0
                  and (f.get("familyName") or "").strip().lower() != wanted]
        if not strays:
            continue
        drawn = ", ".join(sorted({f.get("familyName") or "an unknown face" for f in strays}))
        measured = {"requested": layer["fontFamily"],
                    "used": [{"family": f.get("familyName"), "glyphs": f.get("glyphCount")}
                             for f in used]}
        if wanted not in {f.lower() for f in conf.RENDER_FONT_FAMILIES}:
            # A system face the renderer does not carry (Arial): it draws a
            # stand-in here, and the customer's own editor has the real one. The
            # design asks for what was approved, so nothing in it is wrong.
            out.append(_problem(
                "FONT_STANDIN_IN_PREVIEW", WARNING, element_of[layer["id"]],
                f"{layer['id']} asks for {layer['fontFamily']}, which the preview "
                f"renderer does not have; {drawn} stood in for it in the preview only.",
                **measured))
            continue
        out.append(_problem(
            "FONT_FALLBACK", BLOCKING, element_of[layer["id"]],
            f"{layer['id']} asked for {layer['fontFamily']} but {drawn} drew some of it; "
            "widths, weight and the headline's footprint are not the approved lettering.",
            **measured))
    return out


def _feed_strokes(scene, canvas, feed_width, element_of):
    scale = feed_width / float(canvas["width"])
    out = []
    for layer in scene.get("textLayers") or []:
        stroke = float(layer.get("strokeWidth") or 0)
        if layer.get("fill", "solid") != "transparent" or stroke <= 0:
            continue
        if stroke * scale < FEED_MIN_STROKE_PX:
            out.append(_problem(
                "OUTLINE_THIN_AT_FEED", WARNING, element_of[layer["id"]],
                f"The {stroke:g}px outline of {layer['id']} is "
                f"{stroke * scale:.2f}px at feed width and will vanish there.",
                strokePx=stroke, feedPx=round(stroke * scale, 2),
            ))
    return out


def _outlines(scene, concept_bytes, native_bytes, canvas, element_of):
    out = []
    with Image.open(io.BytesIO(concept_bytes)) as c, Image.open(io.BytesIO(native_bytes)) as n:
        concept, native = c.convert("RGB"), n.convert("RGB")
        for layer in scene.get("textLayers") or []:
            if layer.get("fill", "solid") != "transparent":
                continue
            want = alignment.measure_ink(concept, layer, canvas)
            got = alignment.measure_ink(native, layer, canvas)
            if not want or not got:
                continue
            element = element_of[layer["id"]]
            if (got["coverage"] > FILLED_COVERAGE
                    and got["coverage"] > FILLED_RATIO * max(want["coverage"], 0.05)):
                out.append(_problem(
                    "OUTLINE_FILLED", BLOCKING, element,
                    f"The outlined lettering of {layer['id']} renders "
                    f"{got['coverage']:.0%} filled against {want['coverage']:.0%} in the "
                    "concept: the stroke is closing the counters or merging letters.",
                    rendered=got["coverage"], concept=want["coverage"],
                ))
            width = (got["right"] - got["left"]) / float(max(1, want["right"] - want["left"]))
            height = (got["bottom"] - got["top"]) / float(max(1, want["bottom"] - want["top"]))
            if not (FOOTPRINT[0] <= width <= FOOTPRINT[1]
                    and FOOTPRINT[0] <= height <= FOOTPRINT[1]):
                out.append(_problem(
                    "TYPOGRAPHY_FOOTPRINT", WARNING, element,
                    f"{layer['id']} renders {width:.0%} of the concept's width and "
                    f"{height:.0%} of its height.",
                    widthRatio=round(width, 3), heightRatio=round(height, 3),
                ))
    return out
