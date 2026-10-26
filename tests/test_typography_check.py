"""Outlined Hebrew typography: measured on the render, never silently swapped."""

import io

import pytest
from PIL import Image, ImageDraw

from lib import artifacts, assemble_html, scene_patch, typography_check
from lib.errors import InvalidScene, Stage
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    pipeline, run_stage, skip_external_probes,
)
from tests.test_repair_pipeline import (  # noqa: F401
    FIRST_PASS, job_of, review_pass, script,
)

CANVAS = {"width": 1000, "height": 1000}
BOX = {"x": 100, "y": 100, "width": 600, "height": 200}


def headline(**overrides):
    layer = {"id": "t_title", "x": 100, "y": 100, "width": 600, "height": 200,
             "align": "right", "direction": "rtl", "color": "#FFFFFF", "contentKey": "title",
             "text": "title", "fontFamily": "Heebo", "fontSize": 160, "fontWeight": 800,
             "zIndex": 5, "role": "headline", "fill": "transparent", "strokeColor": "#FFFFFF",
             "strokeWidth": 6.0, "letterSpacing": -2.0, "lineHeight": 1.0, "overflow": "shrink"}
    layer.update(overrides)
    return layer


def scene_of(*layers):
    return {"canvas": dict(CANVAS), "textLayers": list(layers), "shapeLayers": [],
            "imageLayers": [], "effects": [], "alignmentGroups": [], "fillGroups": []}


def picture(draw_letters):
    image = Image.new("RGB", (1000, 1000), (15, 15, 15))
    draw_letters(ImageDraw.Draw(image))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def hollow(draw, left=130, right=670, stroke=4):
    """Letters as outlined boxes: transparent inside."""
    for x in range(left, right, 90):
        draw.rectangle((x, 130, x + 70, 270), outline=(255, 255, 255), width=stroke)


def solid(draw, left=130, right=670):
    for x in range(left, right, 90):
        draw.rectangle((x, 130, x + 70, 270), fill=(255, 255, 255))


def codes(problems):
    return sorted((p["code"], p["severity"]) for p in problems)


def check(scene, concept, native, **kwargs):
    return typography_check.check(scene, concept, native, CANVAS, **kwargs)


# --- outlines that closed up ---------------------------------------------------------------


def test_an_outline_that_rendered_solid_is_blocking():
    problems = check(scene_of(headline()), picture(hollow), picture(solid))

    assert ("OUTLINE_FILLED", "blocking") in codes(problems)
    filled = next(p for p in problems if p["code"] == "OUTLINE_FILLED")
    assert filled["measured"]["rendered"] > 0.7 > 0.5 > filled["measured"]["concept"]


def test_an_outline_that_stayed_hollow_passes():
    assert check(scene_of(headline()), picture(hollow), picture(hollow)) == []


def test_solid_lettering_is_not_judged_as_an_outline():
    assert check(scene_of(headline(fill="solid", strokeWidth=0.0)),
                 picture(solid), picture(solid)) == []


def test_a_footprint_that_shrank_against_the_concept_is_reported():
    narrow = lambda d: hollow(d, left=130, right=400)
    problems = check(scene_of(headline()), picture(hollow), picture(narrow))

    assert ("TYPOGRAPHY_FOOTPRINT", "warning") in codes(problems)
    ratio = next(p for p in problems if p["code"] == "TYPOGRAPHY_FOOTPRINT")["measured"]
    assert ratio["widthRatio"] < 0.8 and 0.9 < ratio["heightRatio"] < 1.1


def test_an_outline_too_thin_to_survive_the_feed_is_reported():
    problems = check(scene_of(headline(strokeWidth=2.0)), None, None)
    assert ("OUTLINE_THIN_AT_FEED", "warning") in codes(problems)
    assert check(scene_of(headline(strokeWidth=4.0)), None, None) == []   # 1.44px at 360


# --- fonts that fell back --------------------------------------------------------------------


def test_a_fallback_face_is_blocking_and_named():
    scene = scene_of(headline())
    element = assemble_html.element_ids(scene)["t_title"]
    fonts = {element: [{"familyName": "Heebo", "glyphCount": 8},
                       {"familyName": "DejaVu Sans", "glyphCount": 3}]}

    problems = check(scene, None, None, platform_fonts=fonts)

    fallback = next(p for p in problems if p["code"] == "FONT_FALLBACK")
    assert fallback["severity"] == "blocking" and "DejaVu Sans" in fallback["message"]
    assert fallback["layerId"] == element


def test_the_requested_face_alone_is_fine_and_an_unmeasured_font_is_not_a_finding():
    scene = scene_of(headline())
    element = assemble_html.element_ids(scene)["t_title"]
    clean = {element: [{"familyName": "Heebo", "glyphCount": 11}]}

    assert [p for p in check(scene, None, None, platform_fonts=clean)
            if p["code"] == "FONT_FALLBACK"] == []
    assert [p for p in check(scene, None, None, platform_fonts=None)
            if p["code"] == "FONT_FALLBACK"] == []


# --- repairs may not hide the problem -----------------------------------------------------------


def test_a_repair_may_not_turn_an_outlined_headline_into_solid_text():
    scene = scene_of(headline())
    with pytest.raises(InvalidScene, match="outlined lettering"):
        scene_patch.apply_properties(scene, "t_title", {"fill": "solid"}, target="typography")
    # Everything that adjusts the outline is still allowed.
    patched = scene_patch.apply_properties(
        scene, "t_title", {"strokeWidth": "4", "letterSpacing": "0"}, target="typography")
    assert patched["textLayers"][0]["fill"] == "transparent"
    assert scene["textLayers"][0]["fill"] == "transparent"


def test_a_size_repair_keeps_its_size_instead_of_being_shrunk_back():
    scene = scene_of(headline(fontSize=120))

    patched = scene_patch.apply_properties(
        scene, "t_title", {"fontSize": "170"}, target="typography")
    stroke_only = scene_patch.apply_properties(
        scene, "t_title", {"strokeWidth": "5"}, target="typography")
    no_op = scene_patch.apply_properties(
        scene, "t_title", {"fontSize": "120"}, target="typography")

    assert patched["textLayers"][0]["overflow"] == "visible"      # fitting skips it
    assert stroke_only["textLayers"][0]["overflow"] == "shrink"
    assert no_op["textLayers"][0]["overflow"] == "shrink"          # nothing changed


def test_assembly_does_not_shrink_a_layer_that_asked_to_keep_its_size(script, monkeypatch):
    from lib import render as renderer

    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)
    job = job_of(script)
    scene = artifacts.get_json(script["jobId"], job["artifacts"][Stage.EXTRACT_SCENE]["scene"])
    kept, shrinkable = scene["textLayers"][0], scene["textLayers"][1]
    kept["overflow"], shrinkable["overflow"] = "visible", "shrink"
    stored = artifacts.put_json(
        artifacts.artifact_key(script["jobId"], Stage.EXTRACT_SCENE, 1, "scene-visible.json"), scene)
    job["artifacts"][Stage.EXTRACT_SCENE]["scene"] = stored["key"]
    element_of = assemble_html.element_ids(scene)
    monkeypatch.setattr(renderer, "fit_text_layers", lambda html, canvas: {
        element_of[kept["id"]]: {"original": kept["fontSize"], "fitted": 10, "fits": True},
        element_of[shrinkable["id"]]: {"original": shrinkable["fontSize"], "fitted": 11,
                                       "fits": True},
    })

    run_stage(script["jobId"], Stage.ASSEMBLE, attempt=1)

    record = job_of(script)["artifacts"][Stage.ASSEMBLE]
    effective = record["typography"]
    assert effective[kept["id"]]["fontSize"] == kept["fontSize"]
    assert effective[kept["id"]]["requestedFontSize"] == kept["fontSize"]
    # The shrunk layer says so: what was asked for beside what was set.
    assert effective[shrinkable["id"]]["requestedFontSize"] == shrinkable["fontSize"]
    assert effective[shrinkable["id"]]["fontSize"] == 11
    assert set(record["fitted"]) == {shrinkable["id"]}


def test_the_render_reports_what_the_browser_used(script, monkeypatch):
    from lib import render as renderer

    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)
    previous = renderer.render

    def render(html, canvas, scene):
        result = previous(html, canvas, scene)
        first = assemble_html.element_ids(scene)[scene["textLayers"][0]["id"]]
        result["checks"]["platformFonts"] = {
            first: [{"familyName": "Not Heebo", "glyphCount": 4}]}
        return result

    monkeypatch.setattr(renderer, "render", render)
    review_pass(script, 1)

    problems = job_of(script)["artifacts"][Stage.RENDER]["problems"]
    assert [p["code"] for p in problems] == ["FONT_FALLBACK"]
    review = artifacts.get_json(
        script["jobId"], job_of(script)["artifacts"][Stage.REVIEW]["review"])
    assert "FONT_FALLBACK" in {i["code"] for i in review["blockingIssues"]}
