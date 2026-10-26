"""Read colours back off the approved concept.

A model reports the colour of a panel it can see, and it is usually close but not
exact - close enough that a CTA button comes out a slightly different yellow from
the one the customer approved. The pixels are right there, so the colour is
measured rather than trusted.

Only colour. Geometry is deliberately left alone: a box that is wrong by a few
pixels is visible to REVIEW and routed to a cheap, image-free layout repair,
whereas snapping boxes to colour regions here would move elements the concept
placed deliberately.
"""

from __future__ import annotations

import io
import math

from PIL import Image

#: How far off a reported colour may be, per channel, before it is replaced.
#: Wide enough to tolerate JPEG-ish noise and antialiasing on the sampled edge,
#: narrow enough that a visibly different colour is corrected.
TOLERANCE = 24

#: The fraction of the box that is sampled, centred. A panel's own edge is
#: antialiased against whatever is behind it, and a circle's corners are not the
#: circle at all, so only the middle is representative.
SAMPLE_FRACTION = 0.6


def median_color(image_bytes, box, canvas):
    """The median RGB of the middle of `box`, as "#RRGGBB", or None.

    The median rather than the mean: a mean slides towards a dark icon or a
    lighter highlight inside the shape, while the median stays on the colour that
    actually covers most of it.
    """
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        crop = _central_box(box, canvas, image.size)
        if crop is None:
            return None
        region = image.crop(crop)
        pixels = list(region.getdata())

    if not pixels:
        return None
    channels = [sorted(channel) for channel in zip(*pixels)]
    middle = len(pixels) // 2
    return "#" + "".join(f"{channel[middle]:02X}" for channel in channels)


#: Per-channel standard deviation below which an area is flat enough to be a
#: solid shape rather than photography. Repainted photography keeps grain,
#: gradients and detail; a baked panel or circle is one colour. Measured against
#: live runs where a dark circle's colour matched dark photography behind it.
FLAT_SPREAD = 10.0


def color_spread(image_bytes, box, canvas):
    """Mean per-channel standard deviation in the middle of `box`, or None.

    How varied the pixels are: near zero for a solid fill, well above it for a
    photograph. This is what separates "the panel is still painted here" from
    "this part of the photograph happens to be that colour".
    """
    from PIL import ImageStat

    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        crop = _central_box(box, canvas, image.size)
        if crop is None:
            return None
        stat = ImageStat.Stat(image.crop(crop))
    return sum(stat.stddev) / len(stat.stddev)


#: Largest side of the working image a shape is sampled from. Nearest-neighbour
#: reduction keeps real colours (no blending at the edges) and keeps the loops
#: below to a few thousand pixels however large the panel is.
SAMPLE_SIDE = 48

#: Where a circle's own colour is found, as a fraction of its radius. Inside this
#: ring sits the pictogram; outside it is antialiasing and whatever the circle
#: lies on.
CIRCLE_RING = (0.62, 0.90)
#: Where a panel's own colour is found, as a fraction of its shorter side,
#: measured inward from its (rounded) edge: clear of the border and shadow, and
#: clear of the words that sit in the middle.
PANEL_BAND = (0.08, 0.24)
#: A sample is trusted when at least this share of it is within COVERAGE_TOLERANCE
#: of its median - that is what a flat fill looks like.
MIN_COVERAGE = 0.6
COVERAGE_TOLERANCE = 32


def fill_color(image_bytes, shape, canvas):
    """The fill of a panel, circle or dot, read where only the fill is.

    Returns {"color": "#RRGGBB", "coverage": 0..1, "samples": n} or None.

    Not the middle of the box: that is where the pictogram sits in a circle and
    the words sit in a button, and a median over it landed on the icon's colour
    (a yellow circle sampled as near-black). A circle is read on a ring between its
    pictogram and its rim, a panel on a band just inside its edge.
    """
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        scale_x = image.size[0] / float(canvas["width"])
        scale_y = image.size[1] / float(canvas["height"])
        left, top = shape["x"] * scale_x, shape["y"] * scale_y
        width, height = shape["width"] * scale_x, shape["height"] * scale_y
        crop = (max(0, int(left)), max(0, int(top)),
                min(image.size[0], int(left + width) + 1),
                min(image.size[1], int(top + height) + 1))
        if crop[2] - crop[0] < 2 or crop[3] - crop[1] < 2:
            return None
        region = image.crop(crop)
    longest = max(region.size)
    if longest > SAMPLE_SIDE:
        ratio = SAMPLE_SIDE / float(longest)
        region = region.resize(
            (max(2, round(region.size[0] * ratio)), max(2, round(region.size[1] * ratio))),
            Image.NEAREST)
    pixels = region.load()
    w, h = region.size
    kind = shape.get("kind")
    # Geometry in the cropped, reduced image's own pixels.
    cx, cy = w / 2.0, h / 2.0
    radius = max(1.0, shape.get("radius", 0) * (w / max(1.0, width)))
    chosen = []
    for y in range(h):
        for x in range(w):
            px, py = x + 0.5, y + 0.5
            if kind in ("circle", "dot", "ring"):
                reach = math.hypot((px - cx) / (w / 2.0), (py - cy) / (h / 2.0))
                low, high = CIRCLE_RING if kind != "dot" else (0.0, 0.75)
                if low <= reach <= high:
                    chosen.append(pixels[x, y])
            else:
                depth = _inside_depth(px, py, w, h, min(radius, min(w, h) / 2.0))
                short = min(w, h)
                if PANEL_BAND[0] * short <= depth <= PANEL_BAND[1] * short:
                    chosen.append(pixels[x, y])
    if not chosen:
        return None
    channels = [sorted(channel) for channel in zip(*chosen)]
    middle = len(chosen) // 2
    median = tuple(channel[middle] for channel in channels)
    near = sum(
        1 for pixel in chosen
        if all(abs(a - b) <= COVERAGE_TOLERANCE for a, b in zip(pixel, median))
    )
    spreads = []
    for channel in zip(*chosen):
        mean = sum(channel) / len(channel)
        spreads.append((sum((v - mean) ** 2 for v in channel) / len(channel)) ** 0.5)
    return {
        "color": "#" + "".join(f"{v:02X}" for v in median),
        "coverage": round(near / len(chosen), 3),
        #: Mean per-channel standard deviation: near zero for a painted fill, well
        #: above FLAT_SPREAD for photography, however dark.
        "spread": round(sum(spreads) / len(spreads), 2),
        "samples": len(chosen),
    }


def _inside_depth(px, py, width, height, radius):
    """How far inside a rounded rectangle a point is (negative outside)."""
    qx = abs(px - width / 2.0) - (width / 2.0 - radius)
    qy = abs(py - height / 2.0) - (height / 2.0 - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return -(outside + inside - radius)


def _luma(rgb):
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def _chroma(rgb):
    return (max(rgb) - min(rgb)) / 255.0


def looks_like_an_icon_or_shadow(reported, sampled):
    """A dark, low-chroma sample standing in for a plausible bright colour.

    That combination is what a pictogram, a border or a drop shadow looks like
    when it is read as a fill. It is never trusted over a colour the model gave
    that is brighter and more saturated: the sampler is there to refine a close
    colour, not to replace a plausible one with the darkest thing nearby.
    """
    try:
        a, b = _rgb(reported), _rgb(sampled)
    except ValueError:
        return False
    return (_luma(b) < 0.25 and _chroma(b) < 0.25
            and _luma(a) > _luma(b) + 0.25)


def differs(reported, sampled, tolerance=TOLERANCE):
    """True when two "#RRGGBB" colours are further apart than `tolerance`."""
    if not reported or not sampled:
        return False
    try:
        a, b = _rgb(reported), _rgb(sampled)
    except ValueError:
        return False
    return any(abs(x - y) > tolerance for x, y in zip(a, b))


#: The shape kinds whose `color` is a fill to be read off the concept. A ring or a
#: border carries its colour in `borderColor`, which a fill sample says nothing
#: about, and a gradient has stops.
FILL_KINDS = ("panel", "circle", "dot")


def correct_shape_colors(image_bytes, scene):
    """Replace shape colours that disagree with the concept's own pixels.

    Mutates `scene` in place and returns one record per correction, so the stored
    scene says what was changed and why rather than quietly differing from what
    the model reported. `measure_shape_colors` also returns what was ignored.
    """
    return measure_shape_colors(image_bytes, scene)["corrections"]


def measure_shape_colors(image_bytes, scene):
    """Correct fills from the concept, and say what was left alone and why.

    {"corrections": [...], "ignored": [...]}. A sample is ignored - the reported
    colour stands - when the area is not flat enough to be a fill (coverage), or
    when it looks like the pictogram, border or shadow rather than the fill.
    """
    canvas = scene["canvas"]
    corrections, ignored = [], []
    for shape in scene.get("shapeLayers") or []:
        if shape.get("kind") not in FILL_KINDS or not shape.get("color"):
            continue
        sample = fill_color(image_bytes, shape, canvas)
        if sample is None:
            continue
        reported, sampled = shape["color"], sample["color"]
        if not differs(reported, sampled):
            continue
        record = {"layerId": shape["id"], "property": "color",
                  "reported": reported, "sampled": sampled,
                  "coverage": sample["coverage"]}
        if sample["coverage"] < MIN_COVERAGE:
            ignored.append({**record, "reason": "the sampled area is not a flat fill"})
        elif looks_like_an_icon_or_shadow(reported, sampled):
            ignored.append({**record,
                            "reason": "the sample looks like an icon, border or shadow"})
        else:
            shape["color"] = sampled
            corrections.append({k: record[k] for k in
                                ("layerId", "property", "reported", "sampled")})
    return {"corrections": corrections, "ignored": ignored}


def _central_box(box, canvas, size):
    """The middle `SAMPLE_FRACTION` of `box`, scaled to the image, or None."""
    width, height = size
    scale_x = width / float(canvas["width"])
    scale_y = height / float(canvas["height"])
    left = box["x"] * scale_x
    top = box["y"] * scale_y
    span_x = box["width"] * scale_x
    span_y = box["height"] * scale_y
    inset_x = span_x * (1 - SAMPLE_FRACTION) / 2
    inset_y = span_y * (1 - SAMPLE_FRACTION) / 2

    crop = (
        max(0, int(round(left + inset_x))),
        max(0, int(round(top + inset_y))),
        min(width, int(round(left + span_x - inset_x))),
        min(height, int(round(top + span_y - inset_y))),
    )
    if crop[2] - crop[0] < 1 or crop[3] - crop[1] < 1:
        return None
    return crop


def _rgb(value):
    digits = value.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
