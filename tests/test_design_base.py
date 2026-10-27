"""The Design Base request and its validation, on synthetic rasters shaped like the fixture.

Synthetic images prove the checks behave as specified on the shapes they are given
(opaque regions beside transparent windows, partial alpha, a baked CTA panel). They do not
show how a real image model draws a design base.
"""

import copy
import io
import json
import pathlib

import pytest
from PIL import Image, ImageDraw, ImageFilter

from lib import design_base, layer_plan, template_validate as tv, validate_assets

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "job_9281413450b7"
CANVAS = {"width": 1080, "height": 1080}


@pytest.fixture
def scene():
    return json.loads((FIXTURE / "scene.json").read_text())


@pytest.fixture
def concept():
    return (FIXTURE / "concept-256.png").read_bytes()


def png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def template(*, soften=True, cover=None, bake_cta=False):
    """Opaque yellow artwork at two corners, everything else transparent."""
    image = Image.new("RGBA", (1080, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((880, 0, 1080, 60), fill=(255, 227, 41, 255))
    draw.rectangle((0, 1020, 200, 1080), fill=(255, 227, 41, 255))
    if bake_cta:
        draw.rectangle((709, 753, 709 + 326, 753 + 71), fill=(255, 227, 41, 255))
    if cover:
        draw.rectangle(cover, fill=(20, 20, 20, 255))
    if soften:
        alpha = image.getchannel("A").filter(ImageFilter.GaussianBlur(3))
        image.putalpha(alpha)
    return png(image)


def codes(result):
    return {p["code"] for p in result["problems"]}


# --- the request: the approved concept's pixel space ---------------------------------


def test_boxes_are_quoted_in_the_pixels_of_the_attached_image_not_the_canvas(scene, concept):
    prompt, info = design_base.build_prompt(scene, concept)

    # The CTA panel is at x=709, y=753, 326x71 on the 1080 canvas; the attached image is 256.
    assert "the panel #FFE329 at x=168, y=178, 77x17 px" in prompt
    assert "x=709" not in prompt and "y=753" not in prompt
    assert "256x256" in prompt
    assert info["imageSize"] == {"width": 256, "height": 256}
    assert info["canvas"] == {"width": 1080, "height": 1080}


def test_independent_backgrounds_and_icons_are_assigned_away_without_the_brief(scene, concept):
    prompt, info = design_base.build_prompt(scene, concept)

    assert "the circle #FFE329" in prompt and "the pictogram" in prompt
    assert "Remove dedicated CTA panels, contact circles and text cards" in prompt
    # The brief and the copy are not reintroduced.
    for layer in scene["textLayers"]:
        assert layer["text"] not in prompt
    assert info["ownershipLines"] > 10


def test_a_css_gradient_is_resolved_before_the_request_into_a_leave_it_out_line(scene, concept):
    scene = copy.deepcopy(scene)
    for effect in scene["effects"]:
        if effect["kind"] == "readabilityGradient":
            effect["owner"] = "layout"
    prompt, _ = design_base.build_prompt(scene, concept)

    assert "drawn in code as an editable gradient; leave it out of this image" in prompt
    assert "not a panel" not in prompt


def test_the_prompt_for_a_full_representation_does_not_ask_for_transparency(scene, concept):
    scene = copy.deepcopy(scene)
    plan = layer_plan.build(scene, concept)
    plan["template"]["representation"] = "full"
    scene["layerPlan"] = plan
    prompt, info = design_base.build_prompt(scene, concept)

    assert info["representation"] == "full"
    assert "No photograph shows through this design" in prompt
    assert "Keep photo windows transparent" not in prompt


def test_alpha_uncertainty_is_recorded_for_a_raster_and_absent_for_css(scene, concept):
    plan = layer_plan.build(scene, concept)
    assert plan["template"]["alphaUncertainty"]
    scene = copy.deepcopy(scene)
    scene["designBase"] = {"kind": "gradient", "note": "a plain gradient"}
    css = layer_plan.build(scene, concept)
    if css["template"]["representation"] == "css-only":
        assert css["template"]["alphaUncertainty"] is None


# --- validation: opaque artwork beside transparent windows ----------------------------


def test_opaque_regions_beside_transparent_windows_are_a_usable_template(scene):
    result = tv.check_template(template(), scene, CANVAS)

    assert result["rgbaOk"] and result["alpha"]["opaque"] > 0.01
    assert not codes(result) & {tv.WINDOWS_COVERED, tv.OBSTRUCTS_PHOTO, tv.UNDECODABLE,
                                tv.BAKED_COMPONENT}


def test_partial_alpha_is_measured_and_is_not_mistaken_for_hard_edges(scene):
    soft = tv.check_template(template(soften=True), scene, CANVAS)
    hard = tv.check_template(template(soften=False), scene, CANVAS)

    assert soft["alpha"]["partial"] > 0 and tv.HARD_EDGES not in codes(soft)
    assert hard["alpha"]["partial"] == 0 and tv.HARD_EDGES in codes(hard)


def test_a_template_covering_the_photograph_windows_is_an_obstruction_not_a_transparency_fault(scene):
    result = tv.check_template(template(cover=(0, 0, 1080, 1080)), scene, CANVAS)

    assert tv.OBSTRUCTS_PHOTO in codes(result)
    assert {p["category"] for p in result["problems"] if p["code"] == tv.OBSTRUCTS_PHOTO} == \
        {tv.CATEGORY_OBSTRUCTION}
    assert "INVALID_TRANSPARENCY" not in json.dumps(result)


def test_flat_artwork_is_refused_never_keyed_out_into_transparency(scene):
    flat = png(Image.new("RGB", (1080, 1080), (255, 255, 255)))
    result = tv.check_template(flat, scene, CANVAS)

    assert codes(result) == {tv.NO_ALPHA} and result["rgbaOk"] is False


def test_a_cta_panel_baked_into_the_template_is_an_ownership_finding(scene):
    clean = tv.check_template(template(), scene, CANVAS)
    baked = tv.check_template(template(bake_cta=True), scene, CANVAS)

    assert not any(p["category"] == tv.CATEGORY_OWNERSHIP for p in clean["problems"])
    ownership = [p for p in baked["problems"] if p["category"] == tv.CATEGORY_OWNERSHIP]
    assert ownership and ownership[0]["code"] in (tv.BAKED_COMPONENT, tv.BROAD_COVER)
