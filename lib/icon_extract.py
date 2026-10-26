"""Cut a contact pictogram out of the approved concept, with no model call.

A phone or envelope symbol inside a flat coloured circle is the easiest thing in
the design to separate: it is a small number of colours on a known background. So
it is cut out arithmetically rather than regenerated - which keeps the approved
concept's own icon (a regenerated one is a different drawing), costs nothing, and
cannot fail halfway and leave the pictogram baked into the artwork.

The result is a transparent PNG placed as its own image layer, so it can be
selected, moved, resized, replaced and deleted independently. Editing its
internal strokes is not supported; that needs the editor's native icon
mechanism, which is deferred.
"""

from __future__ import annotations

import io

#: Per-channel distance from the background colour below which a pixel is the
#: background. Generous: the circle is flat, and its antialiased rim against the
#: glyph is what the rim clean-up below removes.
BACKGROUND_DISTANCE = 40

#: Margin kept around the glyph, as a fraction of its own size, so the pictogram
#: does not sit edge to edge in its layer.
MARGIN = 0.06

#: The extracted PNG is at least this multiple of the size it renders at, so it
#: stays sharp on the output canvas, and never larger than the cap.
MIN_SCALE = 2
MAX_SIDE = 512

#: The crop is grown by this fraction of the box on every side. The reported box
#: is the pictogram's own bounds, so without this there is barely any background
#: inside it: the dominant-colour estimate has nothing to find, and a glyph that
#: fills its box reads as "nothing was removed".
CROP_PAD = 0.15

#: An extraction that keeps almost nothing, or almost everything, did not find a
#: glyph: the crop was empty, or the background colour was wrong and the whole
#: badge survived.
MIN_GLYPH_FRACTION = 0.02
MAX_GLYPH_FRACTION = 0.85


def extract(concept_bytes, box, canvas, circle_color=None):
    """Return `(png_bytes, alpha_fraction)` for the pictogram inside `box`.

    `circle_color` is the colour of the circle behind it when the scene reports
    one; otherwise the dominant colour of the crop is used, which is the same
    thing whenever the glyph is smaller than its background.
    """
    from PIL import Image, ImageFilter

    with Image.open(io.BytesIO(concept_bytes)) as image:
        source = image.convert("RGB")
        crop_box = _scaled_box(box, canvas, source.size)
        if crop_box is None:
            return None, 0.0
        crop = source.crop(crop_box)

    background = _rgb(circle_color) if circle_color else _dominant(crop)
    alpha = Image.new("L", crop.size, 0)
    pixels = crop.load()
    mask = alpha.load()
    for x in range(crop.width):
        for y in range(crop.height):
            r, g, b = pixels[x, y]
            near = (
                abs(r - background[0]) <= BACKGROUND_DISTANCE
                and abs(g - background[1]) <= BACKGROUND_DISTANCE
                and abs(b - background[2]) <= BACKGROUND_DISTANCE
            )
            mask[x, y] = 0 if near else 255

    # MinFilter erodes the one-pixel rim where the circle meets the glyph, which
    # the distance test keeps as "not the background" and which would otherwise
    # outline the pictogram with a ring of circle colour.
    alpha = alpha.filter(ImageFilter.MinFilter(3))

    fraction = _fraction(alpha)
    bounds = alpha.getbbox()
    if not bounds or fraction <= 0:
        return None, fraction

    glyph = crop.convert("RGBA")
    glyph.putalpha(alpha)
    glyph = glyph.crop(_with_margin(bounds, alpha.size))
    glyph = _upscaled(glyph, box)

    buf = io.BytesIO()
    glyph.save(buf, format="PNG")
    return buf.getvalue(), fraction


def looks_like_a_glyph(alpha_fraction):
    """Whether an extraction plausibly found a pictogram rather than noise."""
    return MIN_GLYPH_FRACTION <= alpha_fraction <= MAX_GLYPH_FRACTION


def crop_png(concept_bytes, box, canvas):
    """The `box` of the concept as a PNG, for a model that must redraw it."""
    from PIL import Image

    with Image.open(io.BytesIO(concept_bytes)) as image:
        source = image.convert("RGB")
        crop_box = _scaled_box(box, canvas, source.size)
        if crop_box is None:
            return None
        crop = source.crop(crop_box)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


def glyph_fraction(png_bytes):
    """The opaque fraction of an RGBA PNG, or 0 when it has no alpha at all."""
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as image:
        if image.mode not in ("RGBA", "LA", "PA") and "transparency" not in image.info:
            return 1.0          # no alpha: a solid tile, never an isolated glyph
        alpha = image.convert("RGBA").getchannel("A")
    return _fraction(alpha)


def _fraction(alpha):
    histogram = alpha.histogram()
    total = sum(histogram) or 1
    return sum(histogram[128:]) / total


def _dominant(crop):
    """The modal colour of the crop, which for a badge is its circle."""
    reduced = crop.quantize(colors=8)
    palette = reduced.getpalette() or []
    counts = reduced.getcolors() or []
    if not counts:
        return (255, 255, 255)
    _, index = max(counts)
    return tuple(palette[index * 3: index * 3 + 3]) or (255, 255, 255)


def _scaled_box(box, canvas, size, pad=CROP_PAD):
    width, height = size
    scale_x = width / float(canvas["width"])
    scale_y = height / float(canvas["height"])
    pad_x = box["width"] * pad
    pad_y = box["height"] * pad
    left = max(0, int(round((box["x"] - pad_x) * scale_x)))
    top = max(0, int(round((box["y"] - pad_y) * scale_y)))
    right = min(width, int(round((box["x"] + box["width"] + pad_x) * scale_x)))
    bottom = min(height, int(round((box["y"] + box["height"] + pad_y) * scale_y)))
    if right - left < 4 or bottom - top < 4:
        return None
    return (left, top, right, bottom)


def _with_margin(bounds, size):
    left, top, right, bottom = bounds
    pad_x = max(1, round((right - left) * MARGIN))
    pad_y = max(1, round((bottom - top) * MARGIN))
    return (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(size[0], right + pad_x),
        min(size[1], bottom + pad_y),
    )


def _upscaled(glyph, box):
    """Resize so the pictogram stays sharp at the size it is rendered."""
    from PIL import Image

    target = max(1, min(box["width"], box["height"])) * MIN_SCALE
    shorter = max(1, min(glyph.size))
    if shorter >= target:
        return glyph
    factor = min(target / shorter, MAX_SIDE / max(glyph.size))
    if factor <= 1:
        return glyph
    size = (max(1, round(glyph.width * factor)), max(1, round(glyph.height * factor)))
    return glyph.resize(size, Image.LANCZOS)


def _rgb(value):
    digits = str(value).lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) != 6:
        return (255, 255, 255)
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
