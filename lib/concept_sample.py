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


def differs(reported, sampled, tolerance=TOLERANCE):
    """True when two "#RRGGBB" colours are further apart than `tolerance`."""
    if not reported or not sampled:
        return False
    try:
        a, b = _rgb(reported), _rgb(sampled)
    except ValueError:
        return False
    return any(abs(x - y) > tolerance for x, y in zip(a, b))


def correct_shape_colors(image_bytes, scene):
    """Replace shape colours that disagree with the concept's own pixels.

    Mutates `scene` in place and returns one record per correction, so the stored
    scene says what was changed and why rather than quietly differing from what
    the model reported.
    """
    canvas = scene["canvas"]
    corrections = []
    for shape in scene.get("shapeLayers") or []:
        box = {k: shape[k] for k in ("x", "y", "width", "height")}
        sampled = median_color(image_bytes, box, canvas)
        if sampled is None:
            continue
        for field in ("color", "borderColor"):
            reported = shape.get(field)
            if not reported or not differs(reported, sampled):
                continue
            shape[field] = sampled
            corrections.append(
                {"layerId": shape["id"], "property": field,
                 "reported": reported, "sampled": sampled}
            )
    return corrections


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
