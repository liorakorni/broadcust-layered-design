"""Route review findings to the cheapest repair that can actually fix them.

A review used to answer every blocking issue the same way: regenerate. Scene
extraction, the overlay and the photograph were all produced again, which cost
minutes and several paid image calls even when the defect was a missing outline on
a headline - and the regenerated assets then brought defects of their own.

Each issue now says WHAT is wrong in machine-readable terms:

  repairTarget             content | typography | layout | asset | concept
  affectedElementIds       which layers (or content keys, or assets)
  requiresImageGeneration  whether the fix needs a model that draws pixels
  constraints              property-level corrections, per element

This module validates those fields BEFORE anything is routed. A target that is
missing or unknown, a flag that contradicts the target, an element that does not
exist or a property that may not be changed is a classification that cannot be
trusted, and an untrusted classification never starts image generation: the issue
is reported as unresolved and the best candidate so far is kept.

Content is not negotiated: wording always comes from the approved copy manifest
(see scene_patch.apply_content), so a content repair cannot introduce text.
"""

from __future__ import annotations

import re

from lib import schemas, scene_patch
from lib.dependencies import digest

TARGETS = ("content", "typography", "layout", "asset", "concept")
LIGHT_TARGETS = ("content", "typography", "layout")

ROUTE_APPROVE = "approve"
ROUTE_LIGHT = "light"
ROUTE_ASSET = "asset"
ROUTE_CONCEPT = "concept"
ROUTE_UNRESOLVED = "unresolved"
ROUTE_EXHAUSTED = "exhausted"
ROUTE_NO_PROGRESS = "noProgress"

#: Categories whose findings may only ever be corrected from the approved copy.
_CONTENT_CATEGORIES = ("content",)


def requires_image_generation(target):
    """The only targets that may draw pixels."""
    return target in ("asset", "concept")


def route_issue(issue, ctx):
    """Classify one blocking issue.

    `ctx` carries `scene`, `copyKeys`, `assetForLayer` (image layer id -> asset id),
    `assetIds`, `fonts` and `conceptJustified`. Returns the issue's routing record:
    its `route` (light, asset, concept or unresolved), the validated `patch` for a
    light repair, and every problem found when it cannot be trusted.
    """
    target = issue.get("repairTarget")
    ids = list(issue.get("affectedElementIds") or [])
    flag = issue.get("requiresImageGeneration")
    problems = []

    if target not in TARGETS:
        problems.append(
            "no repairTarget was given" if target is None
            else f"repairTarget {target!r} is not one of {', '.join(TARGETS)}"
        )
    elif flag is None:
        problems.append("requiresImageGeneration was not given")
    elif bool(flag) != requires_image_generation(target):
        problems.append(
            f"requiresImageGeneration={bool(flag)} contradicts repairTarget {target!r}"
        )

    # A content-category finding is about the approved wording. It cannot be fixed
    # by anything except putting that wording back.
    if target in TARGETS and issue.get("category") in _CONTENT_CATEGORIES and target != "content":
        problems.append(
            f"a content finding cannot be repaired as {target!r}; wording comes from "
            "the approved copy"
        )

    record = {
        "code": issue.get("code"),
        "category": issue.get("category"),
        "affected": issue.get("affected"),
        "target": target if target in TARGETS else None,
        "affectedElementIds": ids,
        "requiresImageGeneration": flag,
        "patch": None,
        "assetTarget": None,
        "problems": problems,
        "route": ROUTE_UNRESOLVED,
    }
    if problems:
        return record

    if target == "content":
        return _route_content(record, issue, ids, ctx)
    if target in ("typography", "layout"):
        return _route_properties(record, issue, ids, ctx, target)
    if target == "asset":
        return _route_asset(record, issue, ids, ctx)
    return _route_concept(record, issue, ctx)


def _route_content(record, issue, ids, ctx):
    keys = []
    for element in ids:
        key = element if element in ctx["copyKeys"] else _key_for_layer(ctx["scene"], element)
        if key is None:
            record["problems"].append(f"{element!r} is neither an approved content key nor a text layer")
        else:
            keys.append(key)
    if not ids:
        record["problems"].append("a content repair must name the content keys it concerns")
    if record["problems"]:
        return record
    record["patch"] = {"contentKeys": sorted(set(keys))}
    record["route"] = ROUTE_LIGHT
    return record


def _undrawn_effect(scene, kind):
    """A declared effect of this kind that no shape draws, if there is one."""
    drawn = {s.get("effect") for s in scene.get("shapeLayers") or []}
    for effect in scene.get("effects") or []:
        if effect["kind"] == kind and effect["id"] not in drawn:
            return effect
    return None


def _as_shape_addition(issue, ids, scene, record):
    """Read a correction aimed at an undrawn effect as a request to draw it.

    Measured on a live run: the design declared a readability gradient as baked
    into the photograph, the separated layers did not contain it, and the review
    asked for `angle` and `gradientStrength` on the effect's id. That id is not a
    layer, so the whole finding was unroutable and a design that needed one CSS
    gradient failed. The effect nobody draws is exactly the case the layout can
    take over, so the request is translated into the addShape it should have been
    - deterministically, from the direction the correction asked for.
    """
    drawn = {s.get("effect") for s in scene.get("shapeLayers") or []}
    layers = {l["id"] for l in (scene.get("shapeLayers") or [])
              + (scene.get("textLayers") or []) + (scene.get("imageLayers") or [])}
    orphans = {
        e["id"]: e for e in scene.get("effects") or []
        if e["id"] not in drawn and e["id"] not in layers
        and e["kind"] in schemas.SINGLETON_EFFECTS
    }
    if any(c.get("property") == "addShape" for c in issue.get("constraints") or []):
        return issue, ids          # the review named a preset itself; leave it be
    # "add:<effect>" is how the review asks for something that does not exist, so
    # the prefix is not part of the id it means.
    asked = [i[4:] if str(i).startswith("add:") else i for i in map(str, ids)]
    named = [i for i in asked if i in orphans]
    shading = _about_shading(issue)
    if shading and len(asked) == 1 and not named and not _effect_of_kind(
            scene, "readabilityGradient"):
        # Measured live: the review asked for a darkening by a name of its own
        # choosing on a design that declares none. There is nothing to take over,
        # so the layout adds it under the name the review used.
        return _shape_addition_for(
            issue, {"id": asked[0], "kind": "readabilityGradient", "owner": "none"},
            scene, record, take_over=False)
    unnamed_shading = not ids and shading
    if unnamed_shading and len(orphans) == 1:
        # A finding that names nothing is normally unroutable, and stays that way.
        # The exception, measured live: the review reported the readability
        # shading as missing, in words, with no element named - there was nothing
        # to name, because the thing it wanted does not exist yet. One undrawn
        # effect and a finding about that effect leave one reading.
        named = list(orphans)
    elif unnamed_shading and not _effect_of_kind(scene, "readabilityGradient"):
        # Also measured live: the same complaint about a design that declares no
        # darkening at all, drawn or baked. Then there is nothing to take over and
        # the answer is a new one - the first case the review prompt documents.
        effect = {"id": "readabilityGradient", "kind": "readabilityGradient",
                  "owner": "none"}
        return _shape_addition_for(issue, effect, scene, record, take_over=False)
    elif len(named) != 1 or len(asked) != 1:
        return issue, ids

    effect = orphans[named[0]]
    return _shape_addition_for(issue, effect, scene, record, take_over=True)


def _effect_of_kind(scene, kind):
    return any(e["kind"] == kind for e in scene.get("effects") or [])


def _shape_addition_for(issue, effect, scene, record, *, take_over):
    """The addShape correction that draws `effect`, or the issue left as it was.

    Returns the pair `_route_properties` works on, so a translation that cannot
    be applied changes nothing and the finding stays unroutable.
    """
    ids = list(issue.get("affectedElementIds") or [])
    constraints = list(issue.get("constraints") or [])
    edge = _edge_of(constraints, issue.get("region"), _text_edge(scene))
    preset = f"{'gradient' if effect['kind'] == 'readabilityGradient' else 'panel'}-{edge}"
    if preset not in scene_patch.SHAPE_PRESETS:
        return issue, ids
    try:
        scene_patch.add_shape(scene, preset, effect_id=effect["id"],
                              take_over=take_over)
    except Exception:          # no stacking slot, shapes switched off: leave it
        return issue, ids
    translated = dict(
        issue,
        constraints=[{"elementId": f"add:{effect['id']}", "property": "addShape",
                      "value": preset, "takeOver": take_over}],
    )
    record["translated"] = {
        "from": [{k: c.get(k) for k in ("elementId", "property", "value")}
                 for c in constraints],
        "to": preset,
        "reason": (
            f"effect {effect['id']!r} is declared as owned by the "
            f"{effect['owner']} and no shape draws it, so the layout takes it over"
            if take_over else
            "the design declares no readabilityGradient at all, so the layout "
            "adds the one the finding asks for"
        ),
    }
    return translated, [f"add:{effect['id']}"]


#: A finding is about the shading only if it says so. Checked on the finding's
#: own words, because the alternative - routing an unnamed layout finding to the
#: one effect nobody draws - would answer unrelated complaints with a gradient.
_SHADING_WORDS = re.compile(
    r"gradient|shading|scrim|darken|readab|legib", re.IGNORECASE)


def _about_shading(issue):
    return bool(_SHADING_WORDS.search(" ".join(
        str(issue.get(field) or "") for field in
        ("code", "message", "affected", "impact", "suggestion"))))


def _text_edge(scene):
    """The edge a readability gradient belongs on: the side the words sit on.

    Read from the scene's own geometry rather than the finding's prose, so an
    unnamed correction still darkens the half that carries the text.
    """
    texts = scene.get("textLayers") or []
    area = sum(t["width"] * t["height"] for t in texts)
    if not area:
        return "bottom"
    weighted = lambda f: sum(f(t) * t["width"] * t["height"] for t in texts) / area
    x = weighted(lambda t: t["x"] + t["width"] / 2) / scene["canvas"]["width"]
    y = weighted(lambda t: t["y"] + t["height"] / 2) / scene["canvas"]["height"]
    if x <= 0.4:
        return "left"
    if x >= 0.6:
        return "right"
    return "bottom" if y >= 0.5 else "top"


def _edge_of(constraints, region, default="bottom"):
    """Which edge a gradient correction is about: what it asked for, or where it is."""
    angles = {"0": "top", "90": "left", "180": "bottom", "270": "right"}
    for constraint in constraints:
        if constraint.get("property") == "angle":
            edge = angles.get(str(constraint.get("value", "")).strip())
            if edge:
                return edge
    if region:
        x = float(region.get("x", 0) or 0)
        width = float(region.get("width", 0) or 0)
        y = float(region.get("y", 0) or 0)
        height = float(region.get("height", 0) or 0)
        if x >= 0.5:
            return "right"
        if x + width <= 0.5:
            return "left"
        if y >= 0.5:
            return "bottom"
        if y + height <= 0.5:
            return "top"
    return default


def _key_for_layer(scene, layer_id):
    for layer in scene["textLayers"]:
        if layer["id"] == layer_id:
            return layer["contentKey"]
    return None


def _route_properties(record, issue, ids, ctx, target):
    scene = ctx["scene"]
    if target == "layout":
        issue, ids = _as_shape_addition(issue, ids, scene, record)
    if issue.get("rerender"):
        # A mechanical check that did not run (not one that failed): the repair is
        # to assemble and render again. Nothing in the design changes.
        record["patch"] = {"rerender": True, "target": target}
        record["route"] = ROUTE_LIGHT
        return record
    constraints = list(issue.get("constraints") or [])

    # "Add a shape" is a layout correction naming a preset, never free-form
    # artwork: `add:<effect>` / `addShape` / `<preset>`. Ownership is checked now,
    # so a design that already has the effect is refused before anything runs.
    adds = [c for c in constraints if c.get("property") == "addShape"]
    constraints = [c for c in constraints if c.get("property") != "addShape"]
    ids = [i for i in ids if not str(i).startswith("add:")]
    shapes_to_add = []
    for c in adds:
        element = str(c.get("elementId") or "")
        if target != "layout" or not element.startswith("add:"):
            record["problems"].append("a shape can only be added by a layout correction naming add:<effect>")
            continue
        preset = scene_patch.SHAPE_PRESETS.get(c.get("value"))
        if preset is None:
            record["problems"].append(
                f"{c.get('value')!r} is not a shape preset ({', '.join(scene_patch.SHAPE_PRESETS)})"
            )
            continue
        # The review looked at the rendered design and found the effect absent.
        # When the scene says an asset owns it and no shape draws it, the layout
        # takes that effect over instead of declaring a second one - whatever id
        # the review chose for it. A shape already drawing it is still refused.
        orphan = _undrawn_effect(scene, preset[1])
        effect_id = element[4:] or None
        take_over = bool(c.get("takeOver")) or orphan is not None
        if orphan is not None:
            effect_id = orphan["id"]
        try:
            scene_patch.add_shape(scene, c["value"], effect_id=effect_id,
                                  take_over=take_over)
        except Exception as exc:  # InvalidScene: ownership or stacking refuses it
            record["problems"].append(str(exc))
            continue
        shapes_to_add.append({"preset": c["value"],
                              "effect": effect_id or preset[1],
                              "takeOver": take_over})
    if shapes_to_add and not record["problems"] and not constraints and not ids:
        record["patch"] = {"addShapes": shapes_to_add, "target": target}
        record["route"] = ROUTE_LIGHT
        return record

    if not ids:
        record["problems"].append(f"a {target} repair must name the elements it concerns")
    if not constraints:
        record["problems"].append(
            f"a {target} repair needs specific constraints; none were given"
        )
    if record["problems"]:
        return record

    layers = {}
    for element in ids:
        kind, _ = scene_patch.layer_kind(scene, element)
        if kind is None:
            record["problems"].append(f"element {element!r} does not exist in the design")
    for constraint in constraints:
        element = constraint.get("elementId")
        prop = constraint.get("property")
        if element not in ids:
            record["problems"].append(
                f"constraint on {element!r} names an element that is not affected"
            )
            continue
        kind, _ = scene_patch.layer_kind(scene, element)
        spec = scene_patch.allowed_properties(target, kind).get(prop)
        if spec is None:
            record["problems"].append(
                f"{prop!r} is not a {target} property of the {kind or 'unknown'} layer {element!r}"
            )
            continue
        try:
            value = scene_patch.parse_value(spec, constraint.get("value"), font_names=ctx["fonts"])
        except ValueError as exc:
            record["problems"].append(f"{prop} on {element!r}: {exc}")
            continue
        layers.setdefault(element, {})[prop] = value
    if record["problems"] or not layers:
        if not layers and not record["problems"]:
            record["problems"].append("no usable constraint remained")
        return record
    record["patch"] = {"layers": layers, "target": target}
    if shapes_to_add:
        record["patch"]["addShapes"] = shapes_to_add
    record["route"] = ROUTE_LIGHT
    return record


def _route_asset(record, issue, ids, ctx):
    asset = None
    candidates = list(ids)
    if issue.get("assetId"):
        candidates.append(issue["assetId"])
    for element in candidates:
        asset = ctx["assetForLayer"].get(element) or (element if element in ctx["assetIds"] else None)
        if asset:
            break
    if asset is None:
        record["problems"].append("an asset repair must name an existing asset or image layer")
        return record
    record["assetTarget"] = asset
    record["patch"] = {
        "asset": asset,
        "instruction": " ".join(
            part for part in (issue.get("impact"), issue.get("suggestion")) if part
        )[:1200],
    }
    record["route"] = ROUTE_ASSET
    return record


def _route_concept(record, issue, ctx):
    if not ctx.get("conceptJustified"):
        record["problems"].append(
            "a concept repair regenerates the whole design and is only allowed when the "
            "review reports the approved concept itself as defective"
        )
        return record
    if not (issue.get("impact") or "").strip():
        record["problems"].append("a concept repair must state the impact that justifies it")
        return record
    record["route"] = ROUTE_CONCEPT
    return record


def categories_of(routed):
    """The repair categories a set of routed issues will consume."""
    return sorted({r["target"] for r in routed if r["target"]})


def fingerprint(issues):
    """What a set of blocking issues is ABOUT, independent of how it is worded.

    A model rewords the same defect between reviews (HEADLINE_TREATMENT_DRIFT one
    round, HEADLINE_HIERARCHY_DRIFT the next), so the code is deliberately left
    out: the same target on the same elements is the same failure.
    """
    return digest(
        sorted(
            {
                (
                    i.get("repairTarget") or "unrouted",
                    tuple(sorted(i.get("affectedElementIds") or [i.get("affected") or ""])),
                )
                for i in issues
            }
        )
    )


def patch_digest(routed):
    """A digest of exactly what a repair would change."""
    return digest(
        sorted(
            (r["target"], digest(r["patch"]))
            for r in routed
            if r["route"] == ROUTE_LIGHT and r["patch"]
        )
    )


def plan_route(issues, ctx, *, counts, limits, applied_patches=()):
    """Decide what to do about a review's blocking issues.

    `counts` are the repairs already spent per category and `limits` what each
    category may spend. `applied_patches` are digests of repairs already made, so a
    patch that was already applied (and evidently did not fix the issue) is never
    applied a second time. Returns the routing record stored with the review.
    """
    routed = [route_issue(issue, ctx) for issue in issues]
    light = [r for r in routed if r["route"] == ROUTE_LIGHT]
    asset = [r for r in routed if r["route"] == ROUTE_ASSET]
    concept = [r for r in routed if r["route"] == ROUTE_CONCEPT]
    unresolved = [r for r in routed if r["route"] == ROUTE_UNRESOLVED]

    def done(route, reason, **extra):
        return {
            "route": route,
            "reason": reason,
            "categories": categories_of([r for r in routed if r["route"] == route]),
            "issues": routed,
            "unresolved": [
                {"code": r["code"], "affected": r["affected"], "problems": r["problems"]}
                for r in unresolved
            ],
            "imageGenerationRequired": route in (ROUTE_ASSET, ROUTE_CONCEPT),
            **extra,
        }

    if not issues:
        return done(ROUTE_APPROVE, "no blocking issues")

    if concept:
        if counts.get("concept", 0) >= limits["concept"]:
            return done(ROUTE_EXHAUSTED, "the concept repair budget is spent")
        return done(ROUTE_CONCEPT, "the review justified regenerating the concept")

    if light:
        spent = [c for c in categories_of(light) if counts.get(c, 0) >= limits[c]]
        usable = [r for r in light if r["target"] not in spent]
        if not usable:
            return done(
                ROUTE_EXHAUSTED,
                "the repair budget is spent for " + ", ".join(spent),
                exhaustedCategories=spent,
            )
        digest_ = patch_digest(usable)
        if digest_ in set(applied_patches):
            return done(
                ROUTE_NO_PROGRESS,
                "the same correction was already applied and the issue remains",
                patchDigest=digest_,
            )
        out = done(ROUTE_LIGHT, "text/layout correction without image generation")
        out["categories"] = categories_of(usable)
        out["patchDigest"] = digest_
        return out

    if asset:
        if counts.get("asset", 0) >= limits["asset"]:
            return done(ROUTE_EXHAUSTED, "the asset repair budget is spent")
        return done(ROUTE_ASSET, "an affected asset must be regenerated")

    return done(
        ROUTE_UNRESOLVED,
        "no issue could be routed to a bounded repair; image generation was not started",
    )


# --- deterministic constraints for mechanical findings ----------------------


def overflow_constraints(layer, measured, canvas):
    """The smallest correction for text that overflows its box, or None.

    The renderer measured the overflow, so the correction is computed rather than
    asked of a model. Height overflow grows the box when there is room below it,
    otherwise tightens the line height; width overflow reduces the font size in
    proportion to how much too wide the line is, otherwise widens the box.
    Returns `(target, [constraint, ...])`.
    """
    axis = (measured or {}).get("axis")
    content = int((measured or {}).get("content") or 0)
    box = int((measured or {}).get("box") or 0)
    if not axis or content <= 0 or box <= 0:
        return None
    element = layer["id"]

    def constraint(prop, value):
        return {"elementId": element, "property": prop, "value": str(value)}

    if axis == "height":
        needed = content + 2
        if layer["y"] + needed <= canvas["height"]:
            return "layout", [constraint("height", needed)]
        if layer["lineHeight"] > 1.0:
            return "typography", [constraint("lineHeight", round(max(1.0, layer["lineHeight"] * 0.9), 2))]
        return None

    needed = content + 2
    if layer["x"] + needed <= canvas["width"] and layer.get("align") != "center":
        return "layout", [constraint("width", needed)]
    ratio = max(0.6, (box / content) * 0.98)
    size = int(layer["fontSize"] * ratio)
    if size < layer["fontSize"] and size >= 4:
        return "typography", [constraint("fontSize", size)]
    return None


def clamp_constraints(layer, canvas):
    """Pull a layer that sits outside the canvas back inside it."""
    element = layer["id"]
    width = min(layer["width"], canvas["width"])
    height = min(layer["height"], canvas["height"])
    x = min(max(layer["x"], 0), canvas["width"] - width)
    y = min(max(layer["y"], 0), canvas["height"] - height)
    out = []
    for prop, value, old in (
        ("x", x, layer["x"]), ("y", y, layer["y"]),
        ("width", width, layer["width"]), ("height", height, layer["height"]),
    ):
        if value != old:
            out.append({"elementId": element, "property": prop, "value": str(value)})
    return out


def describe_design(scene, assets):
    """The design's elements as the final reviewer should name them.

    Reviews cite elements by these ids, and routing refuses an id that is not in
    this list, so the reviewer is shown exactly what exists: each text layer with
    its content key and typography, every shape and its effect, every image layer
    and the asset it shows.
    """
    lines = ["TEXT LAYERS (id | contentKey | box | typography):"]
    for l in scene["textLayers"]:
        outline = ""
        if l.get("strokeWidth", 0):
            outline = f", stroke {l['strokeWidth']:g}px {l['strokeColor']}"
        if l.get("fill", "solid") == "transparent":
            outline += ", fill transparent"
        lines.append(
            f"- {l['id']} | {l['contentKey']} | x={l['x']} y={l['y']} w={l['width']} "
            f"h={l['height']} | {l['fontFamily']} {l['fontWeight']} {l['fontSize']}px "
            f"lh={l['lineHeight']:g} ls={l.get('letterSpacing', 0):g}px {l['align']}/"
            f"{l['direction']} color {l['color']}{outline}"
        )
    shapes = scene.get("shapeLayers") or []
    if shapes:
        lines.append("SHAPES (code-owned; id | kind | effect | box):")
        for s in shapes:
            lines.append(
                f"- {s['id']} | {s['kind']} | {s['effect']} | x={s['x']} y={s['y']} "
                f"w={s['width']} h={s['height']} opacity={s.get('opacity', 1):g}"
            )
    lines.append("IMAGE LAYERS (id | type | asset):")
    for l in scene["imageLayers"]:
        lines.append(f"- {l['id']} | {l['type']} | {l.get('assetId') or l['type']}")
    effects = scene.get("effects") or []
    if effects:
        lines.append("EFFECT OWNERS (id | kind | owner):")
        for e in effects:
            lines.append(f"- {e['id']} | {e['kind']} | {e['owner']}")
    return "\n".join(lines)


def asset_for_layers(scene):
    """Map each image layer id to the asset reference it shows."""
    mapping = {}
    for layer in scene["imageLayers"]:
        if layer["type"] == "background":
            mapping[layer["id"]] = "background"
        elif layer["type"] == "decoration":
            mapping[layer["id"]] = "decoration"
        elif layer.get("assetId"):
            mapping[layer["id"]] = layer["assetId"]
    return mapping
