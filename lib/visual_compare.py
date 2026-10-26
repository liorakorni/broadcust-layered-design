"""Measuring how different two renders are, so a verdict can rest on pixels.

A reviewer's words are one source of evidence about a repair; the pictures are
another, and the second is cheap and does not drift between calls. This module
answers questions of the form "how far is this render from the approved concept in
that region?" and "did anything in that region change at all?".

Everything is a mean absolute luminance-and-colour difference on a reduced image,
normalised to 0..1, so it is comparable between canvases and cheap. Tolerances are
fractions of the whole range, not pixel counts, and regions are given in canvas
pixels and scaled, so nothing here depends on the canvas size.

It judges similarity, not quality: a render can be far from the concept and good,
or close and bad. It is used only to compare candidates against the same concept,
or a candidate against its own previous version - never as a pass mark.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops, ImageStat

#: Longest side the images are reduced to before comparing.
WORK_SIZE = 256


def _load(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB").copy()


def _prepare(image, size):
    return image.resize(size, Image.BILINEAR)


def _region_crop(box, canvas, size):
    """A canvas-pixel box as a crop of an image of `size`, or None if empty."""
    sx = size[0] / float(canvas["width"])
    sy = size[1] / float(canvas["height"])
    left = max(0, int(box["x"] * sx))
    top = max(0, int(box["y"] * sy))
    right = min(size[0], int(round((box["x"] + box["width"]) * sx)))
    bottom = min(size[1], int(round((box["y"] + box["height"]) * sy)))
    if right - left < 1 or bottom - top < 1:
        return None
    return left, top, right, bottom


def _work_size(canvas):
    scale = WORK_SIZE / float(max(canvas["width"], canvas["height"]))
    return max(1, round(canvas["width"] * scale)), max(1, round(canvas["height"] * scale))


def distance(a_bytes, b_bytes, canvas, box=None):
    """Mean absolute difference between two images, 0 (same) to 1, over `box`.

    `box` is `{x, y, width, height}` in canvas pixels, or None for the whole
    image. An empty region returns None rather than a misleading 0.
    """
    size = _work_size(canvas)
    a = _prepare(_load(a_bytes), size)
    b = _prepare(_load(b_bytes), size)
    if box is not None:
        crop = _region_crop(box, canvas, size)
        if crop is None:
            return None
        a, b = a.crop(crop), b.crop(crop)
    difference = ImageChops.difference(a, b)
    return sum(ImageStat.Stat(difference).mean) / (3 * 255.0)


def brightness(image_bytes, canvas, box=None):
    """Mean luminance 0..1 over `box` (or the whole image)."""
    size = _work_size(canvas)
    image = _prepare(_load(image_bytes), size).convert("L")
    if box is not None:
        crop = _region_crop(box, canvas, size)
        if crop is None:
            return None
        image = image.crop(crop)
    return ImageStat.Stat(image).mean[0] / 255.0


def union(boxes):
    """The smallest box holding every box in `boxes`, or None when there are none."""
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    left = min(b["x"] for b in boxes)
    top = min(b["y"] for b in boxes)
    right = max(b["x"] + b["width"] for b in boxes)
    bottom = max(b["y"] + b["height"] for b in boxes)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def clamp_box(box, canvas):
    """`box` limited to the canvas, or None if nothing of it is inside."""
    if not box:
        return None
    left = max(0.0, float(box["x"]))
    top = max(0.0, float(box["y"]))
    right = min(float(canvas["width"]), float(box["x"]) + float(box["width"]))
    bottom = min(float(canvas["height"]), float(box["y"]) + float(box["height"]))
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}
