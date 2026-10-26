"""Alignment relationships between text layers: measured, kept, and applied.

A model reads each text box off the concept separately, so a column the concept set
flush on one edge comes back with edges a few pixels apart, and a later change to
one box's width moves its edge. What the concept actually shows is a relationship -
"these lines share a right edge" - so that is what is recorded:

* `detect_groups` measures where each layer's ink really starts and ends in the
  concept and groups layers whose edges sit within a tolerance of one another
  (relative to the canvas). A layer that is off the line on purpose, or centred, is
  not forced onto it.
* `apply` translates the groups into box positions, deterministically, at assembly.
  Boxes keep their own widths; only the position that makes the edge shared is set.

The direction of a layer is never touched: a phone number stays left-to-right inside
a right-to-left design. PIL only.
"""

from __future__ import annotations

import copy
import io

from PIL import Image

#: Edges closer than this share a line, as a fraction of the canvas width. About
#: 13px at 1080: wider than measurement noise and a model's box rounding, narrower
#: than a deliberate indent.
CLUSTER = 0.012
#: How far past a layer's box on its aligned side ink may be looked for, as a
#: fraction of the canvas width. Small on purpose: a model's box is rarely exact,
#: but anything further out is more likely a neighbour (a pictogram, the subject of
#: the photograph) than this text.
REACH = 0.004
#: Contrast, in grey levels, against the local background for a pixel to be ink.
MIN_CONTRAST = 70


def tolerance(canvas):
    """The distance within which two edges are the same edge."""
    return max(3, round(canvas["width"] * CLUSTER))


# --- measuring ---------------------------------------------------------------------


def measure_ink(image, layer, canvas):
    """The edges of `layer`'s ink in the concept, in canvas px, or None.

    `image` is an open PIL image of the concept. None when the layer cannot be
    measured reliably: nothing contrasting inside the box, or ink that runs into
    the edge of the search area (a neighbouring element, not this text).
    """
    scale_x = image.size[0] / float(canvas["width"])
    scale_y = image.size[1] / float(canvas["height"])
    reach = round(REACH * canvas["width"])
    align = layer.get("align", "right")
    left = layer["x"] - (reach if align in ("left", "center") else 0)
    right = layer["x"] + layer["width"] + (reach if align in ("right", "center") else 0)
    left, right = max(0, left), min(canvas["width"], right)
    top, bottom = layer["y"], layer["y"] + layer["height"]
    crop = (int(left * scale_x), int(top * scale_y), int(right * scale_x), int(bottom * scale_y))
    if crop[2] - crop[0] < 4 or crop[3] - crop[1] < 4:
        return None

    region = image.crop(crop).convert("L")
    width, height = region.size
    pixels = region.load()
    # The background is what the sides of the search area look like. Not the top and
    # bottom rows: a line that fills its box is ink all along them. A flush edge is
    # ink for part of one side only, which the median of both sides outvotes.
    frame = [pixels[x, y] for y in range(height) for x in (0, 1, width - 2, width - 1)
             if 0 <= x < width]
    values = sorted(frame)
    background = values[len(values) // 2]

    columns, rows = _ink_counts(pixels, width, height, background)
    inked = [x for x, n in enumerate(columns) if n >= max(2, 0.05 * height)]
    lines = [y for y, n in enumerate(rows) if n >= max(2, 0.03 * width)]
    if not inked or not lines:
        return None
    first, last = inked[0], inked[-1]
    area = (last - first + 1) * (lines[-1] - lines[0] + 1)
    return {
        "left": round(left + first / scale_x),
        "right": round(left + (last + 1) / scale_x),
        "top": round(top + lines[0] / scale_y),
        "bottom": round(top + (lines[-1] + 1) / scale_y),
        #: Share of the ink rectangle that is ink: low for hollow lettering, high
        #: for solid or merged letters.
        "coverage": round(sum(columns[first:last + 1]) / float(area), 3) if area else 0.0,
    }


def _ink_counts(pixels, width, height, background):
    columns = [0] * width
    rows = [0] * height
    for y in range(height):
        for x in range(width):
            if abs(pixels[x, y] - background) > MIN_CONTRAST:
                columns[x] += 1
                rows[y] += 1
    return columns, rows


def detect_groups(concept_bytes, scene):
    """Group the scene's text layers that share an edge in the concept.

    Returns the `alignmentGroups` list for the scene (empty when the concept does
    not support any). The evidence is the ink, not the declared `align`: a model
    that wrote "right" for a column the concept set flush left would otherwise have
    that column re-aligned to an edge the concept never had. A group needs two or
    more members on the same edge within `tolerance`; each layer joins the largest
    group it can (on a tie, the one its own `align` names), and a layer that fits
    none - an intentional offset, a lone line - is left exactly where it is.
    """
    canvas = scene["canvas"]
    tol = tolerance(canvas)
    # Copy set inside a button or badge is placed relative to that shape - centred in
    # it, or inset from its end - not against the column's edge. Moving it to the
    # column's anchor would push it off the shape's centre.
    backing = [s for s in scene.get("shapeLayers") or []
               if s.get("kind") in ("panel", "circle") and s.get("color")]
    layers = {
        layer["id"]: layer for layer in scene.get("textLayers") or []
        if not any(_centre_inside(layer, shape) for shape in backing)
    }
    with Image.open(io.BytesIO(concept_bytes)) as opened:
        image = opened.convert("RGB")
        ink = {
            layer_id: measure_ink(image, layer, canvas)
            for layer_id, layer in layers.items()
        }

    candidates = []
    for edge in ("right", "left", "center"):
        members = []
        for layer_id, box in ink.items():
            if box is None:
                continue
            # Centred layers are the only ones whose centre line is evidence: any
            # two lines of different widths can share a centre by chance.
            if edge == "center" and layers[layer_id].get("align") != "center":
                continue
            value = {"right": box["right"], "left": box["left"],
                     "center": round((box["left"] + box["right"]) / 2)}[edge]
            members.append((value, layer_id))
        members.sort()
        cluster = []
        for item in members + [None]:
            if item is not None and (not cluster or item[0] - cluster[0][0] <= tol):
                cluster.append(item)
                continue
            if len(cluster) >= 2:
                candidates.append((edge, cluster))
            cluster = [item] if item is not None else []

    def preference(candidate):
        edge, cluster = candidate
        declared = sum(1 for _, i in cluster if layers[i].get("align") == edge)
        return (-len(cluster), -declared, ("right", "left", "center").index(edge))

    groups, claimed = [], set()
    for edge, cluster in sorted(candidates, key=preference):
        free = [(value, i) for value, i in cluster if i not in claimed]
        if len(free) < 2:
            continue
        claimed.update(i for _, i in free)
        groups.append(_group(edge, len(groups) + 1,
                             [(value, layers[i], ink[i]) for value, i in free]))
    return groups


def _centre_inside(layer, shape):
    cx = layer["x"] + layer["width"] / 2.0
    cy = layer["y"] + layer["height"] / 2.0
    return (shape["x"] <= cx <= shape["x"] + shape["width"]
            and shape["y"] <= cy <= shape["y"] + shape["height"])


def _group(edge, number, cluster):
    values = sorted(value for value, _, _ in cluster)
    anchor = values[len(values) // 2]
    by_height = sorted(cluster, key=lambda item: item[2]["top"])
    gaps = [
        max(0, nxt[2]["top"] - prev[2]["bottom"])
        for prev, nxt in zip(by_height, by_height[1:])
    ]
    return {
        "id": f"align-{edge}-{number}",
        "edge": edge,
        "layerIds": [layer["id"] for _, layer, _ in by_height],
        "anchor": int(anchor),
        "inkEdges": {layer["id"]: int(value) for value, layer, _ in cluster},
        "gaps": [int(g) for g in gaps],
        "note": "Measured from the approved concept.",
    }


# --- applying ----------------------------------------------------------------------


def apply(scene):
    """Place group members so each group's edge is shared. Returns (scene, moves).

    Only x and the text's alignment within its box change. A member keeps its width,
    its y and its direction; a layer that would leave the canvas stays where it is. Moves are returned so the assembled
    design can say what was aligned.
    """
    groups = scene.get("alignmentGroups") or []
    if not groups:
        return scene, []
    patched = copy.deepcopy(scene)
    width = patched["canvas"]["width"]
    layers = {layer["id"]: layer for layer in patched["textLayers"]}
    moves = []
    for group in groups:
        for layer_id in group["layerIds"]:
            layer = layers.get(layer_id)
            if layer is None:
                continue
            wanted = {
                "right": group["anchor"] - layer["width"],
                "left": group["anchor"],
                "center": round(group["anchor"] - layer["width"] / 2),
            }[group["edge"]]
            # The text sits against the shared edge inside its box, so the layer's
            # alignment follows the edge the concept's ink shares.
            layer["align"] = group["edge"]
            if wanted < 0 or wanted + layer["width"] > width or wanted == layer["x"]:
                continue
            moves.append({"layerId": layer_id, "group": group["id"],
                          "from": layer["x"], "to": wanted})
            layer["x"] = wanted
    return patched, moves


def release(scene, layer_id):
    """Take a layer out of its group because a repair has moved it on purpose.

    A group would otherwise put it straight back at the next assembly. Returns the
    scene (modified in place); a group left with one member is dropped.
    """
    kept = []
    for group in scene.get("alignmentGroups") or []:
        if layer_id in group["layerIds"]:
            group["layerIds"] = [i for i in group["layerIds"] if i != layer_id]
            group["inkEdges"] = {k: v for k, v in (group.get("inkEdges") or {}).items()
                                 if k != layer_id}
        if len(group["layerIds"]) >= 2:
            kept.append(group)
    scene["alignmentGroups"] = kept
    return scene


# --- colours shared between shapes ----------------------------------------------------


def fill_groups(scene, tolerance_=24):
    """Group shapes whose fills are the same colour, without making them equal.

    The reference is the median member colour; each member keeps its own. Shapes
    with different fills (a red badge and a yellow button) are different groups or
    none, which is the point: a shared fill is recorded where it exists and never
    imposed where it does not.
    """
    shapes = [s for s in scene.get("shapeLayers") or []
              if s.get("kind") in ("panel", "circle", "dot") and s.get("color")]
    clusters = []
    for shape in shapes:
        rgb = _rgb(shape["color"])
        for cluster in clusters:
            if all(abs(a - b) <= tolerance_ for a, b in zip(rgb, cluster["rgb"])):
                cluster["members"].append(shape)
                break
        else:
            clusters.append({"rgb": rgb, "members": [shape]})
    groups = []
    for cluster in clusters:
        if len(cluster["members"]) < 2:
            continue
        colors = sorted((_rgb(s["color"]) for s in cluster["members"]))
        median = colors[len(colors) // 2]
        groups.append({
            "id": f"fill-{len(groups) + 1}",
            "layerIds": [s["id"] for s in cluster["members"]],
            "color": "#" + "".join(f"{v:02X}" for v in median),
            "note": "Shapes that share one fill in the concept; each keeps its own colour.",
        })
    return groups


def _rgb(value):
    digits = value.lstrip("#")
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
