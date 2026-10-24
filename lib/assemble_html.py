"""Deterministic HTML assembly from a validated scene.

The HTML structure is frozen in docs/CONTRACTS.md section 5 and enforced by a
golden-file test. Do not add wrapper divs, rename classes or reorder the nesting:
downstream editors parse this structure, so "improving" it breaks them.

No model writes HTML (SPEC section 6). That is what makes the output escapable
and reproducible - the same scene always assembles to the same bytes, which is
what lets FINALIZE verify the delivered HTML by hash.

Styles come from an allowlist rather than from whatever the scene happens to
contain. A scene is model output, and a model that emits
`font-family: Heebo; } </style><script>` must not be able to inject anything.
"""

from __future__ import annotations

import html

from lib.errors import AssemblyFailed

#: Roles permitted on a text layer (docs/CONTRACTS.md section 5).
#: Re-exported from schemas so there is one allowlist rather than two copies to
#: keep in step. A role accepted by the validator but missing here would pass
#: EXTRACT_SCENE and then fail assembly, several paid calls later.
from lib.schemas import ALLOWED_ROLES  # noqa: E402


def assemble(scene, assets, canvas):
    """Build the final HTML document.

    `assets` maps a layer's asset reference to its delivered URL: "background",
    "decoration" and each independent assetId.
    """
    width, height = canvas["width"], canvas["height"]
    children = _backing_layers(scene, assets, canvas) + _text_layers(scene)

    body = "\n".join(_indent(c, 6) for c in children)
    return (
        f'<div class="image container" style="opacity:1;width:{width}px;'
        f'height:{height}px;position:relative;overflow:hidden;">\n'
        '  <div class="content" style="width:100%;height:100%;position:relative;">\n'
        '    <div class="box" style="width:100%;height:100%;position:relative;">\n'
        f"{body}\n"
        "    </div>\n"
        "  </div>\n"
        "</div>"
    )


def _backing_layers(scene, assets, canvas):
    """Images and code-owned shapes, interleaved by zIndex.

    A shape may sit between the photograph and the overlay (a readability
    gradient does), so the two kinds are ordered together rather than one after
    the other. Without any shapes this is exactly the image layers, unchanged.
    """
    images = _image_layers(scene, assets, canvas)
    shapes = scene.get("shapeLayers") or []
    if not shapes:
        return images
    tagged = []
    ordered = sorted(scene["imageLayers"], key=lambda item: (item["zIndex"], item["id"]))
    for layer, html_block in zip(ordered, images):
        tagged.append((layer["zIndex"], 0, layer["id"], html_block))
    for number, shape in shape_numbers(scene):
        tagged.append((shape["zIndex"], 1, shape["id"], _shape(number, shape)))
    tagged.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in tagged]


def shape_numbers(scene):
    """Pair each shape layer with the number its element id is built from."""
    return enumerate(
        sorted(scene.get("shapeLayers") or [], key=lambda item: (item["zIndex"], item["id"])),
        start=1,
    )


def shape_element_ids(scene):
    return {shape["id"]: f"shape-{number}" for number, shape in shape_numbers(scene)}


def _shape(number, shape):
    """One code-owned element. Only allowlisted properties, each sanitised."""
    kind = _enum(shape["kind"], ("panel", "gradient", "circle", "dot", "ring", "border", "chevron"))
    style = [
        "position:absolute",
        f'left:{_px(shape["x"])}',
        f'top:{_px(shape["y"])}',
        f'width:{_px(shape["width"])}',
        f'height:{_px(shape["height"])}',
        "box-sizing:border-box",
    ]
    opacity = _unit(shape.get("opacity", 1.0))
    if opacity != 1:
        style.append(f"opacity:{opacity:g}")
    radius = _int(shape.get("radius", 0), 0, 2048)

    if kind == "gradient":
        stops = ",".join(
            f'{_rgba(stop["color"], stop.get("opacity", 1.0))} '
            f'{round(_unit(stop["position"]) * 100, 2):g}%'
            for stop in shape["stops"]
        )
        # The one place a CSS gradient is emitted: built from validated numbers
        # and colours only, never from model-supplied CSS text.
        style.append(f'background:linear-gradient({_int(shape.get("angle", 180), 0, 360)}deg,{stops})')
        if radius:
            style.append(f"border-radius:{radius}px")
    elif kind in ("panel", "circle", "dot"):
        style.append(f'background-color:{_color(shape["color"])}')
        style.append("border-radius:50%" if kind != "panel" else f"border-radius:{radius}px")
    elif kind == "ring":
        style.append(f'border:{_width(shape["borderWidth"])}px solid {_color(shape["borderColor"])}')
        style.append("border-radius:50%")
    elif kind == "border":
        style.append(f'border:{_width(shape["borderWidth"])}px solid {_color(shape["borderColor"])}')
        style.append(f"border-radius:{radius}px")
    else:  # chevron
        edge = f'{_width(shape["borderWidth"])}px solid {_color(shape["borderColor"])}'
        style.append(f"border-right:{edge}")
        style.append(f"border-bottom:{edge}")
        style.append(f'transform:rotate({_int(shape.get("rotation", 0), 0, 359)}deg)')
    style.append(f"z-index:{_z(shape)}")

    return (
        f'<div class="shape s{number}" id="shape-{number}" data-shape="{_attr(kind)}" '
        f'data-effect="{_attr(shape["effect"])}" style="{";".join(style)};"></div>'
    )


def _image_layers(scene, assets, canvas):
    """Emit the photograph, the overlay group and any independent imagery.

    Ordered by zIndex so the source order matches the visual stacking, which
    makes the output readable to a human editor even though z-index governs.
    """
    out = []
    group = 0
    layers = sorted(scene["imageLayers"], key=lambda item: (item["zIndex"], item["id"]))

    for layer in layers:
        url = _asset_url(layer, assets)
        if layer["type"] == "background":
            out.append(
                f'<img class="background" id="background-1" src="{_attr(url)}" alt="" '
                f'style="position:absolute;left:0;top:0;'
                f'width:{canvas["width"]}px;height:{canvas["height"]}px;'
                f'object-fit:{_fit(layer)};z-index:{_z(layer)};">'
            )
            continue

        group += 1
        out.append(
            f'<div class="badges b{group}" id="decorations-{group}" '
            f'data-asset-type="image" style="position:absolute;'
            f'left:{_px(layer["x"])};top:{_px(layer["y"])};'
            f'width:{_px(layer["width"])};height:{_px(layer["height"])};'
            f'z-index:{_z(layer)};">\n'
            f'  <img src="{_attr(url)}" alt="" style="display:block;width:100%;'
            f'height:100%;object-fit:{_fit(layer)};">\n'
            "</div>"
        )
    return out


def _numbered_text_layers(scene):
    """Pair each text layer with the number its element id is built from."""
    return enumerate(
        sorted(scene["textLayers"], key=lambda item: (item["zIndex"], item["id"])),
        start=1001,
    )


def element_ids(scene):
    """Map each scene text layer id to the element id it is emitted as.

    The emitted HTML carries no reference back to the scene - its shape is fixed
    by docs/CONTRACTS.md and adding an attribute would break the contract - so
    anything that measures the rendered document and wants to act on the scene
    has to recover the correspondence through the same ordering used to emit it.
    """
    return {layer["id"]: f"text-{index}" for index, layer in _numbered_text_layers(scene)}


def _text_layers(scene):
    """Emit one `.words` block per text layer.

    All styling sits on the outer element; the inner `.text` div holds plain
    copy only, which is what makes the text editable without touching layout.
    """
    out = []
    for index, layer in _numbered_text_layers(scene):
        role = layer["role"]
        if role not in ALLOWED_ROLES:
            raise AssemblyFailed(
                f"Text layer {layer['id']!r} uses role {role!r}, which is not "
                "an allowed role."
            )
        style = ";".join(
            [
                "position:absolute",
                f'left:{_px(layer["x"])}',
                f'top:{_px(layer["y"])}',
                f'width:{_px(layer["width"])}',
                f'height:{_px(layer["height"])}',
                f'font-family:{_font(layer["fontFamily"])}',
                f'font-size:{_px(layer["fontSize"])}',
                f'font-weight:{_int(layer["fontWeight"], 100, 900)}',
                f'color:{_color(layer["color"])}',
                # Physical left/right/center, never start/end: the box stays in its
                # column whatever the writing direction, so Hebrew does not
                # mirror a layout that was measured from the left.
                f'text-align:{_enum(layer["align"], ("left", "right", "center"))}',
                f'direction:{_enum(layer["direction"], ("rtl", "ltr"))}',
                f'line-height:{_line_height(layer["lineHeight"])}',
                *_typography(layer),
                "box-sizing:border-box",
                f"z-index:{_z(layer)}",
            ]
        )
        out.append(
            f'<div class="words d{index} fxs" id="text-{index}" '
            f'data-role="{_attr(role)}" style="{style};">\n'
            f'  <div class="text">{html.escape(layer["text"])}</div>\n'
            "</div>"
        )
    return out


def _typography(layer):
    """Declarations for the typography beyond the basics, only when non-default.

    A layer that uses none of them emits nothing here, so documents written before
    these properties existed assemble to byte-identical HTML (and the same hash).
    """
    out = []
    spacing = _number(layer.get("letterSpacing", 0.0), -10, 40, "letterSpacing")
    if spacing:
        out.append(f"letter-spacing:{spacing:g}px")
    if layer.get("wrap", "wrap") == "nowrap":
        out.append("white-space:nowrap")
    elif layer.get("wrap", "wrap") != "wrap":
        raise AssemblyFailed(f"wrap {layer.get('wrap')!r} is not supported.")
    stroke = _number(layer.get("strokeWidth", 0.0), 0, 24, "strokeWidth")
    if stroke:
        # Editable outline text: the glyphs stay real text, only painted with a
        # stroke. paint-order puts the stroke behind the fill so a solid fill is
        # not eaten into by the inner half of the line.
        out.append(f'-webkit-text-stroke:{stroke:g}px {_color(layer.get("strokeColor"))}')
        if layer.get("fill", "solid") == "solid":
            out.append("paint-order:stroke fill")
    fill = layer.get("fill", "solid")
    if fill == "transparent":
        out.append("-webkit-text-fill-color:transparent")
    elif fill != "solid":
        raise AssemblyFailed(f"fill {fill!r} is not supported.")
    return out


def _number(value, low, high, label):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AssemblyFailed(f"{label} {value!r} is not a number.") from exc
    if not low <= number <= high:
        raise AssemblyFailed(f"{label} {number} is outside {low}..{high}.")
    return number


def _width(value):
    return f"{_number(value, 0.1, 48, 'borderWidth'):g}"


def _unit(value):
    return _number(value, 0, 1, "value")


def _rgba(color, opacity):
    color = _color(color)
    digits = color[1:]
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    r, g, b = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{_unit(opacity):g})"


def _asset_url(layer, assets):
    if layer["type"] == "background":
        key = "background"
    elif layer["type"] == "decoration":
        key = "decoration"
    else:
        key = layer.get("assetId")
    url = assets.get(key)
    if not url:
        raise AssemblyFailed(
            f"Layer {layer['id']!r} references asset {key!r}, which has no "
            "delivered URL."
        )
    return _https_url(url)


# --- value sanitizers ------------------------------------------------------
#
# Each one accepts only a known-safe shape rather than escaping a hostile one,
# because a style attribute has no escaping that makes arbitrary text safe.


def _https_url(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise AssemblyFailed(f"Asset URL is not https: {url!r}")
    if any(c in url for c in '"\'<>\n\r '):
        raise AssemblyFailed("Asset URL contains characters that cannot be embedded.")
    return url


def _attr(value):
    return html.escape(str(value), quote=True)


def _px(value):
    return f"{_int(value, -10000, 10000)}px"


def _int(value, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AssemblyFailed(f"Expected a number, got {value!r}.") from exc
    if not low <= number <= high:
        raise AssemblyFailed(f"Value {number} is outside {low}..{high}.")
    return number


def _z(layer):
    return _int(layer["zIndex"], 0, 100)


def _fit(layer):
    return _enum(layer.get("objectFit", "contain"), ("contain", "cover"))


def _enum(value, allowed):
    if value not in allowed:
        raise AssemblyFailed(f"Value {value!r} is not one of {allowed}.")
    return value


def _color(value):
    if (
        not isinstance(value, str)
        or not value.startswith("#")
        or len(value) not in (4, 7)
        or any(c not in "0123456789abcdefABCDEF" for c in value[1:])
    ):
        raise AssemblyFailed(f"Colour {value!r} is not a #RGB or #RRGGBB value.")
    return value


def _font(value):
    """Allow only plain family names, then append the generic fallback.

    A font family is the one style value that legitimately contains arbitrary
    text, so it gets the strictest check.
    """
    if not isinstance(value, str) or not value.replace(" ", "").isalnum():
        raise AssemblyFailed(f"Font family {value!r} contains unsupported characters.")
    return f"{value},Arial" if value != "Arial" else "Arial"


def _line_height(value):
    """A multiplier, never a pixel value (docs/CONTRACTS.md section 6).

    Emitting a unitless number is what keeps it a multiplier: "1.12px" would be
    a 1-pixel line height and the text would collapse into itself.
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AssemblyFailed(f"lineHeight {value!r} is not a number.") from exc
    if not 0.5 <= number <= 3.0:
        raise AssemblyFailed(
            f"lineHeight {number} is outside 0.5..3.0; it looks like a pixel "
            "value rather than a multiplier."
        )
    return f"{number:g}"


def _indent(block, spaces):
    pad = " " * spaces
    return "\n".join(pad + line for line in block.split("\n"))
