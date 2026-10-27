"""Typed, validated patches to a scene document.

A repair, or a user edit, changes a few named properties of a few named layers. It
never regenerates the document. This module is the one place that knows which
properties may be changed on which kind of layer, what type each one is, and how
to apply a change without letting an invalid value reach the HTML.

Values arrive as strings from a model (a review's `constraints`) or as JSON from a
client (an edit request), so each is parsed against an explicit specification
rather than coerced. An unknown property, an unparseable value or an out-of-range
number is refused, which is what lets routing treat a bad constraint as an
unresolved classification instead of guessing.

Nothing here touches text content: wording comes from the approved copy manifest
(`apply_content`), never from the value of a constraint.
"""

from __future__ import annotations

import copy
import re

import conf
from lib import alignment, readability, schemas
from lib.errors import InvalidScene
from lib.schemas import HEX_COLOR

_ALIGN = ("left", "right", "center")
_DIRECTION = ("rtl", "ltr")
_FILL = ("solid", "transparent")
_WRAP = ("wrap", "nowrap")
_OVERFLOW = ("shrink", "visible")
_FIT = ("cover", "contain")
_POSITION = re.compile(r"^(?:left|right|center|top|bottom|\d{1,3}%)(?: (?:left|right|center|top|bottom|\d{1,3}%))?$")

#: property -> (kind, argument). kinds: int(low, high), float(low, high),
#: enum(values), hex, hexOrNone, font, position.
TEXT_TYPOGRAPHY = {
    "fontFamily": ("font", None),
    "fontWeight": ("int", (100, 900)),
    "fontSize": ("int", (4, 512)),
    "lineHeight": ("float", (0.5, 4.0)),
    "letterSpacing": ("float", (-10.0, 40.0)),
    "align": ("enum", _ALIGN),
    "direction": ("enum", _DIRECTION),
    "color": ("hex", None),
    "fill": ("enum", _FILL),
    "strokeColor": ("hexOrNone", None),
    "strokeWidth": ("float", (0.0, 24.0)),
    "wrap": ("enum", _WRAP),
    "overflow": ("enum", _OVERFLOW),
}

_GEOMETRY = {
    "x": ("int", (0, 4096)),
    "y": ("int", (0, 4096)),
    "width": ("int", (1, 4096)),
    "height": ("int", (1, 4096)),
    "zIndex": ("int", (0, 100)),
}

SHAPE_PROPERTIES = {
    **_GEOMETRY,
    "opacity": ("float", (0.0, 1.0)),
    "radius": ("int", (0, 2048)),
    "angle": ("int", (0, 360)),
    "color": ("hex", None),
    "borderColor": ("hex", None),
    "borderWidth": ("float", (0.0, 48.0)),
    "rotation": ("int", (0, 315)),
    # Gradient strength is the opacity of the darkest stop (the others scale with
    # it); a review cannot hand over a CSS gradient string, only this number.
    "gradientStrength": ("float", (0.0, 1.0)),
}

IMAGE_PROPERTIES = {
    **_GEOMETRY,
    "objectFit": ("enum", _FIT),
    "objectPosition": ("position", None),
}

#: What each repair target is allowed to change, per kind of layer.
TYPOGRAPHY_TARGET = {"text": TEXT_TYPOGRAPHY}
LAYOUT_TARGET = {"text": _GEOMETRY, "shape": SHAPE_PROPERTIES, "image": IMAGE_PROPERTIES}


#: How a property is described to the reviewer, where the name alone is not
#: enough to use it correctly. Everything else is listed by name and bounds.
_PROPERTY_NOTES = {
    # The installed families, from the same setting the executor validates
    # against: a reviewer that asks for a face nobody has is a finding no repair
    # can apply, and the whole design fails for one font name.
    "fontFamily": "only " + " or ".join(conf.APPROVED_FONT_FAMILIES),
    "fill": "transparent only for hollow lettering",
    "strokeWidth": "outline thickness in px",
    "letterSpacing": "extra space between letters in px",
    "gradientStrength": "opacity of a gradient's darkest end; never a CSS string",
    "objectPosition": 'e.g. "center", "50% 20%"',
    "rotation": "a multiple of 45 degrees",
    "angle": "gradient direction in CSS degrees; 180 is top to bottom",
}

#: Which kinds of layer each heading covers, for the prompt.
_KIND_LABEL = {"text": "text layers", "shape": "shapes", "image": "image layers"}


def supported_properties(target):
    """Every property name a `target` correction may set, on any kind of layer."""
    table = TYPOGRAPHY_TARGET if target == "typography" else LAYOUT_TARGET
    names = set()
    for properties in table.values():
        names |= set(properties)
    return names


def property_help(target):
    """The properties a `target` correction may set, as prompt text.

    Generated from the same table the executor enforces, so the reviewer is never
    told about a property that would be rejected, and a property added here is
    offered without a second edit to the prompt. A reviewer asking for something
    outside this list is refused by name rather than silently ignored.
    """
    table = TYPOGRAPHY_TARGET if target == "typography" else LAYOUT_TARGET
    lines = []
    for kind, properties in table.items():
        names = []
        for name, spec in sorted(properties.items()):
            parts = [p for p in (_PROPERTY_NOTES.get(name), _bounds(spec)) if p]
            names.append(f"{name} ({'; '.join(parts)})" if parts else name)
        lines.append(f"on {_KIND_LABEL[kind]}: " + ", ".join(names))
    return "\n      ".join(lines)


def _bounds(spec):
    """A property's accepted values, as prompt text.

    Read from the specification the executor validates against. Measured live: a
    review asked for a letterSpacing of -12 where -10 is the limit, and the whole
    design failed because one number in one correction was out of range.
    """
    kind, argument = spec
    if kind in ("int", "float") and argument:
        low, high = argument
        return f"{low} to {high}"
    if kind == "enum":
        return " or ".join(f'"{value}"' for value in argument)
    return None


def layer_kind(scene, layer_id):
    """("text"|"shape"|"image", layer) for an id, or (None, None)."""
    for kind, key in (("text", "textLayers"), ("shape", "shapeLayers"), ("image", "imageLayers")):
        for layer in scene.get(key) or []:
            if layer["id"] == layer_id:
                return kind, layer
    return None, None


def parse_value(spec, raw, *, font_names=()):
    """Parse one value against its specification, or raise ValueError."""
    kind, arg = spec
    if kind == "int":
        number = _number(raw)
        if number != int(number):
            raise ValueError(f"{raw!r} is not a whole number")
        number = int(number)
        _check_range(number, arg)
        return number
    if kind == "float":
        number = float(_number(raw))
        _check_range(number, arg)
        return number
    if kind == "enum":
        if raw not in arg:
            raise ValueError(f"{raw!r} is not one of {', '.join(arg)}")
        return raw
    if kind == "hex":
        if not isinstance(raw, str) or not HEX_COLOR.match(raw):
            raise ValueError(f"{raw!r} is not a #RGB or #RRGGBB colour")
        return raw
    if kind == "hexOrNone":
        if raw is None or (isinstance(raw, str) and raw.lower() in ("none", "null", "")):
            return None
        return parse_value(("hex", None), raw)
    if kind == "font":
        if raw not in font_names:
            raise ValueError(f"{raw!r} is not an installed, approved font")
        return raw
    if kind == "position":
        if not isinstance(raw, str) or not _POSITION.match(raw):
            raise ValueError(f"{raw!r} is not a supported object position")
        return raw
    raise ValueError(f"unsupported property type {kind!r}")


def _number(raw):
    if isinstance(raw, bool):
        raise ValueError(f"{raw!r} is not a number")
    if isinstance(raw, (int, float)):
        return raw
    try:
        return float(str(raw).strip().removesuffix("px"))
    except (TypeError, ValueError):
        raise ValueError(f"{raw!r} is not a number") from None


def _check_range(number, bounds):
    low, high = bounds
    if not low <= number <= high:
        raise ValueError(f"{number} is outside {low}..{high}")


def allowed_properties(target, kind):
    table = TYPOGRAPHY_TARGET if target == "typography" else LAYOUT_TARGET
    return table.get(kind, {})


def apply_properties(scene, layer_id, properties, *, target, font_names=()):
    """Return a copy of `scene` with `properties` applied to one layer.

    `properties` maps name -> raw value. Raises InvalidScene for an unknown
    layer, a property the target may not change on that kind of layer, or a value
    that does not parse. The input scene is never modified.
    """
    kind, _ = layer_kind(scene, layer_id)
    if kind is None:
        raise InvalidScene(f"No layer {layer_id!r} to change.")
    allowed = allowed_properties(target, kind)
    patched = copy.deepcopy(scene)
    _, layer = layer_kind(patched, layer_id)
    if kind == "text":
        _guard_outline(layer, properties)
    before = dict(layer)
    for name, raw in properties.items():
        spec = allowed.get(name)
        if spec is None:
            raise InvalidScene(
                f"{name!r} cannot be changed on {kind} layer {layer_id!r} by a "
                f"{target} correction."
            )
        try:
            value = parse_value(spec, raw, font_names=font_names)
        except ValueError as exc:
            raise InvalidScene(f"{name} on {layer_id!r}: {exc}") from None
        if name == "gradientStrength":
            _set_gradient_strength(layer, value)
        else:
            layer[name] = value
            # A position or alignment someone chose on purpose is not a drift to be
            # corrected: the layer leaves its alignment group, or assembly would
            # put it straight back. A width change keeps it - the shared edge is
            # exactly what makes widths free to differ.
            if kind == "text" and name in ("x", "align"):
                alignment.release(patched, layer_id)
    if kind == "text" and "overflow" not in properties and \
            any(layer.get(name) != before.get(name) for name in SIZING_PROPERTIES):
        # A repair that sets the size, tracking, weight or line height of a line has decided
        # what it should look like. The renderer's automatic fitting would shrink
        # it back to whatever fits its box and the repair would change nothing;
        # keeping the size makes an overflow visible instead, where it is measured
        # and answered by widening the box.
        layer["overflow"] = "visible"
    return patched


#: Typography properties whose value is a size the renderer's fitting could undo.
SIZING_PROPERTIES = ("fontSize", "letterSpacing", "fontWeight", "lineHeight")


def _guard_outline(layer, properties):
    """An outlined headline is never turned into solid text by a repair.

    Solid lettering is the easy way to make an outline "readable", and it changes
    the lettering the customer approved. If an outline cannot be made to work within
    the bounded repair, the design is delivered with that stated as a quality issue,
    not quietly altered.
    """
    if layer.get("fill") == "transparent" and properties.get("fill") == "solid":
        raise InvalidScene(
            f"{layer['id']!r} is outlined lettering; a repair may not replace it with "
            "solid text. Adjust its stroke, size or tracking, or report it as a quality "
            "issue."
        )


def _set_gradient_strength(layer, strength):
    if layer.get("kind") != "gradient" or not layer.get("stops"):
        raise InvalidScene(f"gradientStrength applies to a gradient, not {layer['id']!r}.")
    peak = max(stop["opacity"] for stop in layer["stops"])
    if peak <= 0:
        raise InvalidScene(f"Gradient {layer['id']!r} has no visible stop to strengthen.")
    for stop in layer["stops"]:
        stop["opacity"] = round(min(1.0, stop["opacity"] * strength / peak), 4)


def strip_text(scene):
    """The layout document: the scene without any copy."""
    layout = copy.deepcopy(scene)
    for layer in layout["textLayers"]:
        layer.pop("text", None)
    return layout


def compose_scene(layout, content_items):
    """Rebuild a full scene from a layout document and its content items."""
    by_key = {item["contentKey"]: item["text"] for item in content_items}
    scene = copy.deepcopy(layout)
    for layer in scene["textLayers"]:
        layer["text"] = by_key[layer["contentKey"]]
    return scene


def apply_content(scene, approved_copy):
    """Set every text layer's wording to the approved copy, exactly.

    Wording is never taken from a correction's value: the approved copy manifest
    is the only legitimate source, so a content repair cannot introduce text.
    Returns `(scene, changed contentKeys)`.
    """
    approved = {item["contentKey"]: item["text"] for item in approved_copy}
    patched = copy.deepcopy(scene)
    changed = []
    for layer in patched["textLayers"]:
        want = approved.get(layer["contentKey"])
        if want is not None and layer["text"] != want:
            layer["text"] = want
            changed.append(layer["contentKey"])
    return patched, changed


#: Shapes a layout repair may ADD, as presets. A preset fixes the kind, effect kind
#: and geometry rule, so the model asks for "a bottom gradient" and cannot invent
#: arbitrary new artwork. Each effect has one owner (the layout) and, for the
#: readability gradient, there may be only one per design.
SHAPE_PRESETS = {
    "gradient-bottom": ("gradient", "readabilityGradient", "bottom"),
    "gradient-top": ("gradient", "readabilityGradient", "top"),
    "gradient-left": ("gradient", "readabilityGradient", "left"),
    "gradient-right": ("gradient", "readabilityGradient", "right"),
    "panel-bottom": ("panel", "panel", "bottom"),
    "panel-top": ("panel", "panel", "top"),
}


def shape_id_for(effect_id):
    """The layer id `add_shape` gives the shape that draws `effect_id`.

    Named here so a repair can say which element its addition will touch without
    applying it first.
    """
    return f"shape-{effect_id}"


def add_shape(scene, preset, *, effect_id=None, take_over=False, fitted=None):
    """Return a copy of `scene` with one preset shape added and its effect declared.

    Refuses when it would create a second owner for the effect (an existing effect
    of the same singleton kind, wherever it is owned) or when there is no stacking
    slot between the photograph and the content above it. Both are InvalidScene,
    which routing reports as an unresolved repair rather than painting twice.

    `take_over` is for the one case where that refusal is wrong: a review has
    found that the asset said to own the effect does not actually contain it. The
    effect is then moved to the layout rather than declared twice. It is never
    inferred from the scene - a photograph that really does carry its gradient
    looks identical from here - so only a finding can ask for it.

    `fitted` is the geometry measured by lib/readability.fit (edge, box, angle,
    colour, stops). It replaces the preset's assumed band and stops, so the
    gradient matches the concept and the copy it serves instead of a fixed 60%.
    A preset used without it still sizes its band from where the copy sits.
    """
    if not conf.CODE_SHAPES_ENABLED:
        raise InvalidScene(
            "Code-owned shapes are disabled in this environment (CODE_SHAPES_ENABLED)."
        )
    spec = SHAPE_PRESETS.get(preset)
    if spec is None:
        raise InvalidScene(f"{preset!r} is not a shape preset ({', '.join(SHAPE_PRESETS)}).")
    kind, effect_kind, edge = spec
    patched = copy.deepcopy(scene)
    effects = patched.setdefault("effects", [])
    shapes = patched.setdefault("shapeLayers", [])
    drawn = {s.get("effect") for s in shapes}
    same_kind = [e for e in effects if e["kind"] == effect_kind]
    taken_over = (
        next((e for e in same_kind if e["id"] not in drawn), None)
        if take_over else None
    )
    if effect_kind in schemas.SINGLETON_EFFECTS and same_kind and taken_over is None:
        owner = same_kind[0]["owner"]
        raise InvalidScene(
            f"The design already has a {effect_kind} owned by the {owner}; adding "
            "another would draw it twice."
        )
    if taken_over is not None:
        if effect_id and effect_id != taken_over["id"]:
            raise InvalidScene(
                f"The design's {effect_kind} is {taken_over['id']!r}; adding "
                f"{effect_id!r} as well would draw it twice."
            )
        effect_id = taken_over["id"]
        effects.remove(taken_over)
    effect_id = effect_id or effect_kind
    if any(e["id"] == effect_id for e in effects):
        raise InvalidScene(f"Effect {effect_id!r} already exists.")
    background = next((l for l in patched["imageLayers"] if l["type"] == "background"), None)
    if background is None:
        raise InvalidScene("There is no background layer to place the shape above.")
    # Same z-index as the photograph: at equal stacking an image layer is drawn
    # before a shape (see assemble_html._backing_layers), so the shape lands right
    # above the photograph and below everything that is higher, which is what a
    # readability gradient needs. (Integers leave no slot between 0 and 1.)
    z = background["zIndex"]
    others = [l["zIndex"] for l in patched["imageLayers"] if l is not background]
    others += [l["zIndex"] for l in patched["textLayers"]] + [l["zIndex"] for l in shapes]
    if any(o <= z for o in others):  # something sits at or below the photograph
        raise InvalidScene(
            "There is no stacking slot between the photograph and the layers above it "
            "for a new shape."
        )
    width = patched["canvas"]["width"]
    height = patched["canvas"]["height"]
    if kind == "gradient":
        band = readability.band_for(patched, edge)
    else:
        band = round((width if edge in ("left", "right") else height) * 0.3)
    horizontal = edge in ("left", "right")
    x = width - band if edge == "right" else 0
    y = height - band if edge == "bottom" else 0
    shape_id = shape_id_for(effect_id)
    if any(l["id"] == shape_id for l in shapes) or layer_kind(patched, shape_id)[0]:
        raise InvalidScene(f"Layer id {shape_id!r} is already in use.")
    # Strong enough to carry white text over a busy photograph; the dark end sits at
    # the named edge and fades toward the centre.
    dark_first = [{"color": "#000000", "opacity": 0.9, "position": 0.0},
                  {"color": "#000000", "opacity": 0.75, "position": 0.55},
                  {"color": "#000000", "opacity": 0.0, "position": 1.0}]
    dark_last = [dict(s_, position=round(1.0 - s_["position"], 2)) for s_ in reversed(dark_first)]
    angle = {"bottom": 180, "top": 0, "left": 90, "right": 270}[edge]
    stops = dark_last if edge in ("bottom", "top") else dark_first
    shape = {
        "id": shape_id, "type": "shape", "kind": kind, "effect": effect_id,
        "x": x, "y": y, "width": band if horizontal else width,
        "height": height if horizontal else band, "zIndex": z,
        "color": "#000000" if kind == "panel" else None,
        "opacity": 1.0 if kind == "gradient" else 0.55, "radius": 0,
        "angle": angle if kind == "gradient" else 180,
        "stops": stops if kind == "gradient" else [],
        "borderColor": None, "borderWidth": 0.0, "rotation": 0,
    }
    note = None
    if fitted and kind == "gradient":
        box = fitted["box"]
        shape.update({"x": box["x"], "y": box["y"], "width": box["width"],
                      "height": box["height"], "angle": fitted["angle"],
                      "stops": [dict(stop) for stop in fitted["stops"]]})
        measured = fitted.get("measured") or {}
        note = (
            f"Fitted from the concept and the cleaned photograph: {fitted['edge']} edge "
            f"at {measured.get('edgeAlpha', 0):.0%}, {box['width']}x{box['height']}px."
        )
    shapes.append(shape)
    effects.append({"id": effect_id, "kind": effect_kind, "owner": "layout", "note": note})
    if patched.get("components"):
        # The plan has to stay complete: a shape with no entry in it is read as a
        # component nobody decomposed, and the patched scene is rejected.
        patched["components"].append({
            "id": f"component-{shape_id}", "kind": "editableShape",
            "layerId": shape_id, "pairsWith": None,
            "note": f"Added by the layout to draw {effect_id}.",
        })
    return patched


def refit_gradient(scene, effect_id, fitted):
    """Return a copy of `scene` whose drawn readability gradient has the fitted geometry.

    The shape keeps its id, effect, stacking and class; only where it sits, how far it
    reaches, its direction and its stops change. None when the scene draws no gradient
    for `effect_id` (nothing to refit).
    """
    patched = copy.deepcopy(scene)
    shape = next((s for s in patched.get("shapeLayers") or []
                  if s.get("effect") == effect_id and s.get("kind") == "gradient"), None)
    if shape is None:
        return None
    box = fitted["box"]
    shape.update({"x": box["x"], "y": box["y"], "width": box["width"],
                  "height": box["height"], "angle": fitted["angle"],
                  "stops": [dict(stop) for stop in fitted["stops"]]})
    measured = fitted.get("measured") or {}
    for effect in patched.get("effects") or []:
        if effect["id"] == effect_id:
            effect["note"] = (
                f"Fitted from the concept and the cleaned photograph: {fitted['edge']} edge "
                f"at {measured.get('edgeAlpha', 0):.0%}, {box['width']}x{box['height']}px."
            )
    return patched


def rasterize_shapes(scene, layer_ids, owner, reason):
    """Give named shape layers back to the artwork that still contains them.

    The reverse of extraction, and the only honest answer when the bounded
    overlay repair cannot separate a decorative shape: the element keeps its
    appearance, the asset that still holds it becomes its declared owner, and
    the layout stops drawing a second copy. The plan is kept complete - the
    component entry moves onto the asset that carries it - so nothing becomes
    unaccounted for.

    Only shapes may be surrendered, and only ones the caller has established are
    not required to be editable (lib/validate_assets.mandatory_component_ids).
    """
    patched = copy.deepcopy(scene)
    wanted = set(layer_ids)
    present = {s["id"] for s in patched.get("shapeLayers") or []}
    missing = wanted - present
    if missing:
        raise InvalidScene(
            "Cannot give back shapes the scene does not have: "
            + ", ".join(sorted(missing))
        )
    surrendered = [s for s in patched["shapeLayers"] if s["id"] in wanted]
    patched["shapeLayers"] = [s for s in patched["shapeLayers"] if s["id"] not in wanted]

    kept_effects = {s.get("effect") for s in patched["shapeLayers"]}
    given_effects = {s.get("effect") for s in surrendered} - kept_effects
    for effect in patched.get("effects") or []:
        if effect["id"] in given_effects:
            effect["owner"] = owner
            effect["note"] = reason

    carrier = _carrier(patched, owner)
    notes = []
    for component in list(patched.get("components") or []):
        if component["layerId"] not in wanted:
            continue
        shape = next(s for s in surrendered if s["id"] == component["layerId"])
        notes.append(f"{shape['kind']} {shape['id']}")
        patched["components"].remove(component)
    if notes and carrier is not None:
        existing = carrier.get("note") or ""
        carrier["note"] = (
            f"{existing} Retained in this asset rather than as editable shapes "
            f"({reason}): {', '.join(notes)}."
        ).strip()[:800]

    # A sample that says the photograph shows through where the surrendered shape
    # is cannot survive the surrender: that area is the shape, and the asset now
    # owns it. Kept for the asset that did not take it over, so the opposite
    # asset is still held to the same expectation.
    if owner == "overlay":
        patched["overlaySamples"] = [
            sample for sample in patched.get("overlaySamples") or []
            if not (sample["kind"] == "transparent"
                    and any(_within(shape, sample) for shape in surrendered))
        ]
    return patched


def _within(outer, inner, coverage=0.8):
    """Does `outer` hold at least `coverage` of `inner`'s area?"""
    width = min(outer["x"] + outer["width"], inner["x"] + inner["width"]) \
        - max(outer["x"], inner["x"])
    height = min(outer["y"] + outer["height"], inner["y"] + inner["height"]) \
        - max(outer["y"], inner["y"])
    area = max(1, inner["width"] * inner["height"])
    return width > 0 and height > 0 and (width * height) / area >= coverage


def _carrier(scene, owner):
    """The component entry for the asset that keeps a surrendered shape."""
    layers = {l["id"]: l for l in scene.get("imageLayers") or []}
    want = "background" if owner == "photo" else "decoration"
    for component in scene.get("components") or []:
        layer = layers.get(component["layerId"])
        if layer is not None and layer.get("type") == want:
            return component
    return None
