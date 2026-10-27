"""Coordinate spaces: the canvas, the image the provider actually saw, and the unit square.

The scene is written in canvas pixels (1080x1080 for a square post). The image
provider is shown, and returns, a 1024-wide image for a square canvas, and the
approved concept is stored at that size. A box quoted to the model in canvas pixels
therefore points at the wrong pixels of the image the model is looking at - by up to
5.5% of the width at the far edge, which is the size of the ornaments being asked
about. Every place that talks about a region to the model, builds a mask, or compares
two images converts through here instead of reusing numbers from another space.

All conversions scale each axis independently (a canvas and a generation size can
differ in aspect ratio) and clamp to the destination, never past it.
"""

from __future__ import annotations

import io


def image_size(data):
    """(width, height) of encoded image bytes."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.width, image.height


def scale_box(box, source, target):
    """A {x, y, width, height} box moved from `source` (w, h) space into `target` (w, h)."""
    sx = target[0] / float(source[0])
    sy = target[1] / float(source[1])
    x0 = max(0, min(target[0], round(box["x"] * sx)))
    y0 = max(0, min(target[1], round(box["y"] * sy)))
    x1 = max(0, min(target[0], round((box["x"] + box["width"]) * sx)))
    y1 = max(0, min(target[1], round((box["y"] + box["height"]) * sy)))
    return {"x": x0, "y": y0, "width": max(0, x1 - x0), "height": max(0, y1 - y0)}


def canvas_to_image(box, canvas, size):
    """A canvas-pixel box in the pixel space of an image of `size` (w, h)."""
    return scale_box(box, (canvas["width"], canvas["height"]), size)


def image_to_canvas(box, size, canvas):
    return scale_box(box, size, (canvas["width"], canvas["height"]))


def normalize(box, canvas):
    """A canvas-pixel box as 0..1 fractions of the canvas."""
    w, h = float(canvas["width"]), float(canvas["height"])
    return {
        "x": round(box["x"] / w, 4), "y": round(box["y"] / h, 4),
        "width": round(box["width"] / w, 4), "height": round(box["height"] / h, 4),
    }


def denormalize(box, canvas):
    w, h = canvas["width"], canvas["height"]
    return {
        "x": round(box["x"] * w), "y": round(box["y"] * h),
        "width": round(box["width"] * w), "height": round(box["height"] * h),
    }
