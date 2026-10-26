"""Typography, code-owned shapes, effect ownership, dependencies and design state.

Pure tests: no pipeline, no providers. The behaviours that need a running pipeline
(routing, the lightweight repair path, edits) are in test_repair_pipeline.py.
"""

import copy

import pytest

from lib import assemble_html, dependencies, design_state, scene_patch, schemas
from lib.errors import AssetInvalid, InvalidScene
from tests.test_pipeline import CANVAS, SCENE

URLS = {"background": "https://x/b.png", "decoration": "https://x/d.png"}
FONTS = {"Heebo"}


def scene():
    return copy.deepcopy(SCENE)


def html_of(doc):
    return assemble_html.assemble(doc, URLS, CANVAS)


def parse(doc):
    return schemas.parse_scene(doc, 1024, 1024, allowed_fonts=FONTS)


GRADIENT = {
    "id": "grad", "type": "shape", "kind": "gradient", "effect": "g1",
    "x": 0, "y": 512, "width": 1024, "height": 512, "zIndex": 0,
    "color": None, "opacity": 1.0, "radius": 0, "angle": 180,
    "stops": [{"color": "#000000", "opacity": 0.0, "position": 0.0},
              {"color": "#000000", "opacity": 0.8, "position": 1.0}],
    "borderColor": None, "borderWidth": 0.0, "rotation": 0,
}


# --- typography: represented in the scene and emitted in the HTML -------------


def test_an_outlined_headline_is_represented_in_the_scene_and_emitted_as_text():
    doc = scene()
    doc["textLayers"][0].update(strokeColor="#FFFFFF", strokeWidth=3)
    parsed = parse(doc).model_dump(mode="json")
    layer = parsed["textLayers"][0]
    assert (layer["fill"], layer["strokeColor"], layer["strokeWidth"]) == ("solid", "#FFFFFF", 3)

    html = html_of(parsed)
    assert "-webkit-text-stroke:3px #FFFFFF" in html and "paint-order:stroke fill" in html
    # Still real, editable text: never a raster headline.
    assert '<div class="text">חופשת חורף באיטליה</div>' in html
    assert "-webkit-text-fill-color" not in html          # a solid fill stays solid


def test_hollow_lettering_uses_a_transparent_fill_with_an_outline():
    doc = scene()
    doc["textLayers"][0].update(fill="transparent", strokeColor="#FFFFFF", strokeWidth=2)
    html = html_of(parse(doc).model_dump(mode="json"))
    assert "-webkit-text-fill-color:transparent" in html
    assert "-webkit-text-stroke:2px #FFFFFF" in html


@pytest.mark.parametrize("update", [
    {"fill": "transparent"},                                  # invisible text
    {"strokeWidth": 3},                                       # width without a colour
    {"strokeColor": "#FFFFFF"},                               # colour without a width
    {"strokeColor": "red", "strokeWidth": 2},                 # not a hex colour
])
def test_unrepresentable_typography_is_refused(update):
    doc = scene()
    doc["textLayers"][0].update(update)
    with pytest.raises(InvalidScene):
        parse(doc)


def test_letter_spacing_wrap_and_overflow_are_emitted_only_when_not_default():
    doc = scene()
    doc["textLayers"][2].update(letterSpacing=1.5, wrap="nowrap")
    html = html_of(parse(doc).model_dump(mode="json"))
    assert "letter-spacing:1.5px" in html and "white-space:nowrap" in html
    assert html.count("letter-spacing") == 1               # the other layers are plain


def test_the_column_position_does_not_depend_on_text_direction():
    """Hebrew must not mirror the layout: direction changes how the text runs, never
    where its box sits. Position and alignment are physical."""
    rtl, ltr = scene(), scene()
    for layer in ltr["textLayers"]:
        layer["direction"] = "ltr"
    a, b = html_of(parse(rtl).model_dump(mode="json")), html_of(parse(ltr).model_dump(mode="json"))
    for fragment in ("left:120px;top:140px;width:700px", "left:120px;top:320px;width:700px"):
        assert fragment in a and fragment in b
    assert "text-align:right" in a and "text-align:right" in b
    assert "text-align:start" not in a and "text-align:end" not in a


def test_existing_scene_documents_still_render_with_compatible_defaults():
    """A scene written before typography, shapes and effects existed."""
    legacy = scene()
    assert "letterSpacing" not in legacy["textLayers"][0] and "shapeLayers" not in legacy
    parsed = parse(legacy)
    layer = parsed.textLayers[0]
    assert (layer.letterSpacing, layer.fill, layer.strokeColor, layer.strokeWidth,
            layer.wrap, layer.overflow) == (0.0, "solid", None, 0.0, "wrap", "shrink")
    assert parsed.shapeLayers == [] and parsed.effects == []
    html = html_of(parsed.model_dump(mode="json"))
    for emitted in ("letter-spacing", "text-stroke", "text-fill-color", "nowrap", "data-shape"):
        assert emitted not in html
    assert html == html_of(legacy)                          # identical bytes either way


# --- code-owned shapes and effect ownership -----------------------------------


def with_gradient(owner="layout", extra_effects=(), shapes=(GRADIENT,)):
    doc = scene()
    doc["shapeLayers"] = [copy.deepcopy(s) for s in shapes]
    doc["effects"] = [{"id": "g1", "kind": "readabilityGradient", "owner": owner, "note": None},
                      *extra_effects]
    return doc


def test_a_layout_owned_gradient_is_emitted_once_in_code():
    html = html_of(parse(with_gradient()).model_dump(mode="json"))
    assert html.count('data-effect="g1"') == 1
    assert "linear-gradient(180deg" in html
    # Between the photograph and the overlay, as its zIndex says.
    assert html.index('class="background') < html.index("data-shape") < html.index("badges")


def test_a_gradient_already_baked_into_the_photo_is_not_drawn_a_second_time():
    with pytest.raises(InvalidScene, match="duplicate"):
        parse(with_gradient(owner="photo"))


def test_an_effect_owned_by_the_layout_with_nothing_drawing_it_is_refused():
    with pytest.raises(InvalidScene, match="no shape draws it"):
        parse(with_gradient(shapes=()))


def test_a_shape_without_a_declared_owner_is_refused():
    doc = with_gradient()
    doc["effects"] = []
    with pytest.raises(InvalidScene, match="no declared owner"):
        parse(doc)


def test_there_may_be_only_one_readability_gradient():
    second = {**GRADIENT, "id": "grad2", "effect": "g2"}
    extra = [{"id": "g2", "kind": "readabilityGradient", "owner": "layout", "note": None}]
    with pytest.raises(InvalidScene, match="only one"):
        parse(with_gradient(extra_effects=extra, shapes=(GRADIENT, second)))


def test_adding_a_missing_gradient_declares_one_owner_and_one_shape():
    added = scene_patch.add_shape(scene(), "gradient-bottom")
    parse(added)                                             # valid, ownership consistent
    assert [e["owner"] for e in added["effects"]] == ["layout"]
    assert len(added["shapeLayers"]) == 1 and added["shapeLayers"][0]["kind"] == "gradient"
    assert html_of(added).count("data-shape") == 1


@pytest.mark.parametrize("owner", ["layout", "photo", "overlay"])
def test_a_second_gradient_is_never_added_whoever_owns_the_first(owner):
    doc = with_gradient(owner=owner, shapes=(GRADIENT,) if owner == "layout" else ())
    with pytest.raises(InvalidScene, match="already has a readabilityGradient"):
        scene_patch.add_shape(doc, "gradient-top")


def test_the_gradient_sits_between_the_photo_and_everything_above_it():
    added = scene_patch.add_shape(scene(), "gradient-bottom")
    z = added["shapeLayers"][0]["zIndex"]
    html = html_of(added)
    # Above the photograph, below the overlay artwork and the text.
    assert html.index('class="background') < html.index("data-shape") < html.index("badges")
    assert all(z < layer["zIndex"] for layer in added["textLayers"])


# --- the editor contract for shapes -------------------------------------------
#
# A shape is a layer the customer can select and restyle, so it is emitted in the
# editor's own structure (`.words` with an empty `.text`) rather than in a private
# one. `data-role="decoration"` is what exempts it from the copy checks, and
# nothing else is exempt.


CTA_PANEL = {
    "id": "cta_background", "type": "shape", "kind": "panel", "effect": "ctaBackground",
    "x": 80, "y": 840, "width": 360, "height": 96, "zIndex": 9,
    "color": "#FFD43B", "opacity": 0.9, "radius": 48, "angle": 180, "stops": [],
    "borderColor": None, "borderWidth": 0.0, "rotation": 0,
}


def with_panel():
    doc = scene()
    doc["shapeLayers"] = [copy.deepcopy(CTA_PANEL)]
    doc["effects"] = [{"id": "ctaBackground", "kind": "ctaBackground",
                       "owner": "layout", "note": None}]
    return doc


def test_a_shape_is_emitted_as_an_empty_words_decoration_under_box():
    html = html_of(parse(with_panel()).model_dump(mode="json"))
    block = (
        '      <div class="words d3001" id="cta-background" data-role="decoration" '
        'data-shape="panel" data-effect="ctaBackground" '
        "style=\"position:absolute;left:80px;top:840px;width:360px;height:96px;"
        'box-sizing:border-box;opacity:0.9;background-color:#FFD43B;'
        'border-radius:48px;z-index:9;">\n'
        '        <div class="text"></div>\n'
        "      </div>"
    )
    assert block in html
    # A direct child of .box, like every other layer: no wrapper to select through.
    assert '\n      <div class="words d3001"' in html.split('class="box"', 1)[1]
    assert 'class="shape' not in html


def test_shape_and_text_classes_and_ids_are_unique():
    import re

    doc = with_panel()
    doc["shapeLayers"].append({**copy.deepcopy(CTA_PANEL), "id": "cta background",
                               "effect": "ctaBackground", "zIndex": 8})
    html = html_of(parse(doc).model_dump(mode="json"))

    ids = re.findall(r'id="([^"]+)"', html)
    classes = re.findall(r'class="words (d\d+)', html)
    assert len(ids) == len(set(ids))
    assert len(classes) == len(set(classes))
    # Shapes are numbered from 3001, text from 1001, so the two never collide.
    assert sorted(classes) == ["d1001", "d1002", "d1003", "d3001", "d3002"]
    # Two scene ids that sanitise to the same slug still get distinct element ids.
    assert {"cta-background", "cta-background-2"} <= set(ids)


def test_a_shape_element_id_is_stable_when_another_layer_is_added():
    """Derived from the scene id, so an editor's saved reference survives."""
    one = with_panel()
    two = with_panel()
    two["shapeLayers"].insert(0, {**copy.deepcopy(GRADIENT), "zIndex": 1})
    two["effects"].append({"id": "g1", "kind": "readabilityGradient",
                           "owner": "layout", "note": None})

    assert assemble_html.shape_element_ids(one)["cta_background"] == "cta-background"
    assert assemble_html.shape_element_ids(two)["cta_background"] == "cta-background"


def test_only_decorations_are_exempt_from_empty_text_and_fitting():
    from lib import render as renderer

    source = _inspect_source()
    # The copy checks and the fit are scoped to layers that are not decorations.
    assert renderer._FIT_SCRIPT.count('.words:not([data-role="decoration"])') == 1
    # The checks, the measured rectangles, and the count.
    assert source.count('.words:not([data-role="decoration"])') == 3
    # Nothing else is exempt, and position is checked for every layer.
    assert "'.words, .badges, .background'" in source
    assert "EMPTY_TEXT" in source and "OUT_OF_BOUNDS" in source
    assert assemble_html.DECORATION_ROLE not in schemas.ALLOWED_ROLES


def _inspect_source():
    import inspect

    from lib import render as renderer

    return inspect.getsource(renderer._inspect)


def test_the_layer_map_includes_shapes_and_their_html_ids():
    from lib.stages.review import _layer_map

    doc = parse(with_panel()).model_dump(mode="json")
    entries = {layer["id"]: layer for layer in _layer_map(doc)}

    shape = entries["cta_background"]
    assert shape["kind"] == "shape" and shape["shape"] == "panel"
    assert shape["effect"] == "ctaBackground"
    assert shape["box"] == {"x": 80, "y": 840, "width": 360, "height": 96}
    # Every entry says which element it is, so a caller can act on the HTML.
    html = html_of(doc)
    for layer in entries.values():
        assert f'id="{layer["elementId"]}"' in html
    assert entries["t_title"]["elementId"] == "text-1001"
    assert shape["elementId"] == "cta-background"


def test_a_scene_without_shapes_assembles_byte_identically():
    """The contract for the existing shape-free designs is unchanged."""
    plain = scene()
    assert "shapeLayers" not in plain
    assert html_of(parse(plain).model_dump(mode="json")) == html_of(plain)
    assert "data-role=\"decoration\"" not in html_of(plain)


# --- dependencies: what a change invalidates -----------------------------------


def changed(mutate):
    after = scene()
    mutate(after)
    return dependencies.classify_changes(scene(), after)


def test_a_wording_change_invalidates_text_fit_and_composition_only():
    kinds = changed(lambda s: s["textLayers"][0].update(text="כותרת חדשה"))
    out = dependencies.invalidated_checks(kinds)
    assert kinds == {"wording"}
    assert {"textFit", "finalComposition", "mandatoryContent"} <= out
    assert not out & set(dependencies.ASSET_CHECKS)


def test_a_font_change_invalidates_typography_fit_and_render():
    kinds = changed(lambda s: s["textLayers"][0].update(fontWeight=400))
    out = dependencies.invalidated_checks(kinds)
    assert "font" in kinds and {"typography", "textFit", "render"} <= out
    assert not out & set(dependencies.ASSET_CHECKS)


def test_a_photo_change_invalidates_the_photo_and_affected_composition_checks():
    out = dependencies.invalidated_checks({"photo"})
    assert {"photoValidation", "fidelity", "compositing", "render"} <= out
    assert "composites" not in out and "overlayValidation" not in out   # overlay untouched


def test_an_image_layer_geometry_change_reruns_compositing_without_regenerating():
    kinds = changed(lambda s: s["imageLayers"][0].update(x=10))
    assert kinds == {"compositing"}
    out = dependencies.invalidated_checks(kinds)
    assert "compositing" in out and "fidelity" not in out and "photoValidation" not in out


def test_cache_keys_carry_every_input_so_nothing_is_reused_for_different_inputs():
    base = dict(background="a", concept="c", model="m", prompt="p")
    key = dependencies.cache_key("fidelity", **base)
    assert key == dependencies.cache_key("fidelity", **base)
    for field in base:
        assert key != dependencies.cache_key("fidelity", **{**base, field: "other"})
    assert key != dependencies.cache_key("composites", **base)
    # The policy version is part of the key too.
    original = dependencies.VALIDATION_POLICY_VERSION
    try:
        dependencies.VALIDATION_POLICY_VERSION = "9.9"
        assert key != dependencies.cache_key("fidelity", **base)
    finally:
        dependencies.VALIDATION_POLICY_VERSION = original


# --- design state: parts with independent versions -----------------------------

ASSETS = [{"assetId": "background", "key": "k/bg.png", "sha256": "a" * 64},
          {"assetId": "decoration", "key": "k/d.png", "sha256": "b" * 64}]


def snapshot(doc, parent=None):
    return design_state.build_snapshot(
        "job", version=(parent["version"] + 1) if parent else 1, parent=parent, scene=doc,
        assets=ASSETS, validation={}, reason="t", attempt=1,
    )


def test_a_text_edit_moves_only_the_content_version_and_keeps_the_asset_hashes():
    first = snapshot(scene())
    doc = scene()
    doc["textLayers"][0]["text"] = "כותרת אחרת"
    second = design_state.derive(first, scene=doc, reason="edit", attempt=1)
    assert second["content"]["version"] == first["content"]["version"] + 1
    assert second["layout"]["version"] == first["layout"]["version"]
    assert second["assets"]["version"] == first["assets"]["version"]
    assert second["assets"]["hash"] == first["assets"]["hash"]
    assert second["assets"]["entries"] == first["assets"]["entries"]
    assert design_state.parts_changed(first, second) == ["content"]


def test_a_typography_edit_moves_only_the_layout_version():
    first = snapshot(scene())
    doc = scene()
    doc["textLayers"][0].update(strokeColor="#FFFFFF", strokeWidth=3)
    second = design_state.derive(first, scene=doc, reason="edit", attempt=1)
    assert design_state.parts_changed(first, second) == ["layout"]


def test_a_derived_snapshot_can_never_change_an_asset():
    first = snapshot(scene())
    tampered = copy.deepcopy(first)
    tampered["assets"]["hash"] = "different"
    with pytest.raises(AssetInvalid):
        design_state.require_assets_preserved(first, tampered)


def test_the_scene_round_trips_through_its_parts():
    doc = scene()
    assert design_state.scene_of(snapshot(doc)) == doc


def test_code_owned_shapes_can_be_switched_off_entirely(monkeypatch):
    """Their editor/exporter compatibility is unconfirmed, so there is a switch."""
    import conf

    monkeypatch.setattr(conf, "CODE_SHAPES_ENABLED", False)
    with pytest.raises(InvalidScene, match="disabled"):
        scene_patch.add_shape(scene(), "gradient-bottom")
