"""Geometric validation of the rendered design, in code.

A model looking at a screenshot cannot say that a column's right edge drifted nine
pixels, and cannot be trusted to notice that two lines now overlap by a third. The
browser knows exactly where every line of text ended up, so these are measured from
the DOM rectangles the renderer reports (`_inspect` in lib/render.py).

Tolerances are fractions of the canvas, never pixels: the same design at 1080 and at
1920 has the same defects.

Findings carry a severity. A shared edge off by a few pixels is a warning the
customer can see and the editor can fix; text over text, or text outside the canvas,
makes the design unreadable and blocks.
"""

from __future__ import annotations

#: Edges within this fraction of the canvas width are the same edge.
DRIFT = 0.006
#: Intersection area, as a share of the smaller text's area, above which two texts
#: overlap. Below it is line-box slack: ink rectangles of adjacent lines touch.
OVERLAP_WARNING = 0.08
OVERLAP_BLOCKING = 0.25
#: How far past the canvas text may reach before it is clipped, as a fraction.
CLIP = 0.004
#: A gap is uneven when it differs from the mean by more than this share of it.
UNEVEN = 0.35

WARNING = "warning"
BLOCKING = "blocking"


def check_render(scene, rects, canvas):
    """Problems measured from the rendered rectangles, as render-problem dicts.

    `rects` maps element id -> {"ink": {left, top, right, bottom}} for each text
    layer, in canvas px. Empty or absent (a renderer that did not report them)
    yields no findings: a missing measurement is not a pass, but it is not a
    defect either, and the required-checks list already treats it separately.
    """
    if not rects:
        return []
    from lib import assemble_html

    element_of = assemble_html.element_ids(scene)
    layers = {layer["id"]: layer for layer in scene.get("textLayers") or []}
    ink = {
        layer_id: rects[element]["ink"]
        for layer_id, element in element_of.items()
        if layer_id in layers and element in rects and rects[element].get("ink")
    }
    problems = []
    problems.extend(_drift(scene, ink, element_of, canvas))
    problems.extend(_spacing(scene, ink, element_of))
    problems.extend(_overlaps(layers, ink, element_of))
    problems.extend(_clipping(layers, ink, element_of, canvas))
    return problems


def _problem(code, severity, element, message, **measured):
    return {"code": code, "severity": severity, "layerId": element,
            "message": message, "measured": measured}


def _edge(box, edge):
    return {"left": box["left"], "right": box["right"],
            "center": (box["left"] + box["right"]) / 2.0}[edge]


def _drift(scene, ink, element_of, canvas):
    """Members of an alignment group whose rendered edge left the shared line."""
    tolerance = max(2.0, canvas["width"] * DRIFT)
    problems = []
    for group in scene.get("alignmentGroups") or []:
        members = [i for i in group["layerIds"] if i in ink]
        if len(members) < 2:
            continue
        values = {i: _edge(ink[i], group["edge"]) for i in members}
        ordered = sorted(values.values())
        reference = ordered[len(ordered) // 2]
        for layer_id, value in values.items():
            if abs(value - reference) > tolerance:
                problems.append(_problem(
                    "ALIGNMENT_DRIFT", WARNING, element_of[layer_id],
                    f"{layer_id} sits {abs(value - reference):.0f}px off the {group['edge']} "
                    f"edge shared with {', '.join(m for m in members if m != layer_id)}.",
                    group=group["id"], edge=group["edge"], drift=round(value - reference, 1),
                    tolerance=round(tolerance, 1),
                ))
    return problems


def _spacing(scene, ink, element_of):
    """An even rhythm in the concept that rendered uneven.

    Only judged when the concept's own gaps were even: a design that spaces its
    lines unevenly on purpose has no rhythm to lose.
    """
    problems = []
    for group in scene.get("alignmentGroups") or []:
        gaps = group.get("gaps") or []
        if len(gaps) < 2 or not _even(gaps):
            continue
        members = sorted((i for i in group["layerIds"] if i in ink),
                         key=lambda i: ink[i]["top"])
        rendered = [max(0.0, ink[b]["top"] - ink[a]["bottom"])
                    for a, b in zip(members, members[1:])]
        if len(rendered) >= 2 and not _even(rendered):
            problems.append(_problem(
                "SPACING_UNEVEN", WARNING, element_of[members[0]],
                "The vertical spacing between these lines was even in the concept "
                f"and rendered as {', '.join(f'{g:.0f}' for g in rendered)}px.",
                group=group["id"], rendered=[round(g) for g in rendered], concept=gaps,
            ))
    return problems


def _even(gaps):
    mean = sum(gaps) / len(gaps)
    return mean <= 0 or max(abs(g - mean) for g in gaps) <= max(3.0, UNEVEN * mean)


def _overlaps(layers, ink, element_of):
    problems = []
    ids = sorted(ink)
    for i, first in enumerate(ids):
        for second in ids[i + 1:]:
            a, b = ink[first], ink[second]
            width = min(a["right"], b["right"]) - max(a["left"], b["left"])
            height = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
            if width <= 0 or height <= 0:
                continue
            smaller = min(_area(a), _area(b))
            share = (width * height) / smaller if smaller > 0 else 0.0
            if share < OVERLAP_WARNING:
                continue
            severity = BLOCKING if share >= OVERLAP_BLOCKING else WARNING
            problems.append(_problem(
                "TEXT_OVERLAP", severity, element_of[first],
                f"{first} and {second} overlap by {share:.0%} of the smaller line.",
                other=element_of[second], share=round(share, 3),
            ))
    return problems


def _area(box):
    return max(0.0, box["right"] - box["left"]) * max(0.0, box["bottom"] - box["top"])


def _clipping(layers, ink, element_of, canvas):
    slack = max(2.0, canvas["width"] * CLIP)
    problems = []
    for layer_id, box in ink.items():
        beyond = max(-box["left"], box["right"] - canvas["width"],
                     -box["top"], box["bottom"] - canvas["height"])
        if beyond > slack:
            problems.append(_problem(
                "TEXT_CLIPPED", BLOCKING, element_of[layer_id],
                f"{layer_id} runs {beyond:.0f}px outside the canvas and is cut off.",
                beyond=round(beyond, 1),
            ))
    return problems
