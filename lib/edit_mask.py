"""The mask that locks the visible photograph while CLEAN_PHOTO repaints around it.

Asking the image model to "remove the graphics and reconstruct the photograph"
lets it re-compose the frame: against the stored concept every such photograph
came back zoomed 1.09-1.25x and shifted 90-270 px, so the stacked layers no
longer reproduced the approved design. With a mask the model repaints only the
masked pixels and the rest are kept, and the same measurement gave scale 1.00 and
a shift of 0.1 px.

What must be repainted is exactly what is not the photograph in the concept:

* every pixel the validated decoration overlay draws (panels, frames, ornaments,
  badges, soft edges), grown slightly so a glow or antialiased edge is covered;
* every text box, since the overlay deliberately contains no text;
* every independent inset, which is placed as its own layer on top.

Everything else is the real photograph and stays.
"""

from __future__ import annotations

import io

from lib import validate_assets

#: Overlay pixels at or above this alpha are artwork rather than photo window.
ARTWORK_ALPHA = validate_assets.TRANSPARENT_THRESHOLD

#: How far the artwork is grown, as a fraction of the image width. Covers soft
#: glows and antialiased edges that the alpha cut-off alone would leave behind.
GROW_FRACTION = 0.0125

#: Text and inset boxes are padded by this fraction of the image width, because
#: a model-reported box is a little tighter than the glyphs it describes.
PAD_FRACTION = 0.006


def build_clean_photo_mask(size, overlay_bytes, scene):
    """Return `(png_bytes, edit_fraction)` for a concept of `size` (w, h).

    The PNG follows the provider convention: alpha 0 marks pixels to repaint,
    alpha 255 marks pixels to keep. `edit_fraction` is the repainted share of the
    image, kept for logging and tests.
    """
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    overlay, _ = validate_assets.load_rgba(overlay_bytes)
    if overlay.size != size:
        overlay = overlay.resize(size, Image.LANCZOS)

    artwork = overlay.getchannel("A").point(
        lambda a: 255 if a >= ARTWORK_ALPHA else 0
    )
    # MaxFilter's size is the window width and must be odd: 13 px at 1024.
    grow = max(3, round(width * GROW_FRACTION) | 1)
    artwork = artwork.filter(ImageFilter.MaxFilter(grow))

    canvas = scene["canvas"]
    sx = width / canvas["width"]
    sy = height / canvas["height"]
    pad = width * PAD_FRACTION
    draw = ImageDraw.Draw(artwork)
    for layer in _covered_layers(scene):
        draw.rectangle(
            [
                layer["x"] * sx - pad,
                layer["y"] * sy - pad,
                (layer["x"] + layer["width"]) * sx + pad,
                (layer["y"] + layer["height"]) * sy + pad,
            ],
            fill=255,
        )

    keep = artwork.point(lambda a: 0 if a else 255)
    mask = Image.new("RGBA", size, (0, 0, 0, 255))
    mask.putalpha(keep)

    histogram = artwork.histogram()
    edit_fraction = histogram[255] / (sum(histogram) or 1)
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue(), edit_fraction


def _covered_layers(scene):
    """Layers that sit on top of the photograph and so must not be in it."""
    for layer in scene.get("textLayers") or []:
        yield layer
    for layer in scene.get("imageLayers") or []:
        # Independent imagery, including each contact pictogram, is placed as its
        # own layer on top, so its original copy has to go.
        if layer.get("type") == "image":
            yield layer
    for layer in scene.get("shapeLayers") or []:
        # An opaque simple shape - a CTA panel, a contact circle - is drawn in
        # CSS, so the photograph underneath it must be reconstructed; otherwise
        # moving the CSS panel reveals the baked one. A readability gradient is
        # deliberately excluded: it is translucent, the photograph is meant to
        # show through it, and repainting that much of the frame is what lets the
        # model re-compose the picture.
        if layer.get("kind") != "gradient":
            yield layer


# --------------------------------------------------------------------------
# Concept correction
# --------------------------------------------------------------------------

#: Regions to correct are padded by this fraction of the image width. A planned
#: box is where a line of text is meant to sit, not the glyph bounds, and a mask
#: that clips the first or last letter would produce half a word.
REGION_PAD_FRACTION = 0.012

#: The seam between repainted and kept pixels is feathered by this fraction of
#: the image width, so the correction does not leave a hard rectangle behind.
FEATHER_FRACTION = 0.004


def build_region_mask(size, boxes, pad_fraction=REGION_PAD_FRACTION):
    """Return `(png_bytes, edit_fraction)` repainting only the given boxes.

    `boxes` are normalized `{x, y, width, height}` dicts. Same convention as the
    clean-photo mask: alpha 0 marks pixels the model may repaint, alpha 255 marks
    pixels it must keep. Everything outside the boxes is locked, which is what
    stops a "just add the business name" correction from also changing the photo.
    """
    from PIL import Image, ImageDraw

    width, height = size
    pad = width * pad_fraction
    edit = Image.new("L", size, 0)
    draw = ImageDraw.Draw(edit)
    for box in boxes:
        draw.rectangle(
            [
                max(0, box["x"] * width - pad),
                max(0, box["y"] * height - pad),
                min(width, (box["x"] + box["width"]) * width + pad),
                min(height, (box["y"] + box["height"]) * height + pad),
            ],
            fill=255,
        )
    keep = edit.point(lambda a: 0 if a else 255)
    mask = Image.new("RGBA", size, (0, 0, 0, 255))
    mask.putalpha(keep)
    histogram = edit.histogram()
    edit_fraction = histogram[255] / (sum(histogram) or 1)
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue(), edit_fraction


def preserve_outside(base_bytes, result_bytes, mask_bytes):
    """Return the result with every kept pixel taken from the base, as PNG bytes.

    A mask asks the provider to leave pixels alone; this makes sure it did. The
    model's output is used only inside the repaint region (feathered at its
    edge), so everything the correction was not meant to touch is the base,
    byte for byte, whatever the provider returned.
    """
    from PIL import Image, ImageChops, ImageFilter

    with Image.open(io.BytesIO(base_bytes)) as base_img:
        base = base_img.convert("RGB")
    with Image.open(io.BytesIO(result_bytes)) as result_img:
        result = result_img.convert("RGB")
    with Image.open(io.BytesIO(mask_bytes)) as mask_img:
        keep = mask_img.convert("RGBA").getchannel("A")
    if result.size != base.size:
        result = result.resize(base.size, Image.LANCZOS)
    if keep.size != base.size:
        keep = keep.resize(base.size, Image.NEAREST)

    repaint = keep.point(lambda a: 255 if a < 128 else 0)
    feather = max(1, round(base.size[0] * FEATHER_FRACTION))
    # Blur for a soft seam, then clip to the repaint region: the blend can fade in
    # from the region's edge, but it never reaches a pixel the mask kept.
    soft = ImageChops.darker(repaint.filter(ImageFilter.GaussianBlur(feather)), repaint)
    out = Image.composite(result, base, soft)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def outside_difference(base_bytes, other_bytes, mask_bytes):
    """Mean absolute difference (0..255) over the pixels the mask keeps.

    A measurement, not a gate: tests and the correction record use it to show
    what was actually preserved rather than what the prompt asked for.
    """
    from PIL import Image, ImageChops

    with Image.open(io.BytesIO(base_bytes)) as a_img:
        a = a_img.convert("RGB")
    with Image.open(io.BytesIO(other_bytes)) as b_img:
        b = b_img.convert("RGB")
    with Image.open(io.BytesIO(mask_bytes)) as m_img:
        keep = m_img.convert("RGBA").getchannel("A")
    if b.size != a.size:
        b = b.resize(a.size, Image.LANCZOS)
    diff = ImageChops.difference(a, b).convert("L")
    kept = keep.point(lambda v: 255 if v >= 128 else 0)
    count = kept.histogram()[255]
    if not count:
        return 0.0
    hist = Image.composite(diff, Image.new("L", a.size, 0), kept).histogram()
    return sum(i * c for i, c in enumerate(hist)) / count
