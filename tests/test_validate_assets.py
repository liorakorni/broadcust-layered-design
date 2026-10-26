"""Alpha validation, calibrated against the measured probe histogram.

The synthetic overlays here deliberately top out at alpha 254 rather than 255,
because that is what the image model actually produces (docs/PROBE_RESULTS.md).
A test built on 255 would pass while the real pipeline rejected every asset.
"""

import io

import pytest
from PIL import Image

from lib import validate_assets as va
from lib.errors import AssetInvalid, InvalidTransparency

CANVAS = {"width": 200, "height": 200}

#: What the provider really emits for "fully opaque".
MODEL_OPAQUE = 254


def png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def overlay(*, opaque_box=(20, 20, 120, 120), soft_edge=True, size=200):
    """An overlay: transparent canvas with one opaque panel and a soft edge."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    left, top, right, bottom = opaque_box
    for x in range(left, right):
        for y in range(top, bottom):
            img.putpixel((x, y), (200, 30, 60, MODEL_OPAQUE))
    if soft_edge:
        for x in range(left, right):
            for offset, alpha in ((0, 128), (1, 64)):
                y = bottom + offset
                if y < size:
                    img.putpixel((x, y), (200, 30, 60, alpha))
    return img


def photo():
    """A background with enough colour variation to read as photographic."""
    img = Image.new("RGB", (200, 200))
    for x in range(200):
        for y in range(200):
            img.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    return img


def scene(samples):
    return {"overlaySamples": samples}


GOOD_SAMPLES = [
    {"kind": "opaque", "x": 40, "y": 40, "width": 40, "height": 40},
    {"kind": "transparent", "x": 150, "y": 150, "width": 40, "height": 40},
]


# --- the calibration itself ------------------------------------------------


def test_model_opaque_alpha_is_treated_as_opaque():
    """The whole module hinges on this: 254 must count as opaque.

    If this ever regresses to an == 255 test, every real overlay is rejected.
    """
    rgba, _ = va.load_rgba(png(overlay()))
    stats = va.alpha_stats(rgba)
    assert stats["opaque"] > 0.2
    assert stats["alphaMax"] == MODEL_OPAQUE


def test_normalization_lifts_the_ceiling_to_255():
    rgba, _ = va.load_rgba(png(overlay()))
    normalized = va.normalize_alpha(rgba)
    assert va.alpha_stats(normalized)["alphaMax"] == 255


def test_normalization_preserves_real_translucency():
    """A soft edge at alpha 128 must stay at 128, not get rounded to opaque."""
    rgba, _ = va.load_rgba(png(overlay()))
    normalized = va.normalize_alpha(rgba)
    assert normalized.getchannel("A").histogram()[128] > 0


# --- overlay validation ----------------------------------------------------


def test_valid_overlay_passes():
    stats = va.validate_overlay(png(overlay()), scene(GOOD_SAMPLES), CANVAS)
    assert stats["transparent"] > 0.5
    assert stats["opaque"] > 0.2


def test_flat_image_without_alpha_rejected():
    with pytest.raises(InvalidTransparency, match="no alpha channel"):
        va.validate_overlay(png(photo()), scene(GOOD_SAMPLES), CANVAS)


def test_fully_opaque_rgba_overlay_rejected():
    """The common failure: the model returns the artwork on a white rectangle."""
    img = Image.new("RGBA", (200, 200), (255, 255, 255, MODEL_OPAQUE))
    with pytest.raises(InvalidTransparency, match="effectively opaque"):
        va.validate_overlay(png(img), scene(GOOD_SAMPLES), CANVAS)


def test_empty_overlay_rejected():
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    with pytest.raises(InvalidTransparency, match="effectively empty"):
        va.validate_overlay(png(img), scene(GOOD_SAMPLES), CANVAS)


def test_hard_keyed_overlay_rejected():
    with pytest.raises(InvalidTransparency, match="no soft edges"):
        va.validate_overlay(
            png(overlay(soft_edge=False)), scene(GOOD_SAMPLES), CANVAS
        )


def test_scene_without_sample_regions_rejected():
    with pytest.raises(InvalidTransparency, match="no overlay sample regions"):
        va.validate_overlay(png(overlay()), scene([]), CANVAS)


# --- sample regions --------------------------------------------------------


def test_region_expected_opaque_but_transparent_is_reported():
    samples = [{"kind": "opaque", "x": 150, "y": 150, "width": 40, "height": 40}]
    with pytest.raises(InvalidTransparency, match="should be solid artwork"):
        va.validate_overlay(png(overlay()), scene(samples), CANVAS)


def test_region_expected_transparent_but_opaque_is_reported():
    samples = [
        {"kind": "transparent", "x": 40, "y": 40, "width": 40, "height": 40},
        {"kind": "opaque", "x": 40, "y": 40, "width": 40, "height": 40},
    ]
    with pytest.raises(InvalidTransparency, match="show the photograph through"):
        va.validate_overlay(png(overlay()), scene(samples), CANVAS)


def test_region_outside_the_canvas_is_skipped_not_fatal():
    """A model-reported box can run past the edge; clamp rather than fail."""
    samples = GOOD_SAMPLES + [
        {"kind": "opaque", "x": 199, "y": 199, "width": 50, "height": 50}
    ]
    va.validate_overlay(png(overlay()), scene(samples), CANVAS)


def test_regions_are_checked_against_the_resampled_canvas():
    """Coordinates are canvas pixels, so a differently sized asset must be
    resampled before sampling or every region lands in the wrong place."""
    big = overlay(size=400, opaque_box=(40, 40, 240, 240))
    va.validate_overlay(png(big), scene(GOOD_SAMPLES), CANVAS)


# --- background validation -------------------------------------------------


def test_valid_background_passes():
    result = va.validate_background(png(photo()), CANVAS)
    assert result == {"width": 200, "height": 200}


def test_background_with_holes_rejected():
    """Panel-shaped gaps mean the scene behind the graphics was not rebuilt."""
    img = photo().convert("RGBA")
    for x in range(20, 120):
        for y in range(20, 120):
            img.putpixel((x, y), (0, 0, 0, 0))
    with pytest.raises(AssetInvalid, match="not reconstructed"):
        va.validate_background(png(img), CANVAS)


def test_solid_colour_background_rejected():
    img = Image.new("RGB", (200, 200), (120, 140, 160))
    with pytest.raises(AssetInvalid, match="near-solid colour"):
        va.validate_background(png(img), CANVAS)


def test_background_is_resampled_to_the_canvas():
    result = va.validate_background(png(photo().resize((512, 512))), CANVAS)
    assert result == {"width": 200, "height": 200}


def test_undecodable_bytes_rejected():
    with pytest.raises(AssetInvalid, match="not a decodable image"):
        va.load_rgba(b"this is not a png")


# --- components the layout draws must not also be pixels -----------------------
#
# The defect this catches: a CTA background or a contact circle both drawn in CSS
# and left in the overlay (or the photograph). Nothing looks wrong until the
# customer moves or hides the editable one and the baked copy stays behind.


PANEL_BOX = {"x": 20, "y": 20, "width": 100, "height": 100}
ICON_BOX = {"x": 150, "y": 20, "width": 30, "height": 30}


def decomposed_scene(samples=None):
    return {
        "canvas": CANVAS,
        "overlaySamples": samples if samples is not None else GOOD_SAMPLES,
        "textLayers": [],
        "shapeLayers": [
            {"id": "s_panel", "type": "shape", "kind": "panel", "color": "#C81E3C",
             **PANEL_BOX},
        ],
        "imageLayers": [
            {"id": "l_bg", "type": "background", "x": 0, "y": 0,
             "width": 200, "height": 200},
            {"id": "l_deco", "type": "decoration", "x": 0, "y": 0,
             "width": 200, "height": 200},
            {"id": "i_phone", "type": "image", "assetId": "icon-phone", **ICON_BOX},
        ],
        "components": [
            {"id": "c_panel", "kind": "editableShape", "layerId": "s_panel",
             "pairsWith": None, "note": None},
            {"id": "c_icon", "kind": "rasterIcon", "layerId": "i_phone",
             "pairsWith": None, "note": None},
        ],
    }


def test_a_cta_background_left_in_the_overlay_is_rejected_as_a_duplicate():
    """The opaque panel in `overlay()` sits exactly where the CSS panel goes."""
    with pytest.raises(InvalidTransparency) as raised:
        va.check_no_baked_components(png(overlay()), decomposed_scene(), CANVAS)

    assert "s_panel" in raised.value.message
    assert "duplicate" in raised.value.message
    # What the bounded overlay repair is told: where it is and what to do.
    assert "x=20, y=20 (100x100 px" in raised.value.defect
    assert "leave genuine transparency" in raised.value.defect


def test_an_overlay_with_the_components_removed_passes():
    cleared = overlay()
    for x in range(PANEL_BOX["x"], PANEL_BOX["x"] + PANEL_BOX["width"]):
        for y in range(PANEL_BOX["y"], PANEL_BOX["y"] + PANEL_BOX["height"]):
            cleared.putpixel((x, y), (0, 0, 0, 0))

    measured = va.check_no_baked_components(png(cleared), decomposed_scene(), CANVAS)

    assert [c["layerId"] for c in measured] == ["s_panel", "i_phone"]
    assert [c["kind"] for c in measured] == ["editableShape", "rasterIcon"]


def test_a_pictogram_left_in_the_overlay_is_rejected():
    art = overlay(opaque_box=(ICON_BOX["x"], ICON_BOX["y"],
                              ICON_BOX["x"] + ICON_BOX["width"],
                              ICON_BOX["y"] + ICON_BOX["height"]))
    with pytest.raises(InvalidTransparency, match="i_phone"):
        va.check_no_baked_components(png(art), decomposed_scene(), CANVAS)


def test_a_soft_edge_crossing_a_component_box_is_not_called_a_duplicate():
    """Only the middle is measured, and a faint edge is below the threshold."""
    art = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    for x in range(PANEL_BOX["x"], PANEL_BOX["x"] + PANEL_BOX["width"]):
        for y in range(PANEL_BOX["y"], PANEL_BOX["y"] + 6):
            art.putpixel((x, y), (200, 30, 60, 40))

    assert va.check_no_baked_components(png(art), decomposed_scene(), CANVAS)


def test_a_scene_without_a_plan_is_not_checked_for_duplicates():
    assert va.check_no_baked_components(png(overlay()), scene(GOOD_SAMPLES), CANVAS) == []


def test_a_photograph_that_still_shows_a_removed_panel_is_rejected():
    img = photo()
    for x in range(PANEL_BOX["x"], PANEL_BOX["x"] + PANEL_BOX["width"]):
        for y in range(PANEL_BOX["y"], PANEL_BOX["y"] + PANEL_BOX["height"]):
            img.putpixel((x, y), (200, 30, 60))          # #C81E3C, the panel colour

    with pytest.raises(InvalidTransparency) as raised:
        va.check_component_area_repainted(png(img), decomposed_scene(), CANVAS)

    assert "s_panel" in raised.value.message
    assert "two copies" in raised.value.message
    assert "Reconstruct the photograph behind it" in raised.value.defect


def test_a_photograph_repainted_behind_the_panel_passes():
    checked = va.check_component_area_repainted(png(photo()), decomposed_scene(), CANVAS)
    assert [c["layerId"] for c in checked] == ["s_panel"]


# --- what may be left in the artwork and what may not -------------------------


def contact_scene():
    """A CTA background, a contact circle, a pictogram and a loose chevron."""
    doc = decomposed_scene()
    doc["textLayers"] = [
        {"id": "t_cta", "role": "cta", "contentKey": "cta.primary"},
        {"id": "t_phone", "role": "contact", "contentKey": "contact.phone"},
    ]
    doc["shapeLayers"].append(
        {"id": "s_circle", "type": "shape", "kind": "circle", "color": "#FFD43B",
         "x": 20, "y": 150, "width": 30, "height": 30})
    doc["shapeLayers"].append(
        {"id": "s_chevron", "type": "shape", "kind": "chevron", "color": None,
         "x": 150, "y": 20, "width": 30, "height": 30})
    doc["components"][0]["pairsWith"] = "t_cta"
    doc["components"][1]["pairsWith"] = "t_phone"
    doc["components"] += [
        {"id": "c_circle", "kind": "editableShape", "layerId": "s_circle",
         "pairsWith": "t_phone", "note": None},
        {"id": "c_chevron", "kind": "editableShape", "layerId": "s_chevron",
         "pairsWith": None, "note": None},
        {"id": "c_photo", "kind": "photo", "layerId": "l_bg", "pairsWith": None,
         "note": None},
        {"id": "c_deco", "kind": "retainedRaster", "layerId": "l_deco",
         "pairsWith": None, "note": "torn yellow edge"},
    ]
    return doc


def test_the_cta_background_the_circle_and_the_icon_must_stay_editable():
    required = va.mandatory_component_ids(contact_scene())

    assert required == {"c_panel", "c_circle", "c_icon"}


def test_a_decorative_shape_is_not_mandatory():
    assert "c_chevron" not in va.mandatory_component_ids(contact_scene())


def test_the_error_carries_the_components_it_measured():
    """So the caller can tell a chevron it may give up from a circle it may not."""
    with pytest.raises(InvalidTransparency) as raised:
        va.check_no_baked_components(png(overlay()), decomposed_scene(), CANVAS)

    surrender = raised.value.surrender
    assert surrender["owner"] == "overlay"
    assert [c["componentId"] for c in surrender["components"]] == ["c_panel"]


def test_giving_a_shape_back_to_the_artwork_keeps_the_plan_complete():
    from lib import scene_patch
    from lib.schemas import parse_scene

    doc = contact_scene()
    doc["canvas"] = {"width": 200, "height": 200}

    patched = scene_patch.rasterize_shapes(
        doc, ["s_chevron"], "overlay", "the overlay still contained it after repair")

    assert [s["id"] for s in patched["shapeLayers"]] == ["s_panel", "s_circle"]
    assert not [c for c in patched["components"] if c["layerId"] == "s_chevron"]
    # The decoration layer now declares what it kept.
    decoration = next(c for c in patched["components"] if c["layerId"] == "l_deco")
    assert "chevron s_chevron" in decoration["note"]
    # And the original is untouched: patches are never applied in place.
    assert len(doc["shapeLayers"]) == 3


def test_a_shape_the_scene_does_not_have_cannot_be_given_back():
    from lib import scene_patch
    from lib.errors import InvalidScene

    with pytest.raises(InvalidScene, match="s_nothing"):
        scene_patch.rasterize_shapes(contact_scene(), ["s_nothing"], "overlay", "why")


def test_an_opaque_sample_where_the_layout_now_draws_is_not_evidence():
    """Measured on a live run: the extractor sampled the call-to-action
    background as solid artwork, the overlay correctly removed it, and the
    overlay was then rejected for removing it."""
    doc = decomposed_scene([
        {"kind": "opaque", "x": PANEL_BOX["x"] + 10, "y": PANEL_BOX["y"] + 10,
         "width": 20, "height": 20},
        {"kind": "transparent", "x": 150, "y": 150, "width": 40, "height": 40},
    ])
    cleared = overlay()
    for x in range(PANEL_BOX["x"], PANEL_BOX["x"] + PANEL_BOX["width"]):
        for y in range(PANEL_BOX["y"], PANEL_BOX["y"] + PANEL_BOX["height"]):
            cleared.putpixel((x, y), (0, 0, 0, 0))

    assert va.sample_failures(cleared.convert("RGBA"), doc, CANVAS) == []


def test_a_transparent_sample_is_still_checked_inside_a_component():
    """Only the opaque expectation is dropped; a region that must show the
    photograph through is still a region that must show it through."""
    doc = decomposed_scene([
        {"kind": "transparent", "x": PANEL_BOX["x"] + 10, "y": PANEL_BOX["y"] + 10,
         "width": 20, "height": 20},
    ])

    failures = va.sample_failures(overlay().convert("RGBA"), doc, CANVAS)

    assert [f["kind"] for f in failures] == ["transparent"]


def test_clearing_a_component_box_leaves_the_artwork_around_it():
    art = overlay()
    cleared = va.erase_component_areas(
        png(art), [{"layerId": "s_panel", "box": PANEL_BOX}], CANVAS)

    out, _ = va.load_rgba(cleared)
    inside = out.crop((PANEL_BOX["x"], PANEL_BOX["y"],
                       PANEL_BOX["x"] + PANEL_BOX["width"],
                       PANEL_BOX["y"] + PANEL_BOX["height"]))
    assert inside.getchannel("A").getextrema() == (0, 0)
    # overlay()'s panel ends where the box does, and its feathered edge sits one
    # row below: untouched, so only the box was cleared.
    assert out.getpixel((60, PANEL_BOX["y"] + PANEL_BOX["height"]))[3] == 128


def test_a_dark_circle_over_dark_photography_is_not_called_a_duplicate():
    """Measured on a live run: a dark contact circle's colour matched the dark,
    repainted photography behind it, and a finished design failed. A shape still
    painted there is flat; photography is not."""
    from PIL import ImageDraw

    circle = {"id": "s_circle", "type": "shape", "kind": "circle",
              "color": "#1B1A0E", "x": 30, "y": 30, "width": 40, "height": 40}
    doc = decomposed_scene()
    doc["shapeLayers"] = [circle]
    doc["components"] = [{"id": "c_circle", "kind": "editableShape",
                          "layerId": "s_circle", "pairsWith": None, "note": None}]

    textured = Image.new("RGB", (200, 200))
    for x in range(200):                       # dark, but photographic
        for y in range(200):
            textured.putpixel((x, y), ((x * 3) % 45, (y * 2) % 44, (x + y) % 30))
    assert va.check_component_area_repainted(png(textured), doc, CANVAS)

    flat = textured.copy()
    ImageDraw.Draw(flat).rectangle((30, 30, 70, 70), fill="#1B1A0E")
    with pytest.raises(InvalidTransparency, match="s_circle"):
        va.check_component_area_repainted(png(flat), doc, CANVAS)
