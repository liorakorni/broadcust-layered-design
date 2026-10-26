"""Hide each editable component and see whether it was the only copy.

A call-to-action button, a contact circle or a pictogram that the editor can move
must be the only thing painting that element. If the cleaned photograph or the
overlay still carries a copy, hiding the component leaves the picture looking the
same - and so does moving it: the customer drags the button and finds another one
underneath.

The probe renders the document once, then again with each component hidden, and
measures how much of the component's own box changed. A box whose pixels barely
change was either invisible or backed by a second copy; both are defects of the
reconstruction, and neither is visible to a reviewer looking at one screenshot.

`render_png(html) -> bytes` is injected: the production renderer, a local browser
for a live check, or a fake in tests. PIL only.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops

from lib import assemble_html

#: Per-pixel summed channel difference above which a pixel counts as changed.
PIXEL_DELTA = 60
#: A component whose box changed by less than this share is not the only copy.
#: Text and icons ink only part of their box, so this is low; a solid shape changes
#: nearly all of it.
MIN_CHANGED = 0.04
#: An outline with no fill (a thin chevron, a ring) inks a hairline of its box: a
#: one-pixel diagonal stroke measured 3.6% there. A baked copy of it changes ~0%.
MIN_CHANGED_OUTLINE = 0.012
#: Components are small relative to the page, so each box is padded to catch the
#: edge of a shadow or an antialiased rim.
PAD = 2


def components(scene):
    """The elements an editor can move on their own, as (layerId, elementId, box, kind)."""
    out = []
    text_ids = assemble_html.element_ids(scene)
    for layer in scene.get("textLayers") or []:
        out.append((layer["id"], text_ids[layer["id"]], _box(layer), "text"))
    shape_ids = assemble_html.shape_element_ids(scene)
    for shape in scene.get("shapeLayers") or []:
        out.append((shape["id"], shape_ids[shape["id"]], _box(shape), "shape"))
    image_ids = assemble_html.image_element_ids(scene)
    for layer in scene.get("imageLayers") or []:
        if layer["type"] == "image":
            out.append((layer["id"], image_ids[layer["id"]], _box(layer), "icon"))
    return out


def _box(layer):
    return {k: int(layer[k]) for k in ("x", "y", "width", "height")}


def hide(html, element_id):
    """The same document with one element invisible but still taking its space."""
    rule = f'<style>#{element_id}{{visibility:hidden !important}}</style>'
    return rule + html


def probe(render_png, html, scene, canvas, *, only=None):
    """Hide each component in turn. Returns one record per component.

    {"layerId", "elementId", "kind", "changed": 0..1, "onlyCopy": bool}. `onlyCopy`
    False means hiding it left its own box looking the same.
    """
    baseline = _load(render_png(html))
    records = []
    hollow = {
        shape["id"] for shape in scene.get("shapeLayers") or []
        if not shape.get("color") and shape.get("borderColor")
        and shape.get("kind") != "gradient"
    }
    for layer_id, element_id, box, kind in components(scene):
        if only and layer_id not in only:
            continue
        hidden = _load(render_png(hide(html, element_id)))
        changed = changed_share(baseline, hidden, box, canvas)
        records.append({
            "layerId": layer_id, "elementId": element_id, "kind": kind,
            "changed": None if changed is None else round(changed, 4),
            "onlyCopy": changed is not None and changed >= (
                MIN_CHANGED_OUTLINE if layer_id in hollow else MIN_CHANGED),
        })
    return records


def baked_duplicates(records):
    """The components that hiding did not remove from the picture."""
    return [r for r in records if r["changed"] is not None and not r["onlyCopy"]]


def changed_share(a, b, box, canvas):
    """Share of `box` whose pixels differ between two renders, or None if empty."""
    sx = a.size[0] / float(canvas["width"])
    sy = a.size[1] / float(canvas["height"])
    crop = (max(0, int((box["x"] - PAD) * sx)), max(0, int((box["y"] - PAD) * sy)),
            min(a.size[0], int((box["x"] + box["width"] + PAD) * sx)),
            min(a.size[1], int((box["y"] + box["height"] + PAD) * sy)))
    if crop[2] - crop[0] < 1 or crop[3] - crop[1] < 1:
        return None
    diff = ImageChops.difference(a.crop(crop), b.crop(crop))
    red, green, blue = diff.split()
    total = ImageChops.add(ImageChops.add(red, green), blue)
    mask = total.point(lambda v: 255 if v > PIXEL_DELTA else 0)
    changed = mask.histogram()[255]
    return changed / float(mask.size[0] * mask.size[1])


def _load(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB").copy()
