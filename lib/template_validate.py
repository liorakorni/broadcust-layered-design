"""Technical checks on the Design Base raster before the first HTML (layered-v2).

Before the first HTML exists only questions a machine can answer are asked, and they are
asked of the bytes: can the template be decoded, does it carry real alpha, does it hide
the photograph, does it still contain what another layer owns. How faithful it is to the
concept is judged afterwards, on the assembled composite (lib/template_check.py records
its measurements here too, as findings, never as a reason to stop). A model call is never
made.

A problem has a CATEGORY, so a consumer can tell the cases apart instead of reading
everything as one transparency fault:

    technical    the bytes are unusable (undecodable, no alpha where alpha is required)
    obstruction  the template would hide the photograph or the text it must not cover
    visual       the template differs from the concept; a finding for the composite review
    ownership    it still contains something another layer owns (a duplicate)

and an ACTION: "fallback" (drop the raster, draw the basics in CSS), "finding" (carry on,
tell the review) or "warning" (record only).
"""

from __future__ import annotations

from lib import validate_assets
from lib.errors import AssetInvalid

CATEGORY_TECHNICAL = "technical"
CATEGORY_OBSTRUCTION = "obstruction"
CATEGORY_VISUAL = "visual"
CATEGORY_OWNERSHIP = "ownership"
CATEGORIES = (CATEGORY_TECHNICAL, CATEGORY_OBSTRUCTION, CATEGORY_VISUAL, CATEGORY_OWNERSHIP)

ACTION_FALLBACK = "fallback"
ACTION_FINDING = "finding"
ACTION_WARNING = "warning"

#: Machine-readable codes (documented in docs/CONTRACTS.md). The first four replace the
#: single INVALID_TRANSPARENCY that used to describe all of them.
UNDECODABLE = "TEMPLATE_UNDECODABLE"
NO_ALPHA = "TEMPLATE_NO_ALPHA"
OBSTRUCTS_PHOTO = "TEMPLATE_OBSTRUCTS_PHOTO"
WINDOWS_COVERED = "TEMPLATE_WINDOWS_COVERED"
WINDOW_PARTIAL = "TEMPLATE_WINDOW_PARTIAL"
EMPTY = "TEMPLATE_EMPTY"
HARD_EDGES = "TEMPLATE_HARD_EDGES"
ARTWORK_MISSING = "TEMPLATE_ARTWORK_MISSING"
BAKED_COMPONENT = "TEMPLATE_BAKED_COMPONENT"
BROAD_COVER = "TEMPLATE_BROAD_PANEL_COVERS_COMPONENT"
PHOTO_BAKED_COMPONENT = "PHOTO_BAKED_COMPONENT"

#: Share of the declared photograph windows that must be covered for the template to count
#: as hiding the photograph (rather than being locally off).
WINDOWS_COVERED_SHARE = 0.5

#: A component box is covered by a BROAD panel, not baked into the template, when the ring
#: around the box is as opaque as the box itself. A baked copy of a button is a patch: the
#: ring around it is the photograph (transparent).
RING_FRACTION = 0.5
RING_MIN_OPAQUE = 0.6


def problem(code, category, action, message, affected=None, **extra):
    return {"code": code, "category": category, "action": action, "message": message,
            "affected": list(affected or []), **extra}


def check_template(data, scene, canvas, representation="overlay"):
    """Technical and ownership checks on the template raster.

    Returns `{problems, alpha, rgbaOk}`. Never raises for a template that is merely bad:
    that is a result, not an error, so the caller can choose the fallback.
    """
    problems = []
    try:
        rgba, had_alpha = validate_assets.load_rgba(data)
    except AssetInvalid as exc:
        return {"problems": [problem(UNDECODABLE, CATEGORY_TECHNICAL, ACTION_FALLBACK,
                                     exc.message)],
                "alpha": None, "rgbaOk": False}

    needs_alpha = representation != "full"
    if not had_alpha and needs_alpha:
        return {"problems": [problem(
            NO_ALPHA, CATEGORY_TECHNICAL, ACTION_FALLBACK,
            "The design base has no alpha channel, so the photograph cannot show through "
            "it. It is flat, not transparent; no colour is keyed out to fake it.")],
            "alpha": None, "rgbaOk": False}

    rgba = validate_assets.normalize_alpha(rgba)
    rgba = validate_assets.resample_to_canvas(rgba, canvas)
    stats = validate_assets.alpha_stats(rgba)
    alpha = {k: round(v, 4) for k, v in stats.items() if k != "alphaMax"}

    if representation != "full" and stats["transparent"] < 0.01:
        problems.append(problem(
            OBSTRUCTS_PHOTO, CATEGORY_OBSTRUCTION, ACTION_FALLBACK,
            f"The design base is effectively opaque across the canvas "
            f"({stats['transparent']:.1%} transparent); it would hide the photograph."))
    if stats["opaque"] < 0.001:
        problems.append(problem(
            EMPTY, CATEGORY_VISUAL, ACTION_FINDING,
            "The design base is effectively empty; the structural artwork is missing."))
    elif stats["partial"] < validate_assets.MIN_PARTIAL_FRACTION and needs_alpha:
        problems.append(problem(
            HARD_EDGES, CATEGORY_TECHNICAL, ACTION_WARNING,
            "The design base has no soft edges; its edges may look hard against the "
            "photograph. Alpha is otherwise genuine, so it is used."))

    if needs_alpha:
        problems.extend(_samples(rgba, scene, canvas))
    problems.extend(_ownership(rgba, scene, canvas))
    return {"problems": problems, "alpha": alpha, "rgbaOk": True}


def _samples(rgba, scene, canvas):
    failures = validate_assets.sample_failures(rgba, scene, canvas)
    windows = [s for s in scene.get("overlaySamples") or [] if s["kind"] == "transparent"]
    covered = [f for f in failures if f["kind"] == "transparent"]
    out = []
    if windows and len(covered) / float(len(windows)) >= WINDOWS_COVERED_SHARE:
        out.append(problem(
            WINDOWS_COVERED, CATEGORY_OBSTRUCTION, ACTION_FALLBACK,
            f"{len(covered)} of {len(windows)} photograph windows are covered by the design "
            "base; the photograph would be hidden where the concept shows it.",
            regions=[f["index"] for f in covered]))
    elif covered:
        out.append(problem(
            WINDOW_PARTIAL, CATEGORY_VISUAL, ACTION_FINDING,
            "; ".join(f["message"] for f in covered),
            regions=[f["index"] for f in covered]))
    missing = [f for f in failures if f["kind"] == "opaque"]
    if missing:
        out.append(problem(
            ARTWORK_MISSING, CATEGORY_VISUAL, ACTION_FINDING,
            "; ".join(f["message"] for f in missing), regions=[f["index"] for f in missing]))
    return out


def _ownership(rgba, scene, canvas):
    """Components another layer draws that the template still contains.

    A component box that sits inside a broad opaque panel is NOT a baked copy: the panel
    legitimately covers it, and the original check read that as 100% opaque and
    attributed the finding to the component (the fixture's two wasted repairs). It is
    reported under its own code, as information.
    """
    from PIL import ImageStat

    out = []
    baked, broad = [], []
    for component in validate_assets.editable_component_boxes(scene):
        middle = validate_assets._central(component["box"], canvas,
                                          validate_assets.COMPONENT_SAMPLE)
        if middle is None:
            continue
        inside = ImageStat.Stat(rgba.crop(middle).getchannel("A")).mean[0] / 255.0
        if inside <= validate_assets.BAKED_ALPHA:
            continue
        ring = _ring_opacity(rgba, component["box"], canvas)
        (broad if ring is not None and ring >= RING_MIN_OPAQUE else baked).append(
            component["layerId"])
    if baked:
        out.append(problem(
            BAKED_COMPONENT, CATEGORY_OWNERSHIP, ACTION_WARNING,
            "The design base still contains elements the layout draws itself: "
            + ", ".join(sorted(baked)) + ". Drawn twice, they would duplicate when one "
            "is moved.", affected=sorted(baked)))
    if broad:
        out.append(problem(
            BROAD_COVER, CATEGORY_OWNERSHIP, ACTION_WARNING,
            "These components sit inside a broad opaque area of the design base, which is "
            "not a baked copy of them: " + ", ".join(sorted(broad)) + ".",
            affected=sorted(broad)))
    return out


def _ring_opacity(rgba, box, canvas):
    """Mean opacity (0..1) of the band around `box`, or None when it is off the canvas."""
    from PIL import ImageChops, Image, ImageStat

    pad_x, pad_y = box["width"] * RING_FRACTION, box["height"] * RING_FRACTION
    outer = (max(0, int(box["x"] - pad_x)), max(0, int(box["y"] - pad_y)),
             min(canvas["width"], int(box["x"] + box["width"] + pad_x)),
             min(canvas["height"], int(box["y"] + box["height"] + pad_y)))
    inner = (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"])
    if outer[2] <= outer[0] or outer[3] <= outer[1]:
        return None
    alpha = rgba.getchannel("A").crop(outer)
    hole = Image.new("L", alpha.size, 255)
    hole.paste(0, (inner[0] - outer[0], inner[1] - outer[1],
                   inner[2] - outer[0], inner[3] - outer[1]))
    stat = ImageStat.Stat(alpha, mask=hole)
    return stat.mean[0] / 255.0 if stat.count[0] else None


def photo_problems(background, scene, canvas):
    """Components the cleaned photograph still shows. A violation, never a rebuild trigger."""
    from lib.errors import InvalidTransparency

    try:
        validate_assets.check_component_area_repainted(background, scene, canvas)
    except InvalidTransparency as exc:
        components = (getattr(exc, "surrender", None) or {}).get("components") or []
        return [problem(
            PHOTO_BAKED_COMPONENT, CATEGORY_OWNERSHIP, ACTION_WARNING, exc.message,
            affected=sorted({c["layerId"] for c in components}))]
    return []
