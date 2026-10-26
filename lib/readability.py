"""Measure the readability gradient a design needs, instead of assuming one.

The approved concept has a dark field behind its copy. CLEAN_PHOTO repaints the
photograph under the artwork, and the repainted photograph is often brighter than
the concept there - which is how a design ends up with white text on a light wall.
Declaring the darkening "owned by the photo" cannot help once the photo no longer
carries it, and a fixed "60% of the canvas" gradient added by a repair is a guess
that has no reason to match the concept.

So the layout draws the gradient, and it is fitted to pixels. Wherever the concept
is darker than the cleaned photograph (outside the artwork, which has its own
pixels), the difference is exactly the opacity of a black layer that would make
one into the other:

    concept = photo * (1 - alpha)         ->   alpha = 1 - concept / photo

Taking that difference rather than the concept's own darkness is what keeps the
gradient from being drawn twice. If the photograph already carries the shading,
the difference is about zero and nothing is added; if it lost half of it, half is
added. Either way the stack photograph + gradient reproduces the concept.

PIL only - no numpy in this package - and deliberately coarse: a 64x64 grid is
plenty to describe a soft fade, and it smooths out grain and text ink.
"""

from __future__ import annotations

import io

from PIL import Image

#: The grid a luminance map is reduced to, and the oversampling used to reduce it.
GRID = 64
OVERSAMPLE = 4

#: A gradient is only added when the opacity needed at its edge reaches this.
#: Below it the photograph already carries the shading, or the concept had none.
MIN_EDGE_ALPHA = 0.12
#: ...and the text region itself needs at least this much on average, so a dark
#: corner far from the copy is not mistaken for readability shading.
MIN_TEXT_ALPHA = 0.06
#: Never fully opaque: the photograph stays faintly visible, as in the concept.
MAX_ALPHA = 0.92
#: Where the fade is considered over, as a fraction of the edge opacity.
FADE_END = 0.25
#: A band shorter than this fraction of its axis is widened to it.
MIN_BAND = 0.2
#: Cells whose overlay alpha exceeds this belong to artwork, not photograph.
ARTWORK_ALPHA = 0.15

EDGES = ("left", "right", "top", "bottom")
#: CSS angle with the gradient's first stop on the named edge.
ANGLE = {"left": 90, "right": 270, "top": 180, "bottom": 0}


# --- reading pixels -------------------------------------------------------------


def luminance_grid(image_bytes, *, percentile=None, grid=GRID):
    """Relative luminance (0..1) per cell, as `rows[r][c]`.

    `percentile` chooses which pixel of each cell stands for it. A concept has
    text on it, and the text is not the background: with light text the dark end
    of each cell is the background, with dark text the light end is. The cleaned
    photograph has no text, so it uses the middle.
    """
    with Image.open(io.BytesIO(image_bytes)) as image:
        small = image.convert("L").resize((grid * OVERSAMPLE, grid * OVERSAMPLE), Image.BOX)
        pixels = small.load()
    pick = 0.5 if percentile is None else percentile
    rows = []
    for r in range(grid):
        row = []
        for c in range(grid):
            cell = sorted(
                pixels[c * OVERSAMPLE + i, r * OVERSAMPLE + j]
                for i in range(OVERSAMPLE) for j in range(OVERSAMPLE)
            )
            row.append(cell[min(len(cell) - 1, int(pick * len(cell)))] / 255.0)
        rows.append(row)
    return rows


def alpha_grid(overlay_bytes, *, grid=GRID):
    """Mean overlay opacity (0..1) per cell, or None when there is no overlay."""
    if not overlay_bytes:
        return None
    with Image.open(io.BytesIO(overlay_bytes)) as image:
        if "A" not in image.convert("RGBA").getbands():
            return None
        small = image.convert("RGBA").getchannel("A").resize((grid, grid), Image.BOX)
        pixels = small.load()
    return [[pixels[c, r] / 255.0 for c in range(grid)] for r in range(grid)]


#: Samples per cell side when pairing concept and photograph pixel by pixel.
PAIR_OVERSAMPLE = 8
#: A concept pixel this much brighter (dark copy: darker) than the photograph at the
#: same place is the copy or its artwork, not the background, and is left out.
COPY_MARGIN = 0.06
#: Below this mean luminance the photograph has nothing to darken.
MIN_PHOTO_LUMA = 0.03
#: A cell with fewer background pixels than this share is mostly copy: unmeasured.
MIN_BACKGROUND_SHARE = 0.25


def paired_grids(concept_bytes, photo_bytes, light_text, *, grid=GRID):
    """Per cell: the opacity of black (white, for dark copy) the concept added.

    Compares the two images pixel by pixel, leaving out the pixels that are the copy
    (the concept is brighter than the photograph there), and takes the cell's
    *means*:  alpha = 1 - mean(concept) / mean(photo).  Returns `(needed, concept,
    photo)` grids, `None` where a cell is mostly copy.

    Means rather than a percentile of each image: a dark photograph has very dark
    shadows whatever is done to it, so a low percentile of the photograph and of a
    black concept are both near zero and their ratio says "no shading" - while the
    highlights, which are what makes the background compete with the copy, are
    exactly what the concept removed.
    """
    side = grid * PAIR_OVERSAMPLE
    with Image.open(io.BytesIO(concept_bytes)) as a, Image.open(io.BytesIO(photo_bytes)) as b:
        c_px = a.convert("L").resize((side, side), Image.BOX).load()
        p_px = b.convert("L").resize((side, side), Image.BOX).load()
    needed = [[None] * grid for _ in range(grid)]
    c_mean = [[None] * grid for _ in range(grid)]
    p_mean = [[None] * grid for _ in range(grid)]
    total = PAIR_OVERSAMPLE * PAIR_OVERSAMPLE
    for r in range(grid):
        for c in range(grid):
            kept_c = kept_p = 0.0
            kept = 0
            for j in range(PAIR_OVERSAMPLE):
                for i in range(PAIR_OVERSAMPLE):
                    cv = c_px[c * PAIR_OVERSAMPLE + i, r * PAIR_OVERSAMPLE + j] / 255.0
                    pv = p_px[c * PAIR_OVERSAMPLE + i, r * PAIR_OVERSAMPLE + j] / 255.0
                    if (cv > pv + COPY_MARGIN) if light_text else (cv < pv - COPY_MARGIN):
                        continue
                    kept_c += cv
                    kept_p += pv
                    kept += 1
            if kept < total * MIN_BACKGROUND_SHARE:
                continue
            mc, mp = kept_c / kept, kept_p / kept
            c_mean[r][c], p_mean[r][c] = mc, mp
            if light_text:
                value = 0.0 if mp < MIN_PHOTO_LUMA else 1.0 - mc / mp
            else:
                value = 0.0 if mp > 1.0 - MIN_PHOTO_LUMA else (mc - mp) / (1.0 - mp)
            needed[r][c] = min(MAX_ALPHA, max(0.0, value))
    return needed, c_mean, p_mean


def _luma(hex_color):
    digits = hex_color.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    red, green, blue = (int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


# --- where the copy sits -----------------------------------------------------------


def _inside(inner, outer):
    cx = inner["x"] + inner["width"] / 2.0
    cy = inner["y"] + inner["height"] / 2.0
    return (outer["x"] <= cx <= outer["x"] + outer["width"]
            and outer["y"] <= cy <= outer["y"] + outer["height"])


def text_region(scene):
    """The union box of the copy that sits on the photograph, or None.

    Copy over a button or a circle sits on that shape, not on the photograph, and
    would pull the region towards the contact row.
    """
    backing = [s for s in scene.get("shapeLayers") or [] if s.get("kind") != "gradient"]
    boxes = [
        t for t in scene.get("textLayers") or []
        if not any(_inside(t, s) for s in backing)
    ] or list(scene.get("textLayers") or [])
    if not boxes:
        return None
    left = min(t["x"] for t in boxes)
    top = min(t["y"] for t in boxes)
    right = max(t["x"] + t["width"] for t in boxes)
    bottom = max(t["y"] + t["height"] for t in boxes)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def text_is_light(scene):
    """True when the copy is predominantly light, so the field behind it is dark."""
    layers = scene.get("textLayers") or []
    if not layers:
        return True
    return sum(_luma(t["color"]) for t in layers) / len(layers) >= 0.5


def band_for(scene, edge):
    """How far a readability gradient from `edge` has to reach to cover the copy.

    From the edge to the far side of the text region plus a margin for the fade,
    never narrower than MIN_BAND or wider than 90% of the axis. This replaces the
    fixed 60% a preset used to assume.
    """
    canvas = scene["canvas"]
    region = text_region(scene)
    horizontal = edge in ("left", "right")
    span = canvas["width"] if horizontal else canvas["height"]
    if region is None:
        return round(span * 0.5)
    if edge == "left":
        reach = region["x"] + region["width"]
    elif edge == "right":
        reach = span - region["x"]
    elif edge == "top":
        reach = region["y"] + region["height"]
    else:
        reach = span - region["y"]
    wanted = reach + span * 0.12
    return round(min(max(wanted, span * MIN_BAND), span * 0.9))


# --- the fit ----------------------------------------------------------------------


def fit(concept_bytes, photo_bytes, scene, overlay_bytes=None):
    """Fit a readability gradient from the concept and the cleaned photograph.

    Returns {"action": "add", ...geometry, "measured": {...}} when the photograph
    is missing shading the concept had, and {"action": "none", "reason": ...}
    when it is not. The geometry is what `scene_patch.add_shape(fitted=...)`
    draws: edge, box, CSS angle, colour and up to four stops from the edge.
    """
    canvas = scene["canvas"]
    region = text_region(scene)
    if region is None:
        return {"action": "none", "reason": "the scene has no text to make readable"}

    light_text = text_is_light(scene)
    unfiltered, concept, photo = paired_grids(concept_bytes, photo_bytes, light_text)
    artwork = alpha_grid(overlay_bytes)
    grid = len(concept)

    excluded = _excluded_cells(scene, canvas, grid)
    needed = [[None] * grid for _ in range(grid)]
    for r in range(grid):
        for c in range(grid):
            if excluded[r][c] or (artwork and artwork[r][c] > ARTWORK_ALPHA):
                continue
            needed[r][c] = unfiltered[r][c]

    rows, cols = _span(region, canvas, grid)
    horizontal_profile = _profile(needed, rows, axis="columns")
    vertical_profile = _profile(needed, cols, axis="rows")

    candidates = []
    for edge in EDGES:
        profile = horizontal_profile if edge in ("left", "right") else vertical_profile
        ordered = profile if edge in ("left", "top") else profile[::-1]
        candidates.append((max(ordered[:3]), edge, ordered))
    strength, edge, ordered = max(candidates, key=lambda item: item[0])

    text_mean = _text_mean(needed, rows, cols)
    measured = {
        "edge": edge, "edgeAlpha": round(strength, 3), "textMeanAlpha": round(text_mean, 3),
        "lightText": light_text,
    }
    if strength < MIN_EDGE_ALPHA or text_mean < MIN_TEXT_ALPHA:
        return {
            "action": "none", "measured": measured,
            "reason": (
                "the cleaned photograph already carries the concept's shading: "
                f"the most any edge needs is {strength:.0%} opacity"
            ),
        }

    dim = canvas["width"] if edge in ("left", "right") else canvas["height"]
    # The fade ends where the measurement says it does, but never further than the
    # copy needs it to: beyond the text the difference is the repainted photograph
    # differing in detail, not shading, and a veil there would dim the subject.
    reach = round(band_for(scene, edge) / dim * grid)
    length = min(_fade_length(ordered, strength), max(1, reach))
    stops = _stops(ordered, length, strength)
    band = max(round(dim * MIN_BAND), min(round(dim * 0.9), round(dim * length / grid)))
    across = _across_extent(needed, edge, length, strength, grid, canvas)
    box = _box(edge, band, across, canvas)
    measured.update({
        "bandPx": band, "stops": stops,
        "residual": _residual(concept, photo, needed, edge, length, stops, grid, light_text),
    })
    return {
        "action": "add", "edge": edge, "box": box, "angle": ANGLE[edge],
        "color": "#000000" if light_text else "#FFFFFF",
        "stops": [
            {"color": "#000000" if light_text else "#FFFFFF",
             "opacity": alpha, "position": position}
            for position, alpha in stops
        ],
        "measured": measured,
    }


def _excluded_cells(scene, canvas, grid):
    """Cells that belong to a shape the layout draws, which are not photograph."""
    out = [[False] * grid for _ in range(grid)]
    for shape in scene.get("shapeLayers") or []:
        if shape.get("kind") == "gradient":
            continue
        r0, r1 = _cells(shape["y"], shape["height"], canvas["height"], grid)
        c0, c1 = _cells(shape["x"], shape["width"], canvas["width"], grid)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                out[r][c] = True
    return out


def _cells(start, length, total, grid):
    first = max(0, min(grid - 1, int(start / total * grid)))
    last = max(first, min(grid - 1, int((start + length) / total * grid)))
    return first, last


def _span(region, canvas, grid):
    r0, r1 = _cells(region["y"], region["height"], canvas["height"], grid)
    c0, c1 = _cells(region["x"], region["width"], canvas["width"], grid)
    return range(r0, r1 + 1), range(c0, c1 + 1)


def _profile(needed, across, *, axis):
    """Mean needed opacity at each position along an axis, over `across`."""
    grid = len(needed)
    values = []
    for i in range(grid):
        cells = (
            [needed[r][i] for r in across] if axis == "columns"
            else [needed[i][c] for c in across]
        )
        cells = [v for v in cells if v is not None]
        values.append(sum(cells) / len(cells) if cells else None)
    return _smooth(_fill(values))


def _fill(values):
    """Carry the nearest measured value across gaps (artwork, buttons)."""
    known = [i for i, v in enumerate(values) if v is not None]
    if not known:
        return [0.0] * len(values)
    return [values[min(known, key=lambda k: abs(k - i))] for i in range(len(values))]


def _smooth(values):
    out = []
    for i in range(len(values)):
        window = values[max(0, i - 1):i + 2]
        out.append(sum(window) / len(window))
    return out


def _text_mean(needed, rows, cols):
    cells = [needed[r][c] for r in rows for c in cols if needed[r][c] is not None]
    return sum(cells) / len(cells) if cells else 0.0


def _fade_length(ordered, strength):
    """Cells from the edge until the opacity has fallen away."""
    peak = max(range(min(len(ordered), 6)), key=lambda i: ordered[i])
    for i in range(peak, len(ordered)):
        if ordered[i] < max(0.03, FADE_END * strength):
            return max(i, 1)
    return len(ordered)


def _stops(ordered, length, strength):
    """Four stops from the edge: opacity at the edge, a third, two thirds, none."""
    def at(fraction):
        position = fraction * length
        lower = min(len(ordered) - 1, int(position))
        upper = min(len(ordered) - 1, lower + 1)
        weight = position - int(position)
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    samples = [min(MAX_ALPHA, max(strength, at(0.0))), at(1 / 3), at(2 / 3), 0.0]
    # A fade only ever gets lighter away from the edge; measurement noise must not
    # produce a band in the middle of it.
    for i in range(1, len(samples)):
        samples[i] = min(samples[i], samples[i - 1])
    positions = (0.0, 0.33, 0.66, 1.0)
    if length >= len(ordered):          # never faded: keep the last stop as measured
        samples[3] = min(samples[2], at(1.0))
    return [(p, round(a, 2)) for p, a in zip(positions, samples)]


def _across_extent(needed, edge, length, strength, grid, canvas):
    """The stretch of the other axis the gradient has to cover, in pixels.

    Measured over the band's own cells; a gradient that covers the full height is
    reported as the full height so it does not stop a few pixels short.
    """
    horizontal = edge in ("left", "right")
    lo_band = 0 if edge in ("left", "top") else grid - length
    band = range(lo_band, min(grid, lo_band + length))
    levels = []
    for i in range(grid):
        cells = (
            [needed[i][c] for c in band] if horizontal else [needed[r][i] for r in band]
        )
        cells = [v for v in cells if v is not None]
        levels.append(sum(cells) / len(cells) if cells else None)
    levels = _smooth(_fill(levels))
    active = [i for i, v in enumerate(levels) if v >= 0.25 * strength]
    total = canvas["height"] if horizontal else canvas["width"]
    if not active:
        return 0, total
    first, last = max(0, min(active) - 1), min(grid - 1, max(active) + 1)
    start, end = round(first / grid * total), round((last + 1) / grid * total)
    if end - start >= 0.9 * total:
        return 0, total
    return start, end - start


def _box(edge, band, across, canvas):
    start, extent = across
    width, height = canvas["width"], canvas["height"]
    if edge in ("left", "right"):
        return {"x": 0 if edge == "left" else width - band, "y": start,
                "width": band, "height": extent}
    return {"x": start, "y": 0 if edge == "top" else height - band,
            "width": extent, "height": band}


def _residual(concept, photo, needed, edge, length, stops, grid, light_text):
    """Mean luminance error left inside the band once the fitted gradient is on.

    Evidence that the fit reproduces the concept, recorded with it: a large value
    says the shading is not a simple fade from one edge and a reviewer should look.
    """
    def alpha_at(distance):
        fraction = min(1.0, distance / max(1, length))
        for (p0, a0), (p1, a1) in zip(stops, stops[1:]):
            if fraction <= p1:
                span = max(1e-6, p1 - p0)
                return a0 + (a1 - a0) * (fraction - p0) / span
        return 0.0

    errors = []
    for r in range(grid):
        for c in range(grid):
            if needed[r][c] is None:
                continue
            distance = {"left": c, "right": grid - 1 - c, "top": r, "bottom": grid - 1 - r}[edge]
            if distance >= length:
                continue
            a = alpha_at(distance)
            predicted = (photo[r][c] * (1.0 - a) if light_text
                         else photo[r][c] + (1.0 - photo[r][c]) * a)
            errors.append(abs(predicted - concept[r][c]))
    return round(sum(errors) / len(errors), 3) if errors else None
