"""Did the last repair help? Decided from evidence, not from a count.

The rule this replaces read "more blocking findings than before" as "the repair
made it worse". Measured on a live job, that is wrong in the way that matters
most: a gradient was added and the render became visibly closer to the approved
concept, the reviewer then promoted a headline *warning* to blocking in the same
breath, and the count went from one to two. The repair was a partial
improvement; the count called it a regression and the job stopped.

Three kinds of evidence are compared instead:

  findings   matched across the two reviews by semantic identity
             (lib/finding_identity.py), so a reworded finding is the same finding
             and a warning that became blocking is an escalation, not a new defect
  pixels     how far each render is from the approved concept, over the region
             the patch touched and over each finding's own region, and whether
             that region changed at all (lib/visual_compare.py)
  the patch  which elements it changed

and the outcome is one of five verdicts:

  A  verified improvement   every earlier blocking finding is gone and nothing new
                            blocks
  B  partial improvement    something improved - a finding resolved, or the render
                            moved measurably towards the concept - and something
                            still blocks
  C  no meaningful change   nothing resolved, nothing moved
  D  verified regression    the render moved measurably *away* from the concept
                            where it was changed or where a finding sits, or a
                            mandatory text was lost
  E  uncertain              the reviewer says it is worse and the pixels do not
                            back that up (or the evidence cannot be read)

Only D is a regression. A reviewer promoting a warning, or a new finding the
pixels do not support, is E at most: surrounding contrast can change how an
untouched layer reads, so unchanged properties neither prove nor disprove an
unchanged visual result - which is why the comparison is on pixels.
"""

from __future__ import annotations

from lib import finding_identity, visual_compare

VERIFIED_IMPROVEMENT = "A"
PARTIAL_IMPROVEMENT = "B"
NO_CHANGE = "C"
VERIFIED_REGRESSION = "D"
UNCERTAIN = "E"

LABELS = {
    VERIFIED_IMPROVEMENT: "verified improvement",
    PARTIAL_IMPROVEMENT: "partial improvement with remaining issues",
    NO_CHANGE: "no meaningful change",
    VERIFIED_REGRESSION: "verified regression",
    UNCERTAIN: "inconsistent or uncertain reviewer judgment",
}

#: Distance changes (0..1 of the full range) that count as measurable. A fraction
#: of the whole range rather than pixels, so it holds at any canvas size. Moving
#: *away* from the concept has to clear a higher bar than moving towards it: an
#: improvement can be small and real, but a regression is a claim that a repair did
#: damage, and that needs to be unmistakable.
MEANINGFUL_GAIN = 0.01
REGRESSION_REGION = 0.02
REGRESSION_GLOBAL = 0.012


def is_blocking(issue):
    return issue.get("severity") == "blocking"


def evaluate(*, previous_findings, current_findings, scene, changed_ids,
             canvas, previous_render=None, current_render=None, concept=None,
             mandatory_lost=()):
    """Compare a repaired candidate with the one it was repaired from.

    `previous_findings` / `current_findings` are every finding of each review,
    blocking and warning alike, each carrying its `severity`. `changed_ids` are the
    elements the patch changed. The renders and the concept are PNG bytes; when any
    is missing the comparison is made on findings alone and cannot prove a
    regression.

    Returns `{"verdict", "label", "reason", "evidence"}`.
    """
    matched = finding_identity.match(previous_findings, current_findings)
    pairs = matched["pairs"]

    resolved = [old for old in matched["resolved"] if is_blocking(old)]
    resolved += [old for old, new in pairs if is_blocking(old) and not is_blocking(new)]
    persisted = [(old, new) for old, new in pairs if is_blocking(old) and is_blocking(new)]
    escalated = [(old, new) for old, new in pairs if not is_blocking(old) and is_blocking(new)]
    brand_new = [new for new in matched["new"] if is_blocking(new)]
    blocking_now = [new for _, new in persisted] + [new for _, new in escalated] + brand_new

    patch_region = visual_compare.clamp_box(
        visual_compare.union(_boxes(scene, changed_ids)), canvas)
    visual = _visual(
        previous_render, current_render, concept, canvas, patch_region,
        regions={
            _label(new): _region_of(new, scene, canvas)
            for _, new in persisted + escalated
        } | {_label(new): _region_of(new, scene, canvas) for new in brand_new},
    )

    evidence = {
        "resolved": [_label(i) for i in resolved],
        "persisted": [_label(new) for _, new in persisted],
        "escalated": [{"from": _label(old), "to": _label(new)} for old, new in escalated],
        "new": [_label(i) for i in brand_new],
        "patchRegion": patch_region,
        "visual": visual,
        "changedElements": sorted(changed_ids),
    }

    def verdict(code, reason):
        return {"verdict": code, "label": LABELS[code], "reason": reason, "evidence": evidence}

    if mandatory_lost:
        return verdict(VERIFIED_REGRESSION,
                       "approved text was lost: " + ", ".join(sorted(mandatory_lost)))

    worsened = _worsened(visual)
    if worsened:
        return verdict(VERIFIED_REGRESSION, worsened)

    improved = bool(resolved) or _improved(visual)
    if improved and not blocking_now:
        return verdict(VERIFIED_IMPROVEMENT,
                       "every earlier blocking finding is resolved and nothing new blocks")
    if improved:
        note = ""
        if escalated or brand_new:
            note = (
                "; "
                + ", ".join(_label(i) for i in [new for _, new in escalated] + brand_new)
                + " became blocking without the render getting worse where it sits"
            )
        return verdict(PARTIAL_IMPROVEMENT,
                       _improvement_reason(resolved, visual) + note)
    if escalated or brand_new:
        return verdict(UNCERTAIN,
                       "the review reports new blocking findings but the pixels show "
                       "neither a regression nor a measurable change")
    if visual["compared"]:
        return verdict(NO_CHANGE, "no finding resolved and the render did not move "
                                  "measurably towards the concept")
    # Nothing could be compared and nothing resolved: the same findings on the same
    # elements, with no pixels to say the repair helped.
    return verdict(NO_CHANGE, "the same blocking findings remain and no render comparison "
                              "was available")


def _visual(previous, current, concept, canvas, patch_region, regions):
    out = {"compared": False}
    if not (previous and current and concept):
        return out
    try:
        before_global = visual_compare.distance(previous, concept, canvas)
        after_global = visual_compare.distance(current, concept, canvas)
        out.update({
            "compared": True,
            "globalBefore": _r(before_global), "globalAfter": _r(after_global),
            "globalGain": _r(before_global - after_global),
            "renderChange": _r(visual_compare.distance(previous, current, canvas)),
        })
        if patch_region:
            before = visual_compare.distance(previous, concept, canvas, patch_region)
            after = visual_compare.distance(current, concept, canvas, patch_region)
            out["patchGain"] = _r(before - after)
            out["patchChange"] = _r(visual_compare.distance(previous, current, canvas,
                                                            patch_region))
        found = {}
        for name, box in regions.items():
            if not box:
                continue
            before = visual_compare.distance(previous, concept, canvas, box)
            after = visual_compare.distance(current, concept, canvas, box)
            found[name] = {
                "gain": _r(before - after),
                "change": _r(visual_compare.distance(previous, current, canvas, box)),
            }
        out["findingRegions"] = found
    except Exception as exc:  # an unreadable image must not become a verdict
        return {"compared": False, "unreadable": f"{type(exc).__name__}: {exc}"}
    return out


def _worsened(visual):
    if not visual.get("compared"):
        return None
    gain = visual.get("patchGain")
    if gain is not None and gain <= -REGRESSION_REGION:
        return (f"the patched region moved {abs(gain):.3f} further from the approved "
                "concept")
    if visual["globalGain"] <= -REGRESSION_GLOBAL:
        return (f"the render as a whole moved {abs(visual['globalGain']):.3f} further "
                "from the approved concept")
    for name, found in (visual.get("findingRegions") or {}).items():
        if found["gain"] <= -REGRESSION_REGION:
            return (f"{name} moved {abs(found['gain']):.3f} further from the approved "
                    "concept where the finding sits")
    return None


def _improved(visual):
    if not visual.get("compared"):
        return False
    gain = visual.get("patchGain")
    return (gain is not None and gain >= MEANINGFUL_GAIN) or \
        visual["globalGain"] >= MEANINGFUL_GAIN


def _improvement_reason(resolved, visual):
    parts = []
    if resolved:
        parts.append(f"{len(resolved)} blocking finding(s) resolved")
    gain = visual.get("patchGain")
    if gain is not None and gain >= MEANINGFUL_GAIN:
        parts.append(f"the patched region moved {gain:.3f} closer to the approved concept")
    elif visual.get("compared") and visual["globalGain"] >= MEANINGFUL_GAIN:
        parts.append(f"the render moved {visual['globalGain']:.3f} closer to the approved concept")
    return "; ".join(parts) or "improved"


def _label(issue):
    family, elements = finding_identity.identity(issue)
    return f"{family}:{','.join(sorted(elements)) or '*'}"


def _boxes(scene, ids):
    wanted = {finding_identity.normalize_element(i) for i in ids if i != "effects"}
    out = []
    for key in ("textLayers", "shapeLayers", "imageLayers"):
        for layer in scene.get(key) or []:
            if finding_identity.normalize_element(layer["id"]) in wanted or layer["id"] in ids:
                out.append({k: layer[k] for k in ("x", "y", "width", "height")})
    return out


def _region_of(issue, scene, canvas):
    boxes = _boxes(scene, finding_identity.elements_of(issue) | set(
        issue.get("affectedElementIds") or []))
    if not boxes and isinstance(issue.get("region"), dict):
        region = issue["region"]
        if all(k in region for k in ("x", "y", "width", "height")):
            if max(region["x"] + region["width"], region["y"] + region["height"]) <= 1.5:
                region = {"x": region["x"] * canvas["width"], "y": region["y"] * canvas["height"],
                          "width": region["width"] * canvas["width"],
                          "height": region["height"] * canvas["height"]}
            boxes = [region]
    return visual_compare.clamp_box(visual_compare.union(boxes), canvas)


def _r(value):
    return None if value is None else round(float(value), 4)
