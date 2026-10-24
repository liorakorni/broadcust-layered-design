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
        if layer.get("type") == "image":
            yield layer
