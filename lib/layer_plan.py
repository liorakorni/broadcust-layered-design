"""The layer plan: who owns what, and what to do if the structural template cannot be used.

It lives INSIDE the scene (`scene["layerPlan"]`), not beside it, so it is validated, stored
and versioned with the design it describes. It is derived deterministically from what
EXTRACT_SCENE already produced (text and shape layers, image layers, the component plan,
the effect owners) plus a few measurements of the approved concept; it asks the model
for nothing except the one thing only the model can see, whether the design base is more
than a gradient (`scene["designBase"]`).

Six categories, not six mandatory images. A category is an ownership statement; most are
not images at all:

    backgroundPhoto        the photograph (raster)
    structuralTemplate     the Design Base: broad fills, readability gradients, torn
                           edges, waves, fans. Raster for complex artwork, CSS for
                           simple fills and gradients.
    movableDecoration      things an editor moves together: dot rows, chevron runs,
                           borders, logos, insets. Grouped; never one image per dot.
    editableText           HTML text
    editableTextBackground CTA panels, contact circles, text cards (CSS, never baked)
    genericIcon            transparent raster pictograms, independent `.badges` layers

The template raster EXCLUDES text, logos, icons, text cards, CTA backgrounds and contact
circles, and every effect the layout draws in CSS (so a readability gradient exists once).

Coordinates: every box here is in CANVAS pixels. `coordinateSpace` records the size of the
approved concept the extraction saw, and lib/coords.py converts between them, so a box is
never quoted to a model, a mask or a comparison in the wrong space.

The plan is VERSIONED. A local correction (a layer moved, a template replaced by CSS)
bumps `revision` and appends to `corrections` with the elements it touched; `sync` brings
the geometry up to date after a scene patch and says what moved. A correction names the
elements it invalidates (`dependents`), so nothing else is regenerated and the concept is
never touched.
"""

from __future__ import annotations

import copy
import io

VERSION = "layer-plan-1"

CATEGORIES = (
    "backgroundPhoto", "structuralTemplate", "movableDecoration", "editableText",
    "editableTextBackground", "genericIcon",
)
OWNERS = ("photo", "template", "layout", "text", "asset")
RENDERERS = ("raster", "css", "text")
REPRESENTATIONS = ("overlay", "full", "css-only")

#: Categories the template raster must not contain.
EXCLUDED_FROM_TEMPLATE = (
    "editableText", "editableTextBackground", "genericIcon", "movableDecoration",
)

#: Largest fill a fallback may paint, as a share of the canvas. A "fill" larger than this
#: is more likely the photograph than a panel, so it is not trusted.
MAX_FILL_AREA = 0.55
EXTENT_STEP = 8
EXTENT_TOLERANCE = 40
MAX_FALLBACK_FILLS = 4
FALLBACK_SCRIM_OPACITY = 0.6

ALPHA_NOTE = (
    "Transparency of the template is verified only at the sampled photograph windows and "
    "artwork regions; the alpha of soft edges and partial-opacity gradients is not "
    "measured individually."
)


class PlanError(ValueError):
    """The plan contradicts itself (a duplicate owner, an unknown dependency)."""


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def build(scene, concept_bytes=None):
    """The layer plan for a scene document. Pure: the scene is not modified."""
    components = {c["layerId"]: c for c in scene.get("components") or []}
    effects = {e["id"]: e for e in scene.get("effects") or []}
    paired = _paired_text(scene)
    # Dots and chevrons that move together are one group, not one image (or one editing
    # handle) each: grouped by the kind of effect they implement.
    shape_counts = {}
    for shape in scene.get("shapeLayers") or []:
        kind = (effects.get(shape["effect"]) or {}).get("kind")
        shape_counts[kind] = shape_counts.get(kind, 0) + 1

    elements = []
    for layer in scene.get("imageLayers") or []:
        category, owner = _image_category(layer, components.get(layer["id"]))
        elements.append(_element(
            layer, category, owner, "raster",
            depends=[components[layer["id"]]["pairsWith"]]
            if category == "genericIcon" and (components.get(layer["id"]) or {}).get("pairsWith")
            else [],
        ))
    for layer in scene.get("textLayers") or []:
        elements.append(_element(layer, "editableText", "text", "text"))
    for shape in scene.get("shapeLayers") or []:
        effect = effects.get(shape["effect"]) or {}
        owner_text = paired.get(shape["id"])
        if owner_text:
            category, group = "editableTextBackground", None
            depends = [owner_text]
        elif effect.get("kind") == "readabilityGradient":
            category, group, depends = "structuralTemplate", None, []
        else:
            category, depends = "movableDecoration", []
            kind = effect.get("kind")
            group = (f"group-{kind}" if kind in ("dots", "chevrons")
                     and shape_counts.get(kind, 0) > 1 else None)
        elements.append(_element(shape, category, "layout", "css", depends=depends, group=group))

    for effect in scene.get("effects") or []:
        if effect["kind"] == "readabilityGradient" and effect["owner"] != "layout":
            # Declared as baked: into the photograph or into the template. It is listed
            # so the ownership is on the record (one owner, never a second copy).
            baked_in = "photo" if effect["owner"] == "photo" else "template"
            elements.append({
                "id": effect["id"], "category": "structuralTemplate", "owner": baked_in,
                "renderer": "raster", "geometry": None, "zIndex": 2, "dependsOn": [],
                "groupId": None, "status": "baked",
                "note": "Readability darkening baked into the "
                        + ("photograph" if baked_in == "photo" else "template")
                        + "; a CSS scrim is drawn only if the template is dropped.",
            })

    representation, reason = _representation(scene)
    template = {
        "representation": representation,
        "reason": reason,
        "gradientOnly": representation == "css-only",
        "alphaUncertainty": None if representation == "css-only" else ALPHA_NOTE,
        "cssOwnedEffects": sorted(
            e["id"] for e in effects.values() if e["owner"] == "layout"),
        "excludes": sorted(
            el["id"] for el in elements if el["category"] in EXCLUDED_FROM_TEMPLATE),
        "retains": [
            (c.get("note") or "")[:300]
            for c in scene.get("components") or []
            if c["kind"] == "retainedRaster" and _is_decoration(scene, c["layerId"])
        ],
    }
    plan = {
        "version": VERSION,
        "revision": 1,
        "coordinateSpace": _space(scene, concept_bytes),
        "elements": elements,
        "template": template,
        "fallback": fallback(scene, concept_bytes),
        "corrections": [],
    }
    check(plan)
    return plan


def _element(layer, category, owner, renderer, depends=(), group=None):
    return {
        "id": layer["id"], "category": category, "owner": owner, "renderer": renderer,
        "geometry": {k: int(layer[k]) for k in ("x", "y", "width", "height")},
        "zIndex": int(layer["zIndex"]), "dependsOn": list(depends), "groupId": group,
        "status": "present", "note": None,
    }


def _image_category(layer, component):
    if layer["type"] == "background":
        return "backgroundPhoto", "photo"
    if layer["type"] == "decoration":
        return "structuralTemplate", "template"
    if (component or {}).get("kind") == "rasterIcon" or str(layer.get("assetId", "")).startswith("icon-"):
        return "genericIcon", "asset"
    return "movableDecoration", "asset"        # a logo or inset: moves as one thing


def _paired_text(scene):
    """{shape layer id: text layer id} for CTA panels, contact circles and text cards."""
    text_ids = {t["id"] for t in scene.get("textLayers") or []}
    out = {}
    for component in scene.get("components") or []:
        if component["kind"] == "editableShape" and component.get("pairsWith") in text_ids:
            out[component["layerId"]] = component["pairsWith"]
    return out


def _is_decoration(scene, layer_id):
    return any(l["id"] == layer_id and l["type"] == "decoration"
               for l in scene.get("imageLayers") or [])


def _space(scene, concept_bytes):
    canvas = scene["canvas"]
    space = {"canvas": {"width": canvas["width"], "height": canvas["height"]}, "concept": None}
    if concept_bytes:
        from lib import coords

        width, height = coords.image_size(concept_bytes)
        space["concept"] = {"width": width, "height": height}
    return space


def _representation(scene):
    """How the Design Base is delivered, decided from the scene BEFORE any extraction.

    The model declared it (`designBase`); the scene's own samples can contradict the
    declaration, and then the samples win, because they are what validation will check.
    A design whose only structure is a gradient has nothing for a raster call to extract,
    and asking for one invites the provider to invent ornaments.
    """
    declared = ((scene.get("designBase") or {}).get("representation")) or "overlay"
    samples = scene.get("overlaySamples") or []
    opaque = any(s["kind"] == "opaque" for s in samples)
    transparent = any(s["kind"] == "transparent" for s in samples)
    if declared == "css-only":
        if opaque:
            return "overlay", (
                "The design base was declared gradient-only, but the scene reports opaque "
                "artwork regions; a raster template is kept."
            )
        return "css-only", (
            "The design base is a gradient or plain fills that CSS represents fully; no "
            "raster extraction is made, so none can add ornaments."
        )
    if declared == "full":
        if transparent:
            return "overlay", (
                "The design base was declared full-canvas, but the scene reports "
                "photograph windows; the template stays an overlay."
            )
        return "full", (
            "No photograph shows through the design base, so it may be opaque everywhere."
        )
    return "overlay", (
        "Structural artwork lies over the photograph, which shows through transparent windows."
    )


# --------------------------------------------------------------------------
# The CSS fallback, persisted before any asset is generated
# --------------------------------------------------------------------------


def fallback(scene, concept_bytes=None):
    """The parameters of the deterministic fallback for an unusable template.

    Fill colours and regions, one readability gradient, geometry and stacking, and the
    details that are simply omitted. Measured from the concept when its bytes are at hand;
    without them the fills are left out rather than guessed.
    """
    canvas = scene["canvas"]
    decoration = next((l for l in scene.get("imageLayers") or [] if l["type"] == "decoration"), None)
    z = int(decoration["zIndex"]) if decoration else 1
    fills = []
    if concept_bytes:
        fills = _fills(concept_bytes, scene)
    return {
        "version": VERSION,
        "fills": fills,
        "gradient": _gradient(scene, concept_bytes),
        "stacking": {"fillsZIndex": z, "gradientZIndex": min(100, z + 1)},
        "omitted": [
            (c.get("note") or "")[:300]
            for c in scene.get("components") or []
            if c["kind"] == "retainedRaster" and _is_decoration(scene, c["layerId"])
        ],
        "basis": "concept" if concept_bytes else "none",
        "canvas": {"width": canvas["width"], "height": canvas["height"]},
    }


def _fills(concept_bytes, scene):
    from lib import concept_sample

    canvas = scene["canvas"]
    windows = [s for s in scene.get("overlaySamples") or [] if s["kind"] == "transparent"]
    out = []
    for number, sample in enumerate(
            [s for s in scene.get("overlaySamples") or [] if s["kind"] == "opaque"], 1):
        box = {k: sample[k] for k in ("x", "y", "width", "height")}
        colour = concept_sample.median_color(concept_bytes, box, canvas)
        if not colour:
            continue
        grown = _extent(concept_bytes, box, colour, canvas)
        area = grown["width"] * grown["height"] / float(canvas["width"] * canvas["height"])
        if area > MAX_FILL_AREA or any(_overlap(grown, w) for w in windows):
            continue                    # more likely the photograph than a panel
        out.append({"id": f"fallback-fill-{number}", **grown, "color": colour, "opacity": 1.0})
        if len(out) >= MAX_FALLBACK_FILLS:
            break
    return out


def _extent(concept_bytes, box, colour, canvas):
    """Grow `box` while the strip beside it is still the same fill colour."""
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(concept_bytes)) as image:
        image = image.convert("RGB")
        sx = image.width / float(canvas["width"])
        sy = image.height / float(canvas["height"])
        x0, y0 = box["x"], box["y"]
        x1, y1 = box["x"] + box["width"], box["y"] + box["height"]
        target = tuple(int(colour.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) \
            if len(colour) == 7 else (0, 0, 0)

        def same(rect):
            left, top, right, bottom = rect
            if right <= left or bottom <= top:
                return False
            crop = image.crop((round(left * sx), round(top * sy), round(right * sx), round(bottom * sy)))
            if crop.width < 1 or crop.height < 1:
                return False
            mean = ImageStat.Stat(crop).mean
            return max(abs(mean[i] - target[i]) for i in range(3)) <= EXTENT_TOLERANCE

        step, width, height = EXTENT_STEP, canvas["width"], canvas["height"]
        grew = True
        while grew:
            grew = False
            if x0 - step >= 0 and same((x0 - step, y0, x0, y1)):
                x0 -= step; grew = True
            if x1 + step <= width and same((x1, y0, x1 + step, y1)):
                x1 += step; grew = True
            if y0 - step >= 0 and same((x0, y0 - step, x1, y0)):
                y0 -= step; grew = True
            if y1 + step <= height and same((x0, y1, x1, y1 + step)):
                y1 += step; grew = True
    return {"x": int(x0), "y": int(y0), "width": int(x1 - x0), "height": int(y1 - y0)}


def _overlap(a, b):
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


def _gradient(scene, concept_bytes):
    """A readability scrim on the side the text sits, when the scene wants one baked.

    Only when the readability effect is declared and nothing in the layout draws it (the
    template or the photograph was carrying it). Darkest side is where the words are.
    """
    effect = next((e for e in scene.get("effects") or [] if e["kind"] == "readabilityGradient"), None)
    if not effect or effect["owner"] != "overlay" or any(
            s["effect"] == effect["id"] for s in scene.get("shapeLayers") or []):
        return None             # photo-owned darkening survives the template's removal
    texts = scene.get("textLayers") or []
    if not texts:
        return None
    canvas = scene["canvas"]
    cx = sum((t["x"] + t["width"] / 2.0) * t["width"] * t["height"] for t in texts)
    cy = sum((t["y"] + t["height"] / 2.0) * t["width"] * t["height"] for t in texts)
    mass = sum(t["width"] * t["height"] for t in texts) or 1
    fx, fy = cx / mass / canvas["width"], cy / mass / canvas["height"]
    if abs(fx - 0.5) >= abs(fy - 0.5):
        angle = 90 if fx > 0.5 else 270          # opaque end on the text's side
    else:
        angle = 180 if fy > 0.5 else 0
    return {
        "effect": effect["id"], "angle": angle, "color": "#000000",
        "x": 0, "y": 0, "width": canvas["width"], "height": canvas["height"],
        "stops": [{"color": "#000000", "opacity": 0.0, "position": 0.0},
                  {"color": "#000000", "opacity": FALLBACK_SCRIM_OPACITY, "position": 1.0}],
    }


# --------------------------------------------------------------------------
# Applying the fallback
# --------------------------------------------------------------------------


def apply_fallback(scene, reason, code, deliberate=False):
    """A copy of `scene` with the raster template omitted and its basics drawn in CSS.

    Never substitutes the flattened concept and invents no artwork: the fills and the one
    scrim were measured from the concept before any asset existed, and everything else the
    template carried is listed under `omitted`. Subject visibility is protected by
    refusing, at build time, any fill that overlaps a photograph window.

    Returns `(scene, summary)`; `summary` is what `fallbacksUsed` records.
    """
    scene = copy.deepcopy(scene)
    plan = scene.get("layerPlan") or build(scene)
    spec = plan["fallback"]
    decoration = next((l for l in scene.get("imageLayers") or [] if l["type"] == "decoration"), None)
    removed_effects = []
    if decoration:
        scene["imageLayers"] = [l for l in scene["imageLayers"] if l["id"] != decoration["id"]]
        scene["components"] = [c for c in scene.get("components") or []
                               if c["layerId"] != decoration["id"]]
        kept = []
        for effect in scene.get("effects") or []:
            if effect["owner"] == "overlay":
                removed_effects.append(effect["id"])
            else:
                kept.append(effect)
        scene["effects"] = kept

    drawn = []
    shapes = scene.setdefault("shapeLayers", [])
    effects = scene.setdefault("effects", [])
    if spec["fills"] and len(shapes) + len(spec["fills"]) <= 40:
        effects.append({"id": "template-fallback", "kind": "panel", "owner": "layout",
                        "note": "Plain fill measured from the concept; replaces an unusable template."})
        for fill in spec["fills"]:
            shapes.append({
                "id": fill["id"], "type": "shape", "kind": "panel", "effect": "template-fallback",
                "x": fill["x"], "y": fill["y"], "width": fill["width"], "height": fill["height"],
                "zIndex": spec["stacking"]["fillsZIndex"], "color": fill["color"],
                "opacity": fill["opacity"], "radius": 0, "angle": 180, "stops": [],
                "borderColor": None, "borderWidth": 0.0, "rotation": 0,
            })
            drawn.append(fill["id"])
            _classify(scene, {
                "id": f"component-{fill['id']}", "kind": "editableShape", "layerId": fill["id"],
                "pairsWith": None, "note": "Template fallback fill."})
    gradient = spec.get("gradient")
    if gradient and not any(e["kind"] == "readabilityGradient" and e["owner"] == "layout"
                            for e in effects):
        for effect in effects:
            if effect["id"] == gradient["effect"]:
                effect["owner"] = "layout"
        if not any(e["id"] == gradient["effect"] for e in effects):
            effects.append({"id": gradient["effect"], "kind": "readabilityGradient",
                            "owner": "layout", "note": "Scrim replacing the template's."})
        shapes.append({
            "id": "fallback-scrim", "type": "shape", "kind": "gradient", "effect": gradient["effect"],
            "x": gradient["x"], "y": gradient["y"], "width": gradient["width"],
            "height": gradient["height"], "zIndex": spec["stacking"]["gradientZIndex"],
            "color": None, "opacity": 1.0, "radius": 0, "angle": gradient["angle"],
            "stops": gradient["stops"], "borderColor": None, "borderWidth": 0.0, "rotation": 0,
        })
        drawn.append("fallback-scrim")
        _classify(scene, {
            "id": "component-fallback-scrim", "kind": "editableShape", "layerId": "fallback-scrim",
            "pairsWith": None, "note": "Template fallback scrim."})

    summary = {
        "kind": "template-css-only" if deliberate else "template-css-fallback", "code": code, "reason": reason[:400],
        "drawn": drawn, "omitted": spec["omitted"], "removedEffects": removed_effects,
        "templateElement": decoration["id"] if decoration else None,
    }
    scene.pop("layerPlan", None)
    updated = build(scene)
    updated["revision"] = int(plan.get("revision", 1)) + 1
    updated["corrections"] = list(plan.get("corrections") or []) + [{
        "revision": updated["revision"],
        "action": "template-css-only" if deliberate else "template-omitted-css-fallback",
        "code": code, "touched": ([decoration["id"]] if decoration else []) + drawn,
        "invalidates": [],           # nothing else: photograph, text and icons are untouched
    }]
    updated["template"]["representation"] = "css-only"
    updated["template"]["gradientOnly"] = bool(deliberate)
    updated["template"]["alphaUncertainty"] = None
    updated["template"]["fallbackActive"] = not deliberate
    updated["template"]["reason"] = (
        reason[:300] if deliberate else "CSS fallback in use: " + reason[:300])
    updated["fallback"] = spec
    scene["layerPlan"] = updated
    return scene, summary


# --------------------------------------------------------------------------
# Keeping the plan true
# --------------------------------------------------------------------------


def _classify(scene, component):
    """Add a component entry, only to a scene that has a plan to keep complete."""
    if scene.get("components"):
        scene["components"].append(component)


def sync(scene):
    """Refresh element geometry from the scene's layers after a patch.

    Returns `(scene, changed_ids)`. The revision is bumped, and the correction recorded,
    only when something moved; an unchanged scene keeps its plan byte-identical.
    """
    plan = scene.get("layerPlan")
    if not plan:
        return scene, []
    layers = {}
    for kind in ("textLayers", "shapeLayers", "imageLayers"):
        layers.update({l["id"]: l for l in scene.get(kind) or []})
    changed = []
    for element in plan["elements"]:
        layer = layers.get(element["id"])
        if layer is None:
            continue
        box = {k: int(layer[k]) for k in ("x", "y", "width", "height")}
        z = int(layer["zIndex"])
        if element["geometry"] != box or element["zIndex"] != z:
            element["geometry"], element["zIndex"] = box, z
            changed.append(element["id"])
    if changed:
        plan["revision"] = int(plan["revision"]) + 1
        dependents = sorted({
            e["id"] for e in plan["elements"] if set(e["dependsOn"]) & set(changed)
        } - set(changed))
        plan["corrections"].append({
            "revision": plan["revision"], "action": "geometry-synced", "code": None,
            "touched": sorted(changed), "invalidates": dependents,
        })
    return scene, changed


def check(plan):
    """Raise PlanError for a plan that contradicts itself."""
    ids = [e["id"] for e in plan["elements"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise PlanError(f"Layer plan lists {duplicates[0]!r} more than once.")
    known = set(ids)
    for element in plan["elements"]:
        if element["category"] not in CATEGORIES:
            raise PlanError(f"Unknown layer category {element['category']!r}.")
        if element["owner"] not in OWNERS or element["renderer"] not in RENDERERS:
            raise PlanError(f"Layer {element['id']!r} has an unknown owner or renderer.")
        missing = [d for d in element["dependsOn"] if d not in known]
        if missing:
            raise PlanError(f"Layer {element['id']!r} depends on unknown {missing[0]!r}.")
    templates = [e for e in plan["elements"]
                 if e["owner"] == "template" and e.get("status") == "present"]
    if len(templates) > 1:
        raise PlanError("The plan has more than one structural template raster.")
    template = plan["template"]
    leaked = sorted(set(template["excludes"]) & {e["id"] for e in templates})
    if leaked:
        raise PlanError(f"The template raster is excluded from itself: {leaked[0]!r}.")


def template_excluded_ids(plan):
    return list(plan["template"]["excludes"])
