"""What depends on what, and the keys that make a cached result safe to reuse.

Two questions come up whenever a design changes after a check has already run:

  * which checks does THIS change invalidate? (`invalidated_checks`)
  * may the result I stored earlier be reused for these exact inputs? (`cache_key`)

Both are answered from explicit tables here, not from convention. A cached result
is reused only when its key matches, and the key contains every input the result
depends on: the bytes it looked at, the prompt and model that judged them, and the
version of the validation policy. Nothing is ever reused "because nothing seemed to
change"; a key either matches or it does not.
"""

from __future__ import annotations

import hashlib
import json

from lib import artifacts

#: Bump when what a validation MEANS changes (thresholds, what is inspected).
#: Part of every cache key, so results from an older policy are never reused.
VALIDATION_POLICY_VERSION = "1.0"

# Check names. The first group is validation of the assets; the second is what runs
# on every assembled candidate.
ASSET_CHECKS = ("photoValidation", "overlayValidation", "composites", "fidelity")
CANDIDATE_CHECKS = (
    "mandatoryContent", "typography", "textFit", "bounds", "render", "finalComposition",
)
ALL_CHECKS = ASSET_CHECKS + CANDIDATE_CHECKS + ("compositing",)

#: Kind of change -> the checks it invalidates. A check not listed keeps its
#: cached result when its own inputs are unchanged.
INVALIDATES = {
    # The words changed: how they fit and whether they are all present, and how
    # the finished composition reads. Assets are untouched.
    "wording": {"mandatoryContent", "textFit", "bounds", "render", "finalComposition"},
    # A font family or weight changes glyph widths, so fit and render too.
    "font": {"typography", "mandatoryContent", "textFit", "bounds", "render", "finalComposition"},
    # Size, spacing, stroke, fill, wrap, alignment, direction.
    "typography": {"typography", "textFit", "bounds", "render", "finalComposition"},
    # Geometry of text and shapes.
    "layout": {"textFit", "bounds", "render", "finalComposition"},
    # Geometry or fit of an image layer, or the overlay's sample regions, changes
    # how the layers composite, so the compositing checks rerun - without
    # regenerating anything.
    "compositing": {"compositing", "overlayValidation", "bounds", "render", "finalComposition"},
    "photo": {"photoValidation", "fidelity", "compositing", "render", "finalComposition"},
    "overlay": {
        "overlayValidation", "composites", "fidelity", "compositing", "render",
        "finalComposition",
    },
    "shape": {"bounds", "render", "finalComposition"},
}


def invalidated_checks(kinds):
    """The union of checks invalidated by a set of change kinds."""
    out = set()
    for kind in kinds:
        out |= INVALIDATES[kind]
    return out


def classify_changes(before, after, *, assets_before=None, assets_after=None):
    """Which kinds of change turn scene `before` into scene `after`.

    Both are full scene documents. Asset hashes (a mapping of asset id to sha256)
    are compared when given, so a photo or overlay change is recognised from the
    bytes rather than from a layer property.
    """
    kinds = set()

    old_text = {l["contentKey"]: l for l in before.get("textLayers", [])}
    new_text = {l["contentKey"]: l for l in after.get("textLayers", [])}
    if set(old_text) != set(new_text):
        kinds.add("wording")
    for key in set(old_text) & set(new_text):
        a, b = old_text[key], new_text[key]
        if a["text"] != b["text"]:
            kinds.add("wording")
        if (a["fontFamily"], a["fontWeight"]) != (b["fontFamily"], b["fontWeight"]):
            kinds.add("font")
        if _typography(a) != _typography(b):
            kinds.add("typography")
        if _geometry(a) != _geometry(b):
            kinds.add("layout")

    if _by_id(before.get("shapeLayers")) != _by_id(after.get("shapeLayers")):
        kinds.add("shape")

    if _image_geometry(before) != _image_geometry(after):
        kinds.add("compositing")
    if before.get("overlaySamples") != after.get("overlaySamples"):
        kinds.add("compositing")

    if assets_before is not None and assets_after is not None:
        if assets_before.get("background") != assets_after.get("background"):
            kinds.add("photo")
        if assets_before.get("decoration") != assets_after.get("decoration"):
            kinds.add("overlay")
    return kinds


_TYPOGRAPHY_KEYS = (
    "fontSize", "lineHeight", "letterSpacing", "align", "direction", "color", "fill",
    "strokeColor", "strokeWidth", "wrap", "overflow",
)

_DEFAULTS = {
    "letterSpacing": 0.0, "fill": "solid", "strokeColor": None, "strokeWidth": 0.0,
    "wrap": "wrap", "overflow": "shrink",
}


def _typography(layer):
    return tuple(layer.get(k, _DEFAULTS.get(k)) for k in _TYPOGRAPHY_KEYS)


def _geometry(layer):
    return (layer["x"], layer["y"], layer["width"], layer["height"], layer["zIndex"])


def _by_id(layers):
    return {l["id"]: l for l in layers or []}


def _image_geometry(scene):
    return {
        l["id"]: (_geometry(l), l.get("objectFit"), l.get("objectPosition"), l.get("radius"))
        for l in scene.get("imageLayers", [])
    }


def digest(obj):
    """Stable hash of any JSON-able value."""
    return hashlib.sha256(
        json.dumps(
            obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            default=artifacts.json_default,
        ).encode("utf-8")
    ).hexdigest()


def cache_key(check, **inputs):
    """Key for a cached result: the check, the policy version and every input.

    Pass everything the result depends on - hashes of the bytes inspected, the
    model id, the hash of the prompt text. Anything left out is something a later
    change could alter without the key noticing, so leave nothing out.
    """
    return digest(
        {"check": check, "policy": VALIDATION_POLICY_VERSION, "inputs": inputs}
    )
