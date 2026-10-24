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
from lib import schemas
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
    return patched


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


def add_shape(scene, preset, *, effect_id=None):
    """Return a copy of `scene` with one preset shape added and its effect declared.

    Refuses when it would create a second owner for the effect (an existing effect
    of the same singleton kind, wherever it is owned) or when there is no stacking
    slot between the photograph and the content above it. Both are InvalidScene,
    which routing reports as an unresolved repair rather than painting twice.
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
    if effect_kind in schemas.SINGLETON_EFFECTS and any(e["kind"] == effect_kind for e in effects):
        owner = next(e["owner"] for e in effects if e["kind"] == effect_kind)
        raise InvalidScene(
            f"The design already has a {effect_kind} owned by the {owner}; adding "
            "another would draw it twice."
        )
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
    band = round((width if edge in ("left", "right") else height)
                 * (0.6 if kind == "gradient" else 0.3))
    horizontal = edge in ("left", "right")
    x = width - band if edge == "right" else 0
    y = height - band if edge == "bottom" else 0
    shape_id = f"shape-{effect_id}"
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
    shapes.append(shape)
    effects.append({"id": effect_id, "kind": effect_kind, "owner": "layout", "note": None})
    return patched
