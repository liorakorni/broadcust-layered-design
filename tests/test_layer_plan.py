"""The versioned layer plan inside the scene (addendum sections 2, 6 and 13)."""

import copy
import json
import pathlib

import pytest

import conf
from lib import layer_plan, schemas

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "job_9281413450b7"


@pytest.fixture
def scene():
    return json.loads((FIXTURE / "scene.json").read_text())


@pytest.fixture
def concept():
    return (FIXTURE / "concept-256.png").read_bytes()


def _parse(document):
    return schemas.parse_scene(
        document, 1080, 1080, allowed_fonts=set(conf.APPROVED_FONT_FAMILIES) | {
            t["fontFamily"] for t in document["textLayers"]})


def _by_id(plan):
    return {e["id"]: e for e in plan["elements"]}


def test_every_scene_element_has_one_owner_one_category_and_a_renderer(scene, concept):
    plan = layer_plan.build(scene, concept)
    elements = _by_id(plan)

    assert elements["photo-background"]["category"] == "backgroundPhoto"
    assert elements["photo-background"]["owner"] == "photo"
    assert elements["graphic-overlay"]["category"] == "structuralTemplate"
    assert elements["graphic-overlay"]["owner"] == "template"
    assert elements["text-title-primary"]["category"] == "editableText"
    assert elements["text-title-primary"]["renderer"] == "text"
    assert elements["phone-icon"]["category"] == "genericIcon"
    assert elements["phone-icon"]["renderer"] == "raster"
    for contact in ("phone-circle", "email-circle", "cta-panel", "business-rule"):
        assert elements[contact]["category"] == "editableTextBackground"
        assert elements[contact]["renderer"] == "css"
    assert plan["version"] == layer_plan.VERSION and plan["revision"] == 1
    assert set(e["category"] for e in plan["elements"]) <= set(layer_plan.CATEGORIES)


def test_dependencies_follow_the_pairing_and_geometry_is_in_canvas_pixels(scene, concept):
    elements = _by_id(layer_plan.build(scene, concept))

    assert elements["phone-icon"]["dependsOn"] == ["text-phone"]
    assert elements["phone-circle"]["dependsOn"] == ["text-phone"]
    assert elements["cta-panel"]["geometry"] == {
        k: next(s for s in scene["shapeLayers"] if s["id"] == "cta-panel")[k]
        for k in ("x", "y", "width", "height")}
    assert elements["phone-icon"]["zIndex"] == 15


def test_details_that_move_together_are_one_group_and_are_not_images(scene, concept):
    elements = _by_id(layer_plan.build(scene, concept))
    dots = [elements[f"dot-{n}"] for n in range(1, 5)]
    chevrons = [elements[f"chevron-{n}"] for n in range(1, 6)]

    assert {d["groupId"] for d in dots} == {"group-dots"}
    assert {c["groupId"] for c in chevrons} == {"group-chevrons"}
    assert all(d["renderer"] == "css" for d in dots + chevrons)       # never one image per dot
    assert sum(1 for e in elements.values()
               if e["renderer"] == "raster" and e["status"] == "present") == 4  # photo, template, 2 icons


def test_the_template_excludes_text_icons_text_backgrounds_and_movable_details(scene, concept):
    template = layer_plan.build(scene, concept)["template"]

    for excluded in ("text-title-primary", "phone-icon", "cta-panel", "phone-circle",
                     "dot-1", "chevron-3"):
        assert excluded in template["excludes"]
    assert "graphic-overlay" not in template["excludes"]
    assert "photo-background" not in template["excludes"]
    assert template["representation"] == "overlay"
    assert template["alphaUncertainty"]                              # recorded, not hidden
    assert "torn-wave" in template["retains"][0]


def test_a_baked_readability_effect_has_one_owner_and_is_listed_once(scene, concept):
    elements = [e for e in layer_plan.build(scene, concept)["elements"]
                if e["id"] == "effect-photo-darkening"]

    assert len(elements) == 1 and elements[0]["owner"] == "photo"
    assert elements[0]["status"] == "baked"


def test_the_plan_records_both_coordinate_spaces(scene, concept):
    space = layer_plan.build(scene, concept)["coordinateSpace"]

    assert space["canvas"] == {"width": 1080, "height": 1080}
    assert space["concept"] == {"width": 256, "height": 256}


# --- representation, decided before extraction ------------------------------


def test_a_declared_gradient_only_base_with_no_opaque_artwork_is_css_only(scene, concept):
    scene["designBase"] = {"representation": "css-only", "note": "a fade"}
    scene["overlaySamples"] = [s for s in scene["overlaySamples"] if s["kind"] == "transparent"]

    template = layer_plan.build(scene, concept)["template"]
    assert template["representation"] == "css-only" and template["gradientOnly"] is True
    assert template["alphaUncertainty"] is None


def test_opaque_artwork_contradicts_a_gradient_only_declaration(scene, concept):
    scene["designBase"] = {"representation": "css-only", "note": "a fade"}   # opaque samples remain

    template = layer_plan.build(scene, concept)["template"]
    assert template["representation"] == "overlay"
    assert "opaque artwork" in template["reason"]


def test_a_scene_without_the_declaration_means_overlay(scene, concept):
    scene.pop("designBase", None)
    assert layer_plan.build(scene, concept)["template"]["representation"] == "overlay"


# --- the fallback is persisted before any asset exists -----------------------


def test_the_fallback_parameters_are_measured_from_the_concept_up_front(scene, concept):
    spec = layer_plan.build(scene, concept)["fallback"]

    assert spec["basis"] == "concept"
    assert spec["stacking"]["fillsZIndex"] == 1
    for fill in spec["fills"]:
        assert fill["color"].startswith("#") and fill["opacity"] == 1.0
        # A fill never covers a window the photograph must show through.
        for window in (s for s in scene["overlaySamples"] if s["kind"] == "transparent"):
            assert layer_plan._overlap(fill, window) is False
    assert spec["omitted"] and "torn-wave" in spec["omitted"][0]


def test_without_the_concept_bytes_no_fill_is_guessed(scene):
    assert layer_plan.build(scene, None)["fallback"]["fills"] == []


def test_applying_the_fallback_omits_only_the_template(scene, concept):
    scene["layerPlan"] = layer_plan.build(scene, concept)
    before_text = copy.deepcopy(scene["textLayers"])
    result, summary = layer_plan.apply_fallback(scene, "template could not be decoded",
                                                "TEMPLATE_UNDECODABLE")
    parsed = _parse(result)                                     # still a valid scene

    assert [l.type for l in parsed.imageLayers].count("decoration") == 0
    assert [l.type for l in parsed.imageLayers].count("background") == 1   # photo untouched
    assert result["textLayers"] == before_text                  # text untouched
    assert {l.assetId for l in parsed.imageLayers if l.assetId} == {"icon-phone", "icon-email"}
    assert all(c["layerId"] != "graphic-overlay" for c in result["components"])
    assert summary["kind"] == "template-css-fallback"
    assert summary["code"] == "TEMPLATE_UNDECODABLE"
    assert summary["omitted"]                                    # what is NOT replaced is listed
    plan = result["layerPlan"]
    assert plan["revision"] == 2
    assert plan["corrections"][-1]["action"] == "template-omitted-css-fallback"
    assert plan["corrections"][-1]["invalidates"] == []          # nothing else is regenerated
    assert "graphic-overlay" not in _by_id(plan)
    assert plan["template"]["fallbackActive"] is True


def test_the_fallback_never_substitutes_the_flattened_concept(scene, concept):
    scene["layerPlan"] = layer_plan.build(scene, concept)
    result, _ = layer_plan.apply_fallback(scene, "x", "TEMPLATE_UNDECODABLE")

    assert not any(l["type"] == "image" and l.get("assetId") in (None, "concept")
                   for l in result["imageLayers"])
    assert json.dumps(result).count("data:image") == 0


def test_a_deliberate_gradient_only_base_is_not_reported_as_a_fallback(scene, concept):
    scene["designBase"] = {"representation": "css-only", "note": "fade"}
    scene["overlaySamples"] = [s for s in scene["overlaySamples"] if s["kind"] == "transparent"]
    scene["layerPlan"] = layer_plan.build(scene, concept)

    result, summary = layer_plan.apply_fallback(scene, "gradient only", "TEMPLATE_CSS_ONLY",
                                                deliberate=True)
    assert summary["kind"] == "template-css-only"
    assert result["layerPlan"]["template"]["gradientOnly"] is True
    assert result["layerPlan"]["template"]["fallbackActive"] is False


def test_an_overlay_owned_gradient_is_redrawn_once_in_css_when_the_template_goes(scene, concept):
    for effect in scene["effects"]:
        if effect["kind"] == "readabilityGradient":
            effect["owner"] = "overlay"
    scene["layerPlan"] = layer_plan.build(scene, concept)
    assert scene["layerPlan"]["fallback"]["gradient"]["angle"] == 90     # text sits on the right

    result, summary = layer_plan.apply_fallback(scene, "x", "TEMPLATE_UNDECODABLE")
    _parse(result)
    scrims = [s for s in result["shapeLayers"] if s["kind"] == "gradient"]
    assert len(scrims) == 1 and "fallback-scrim" in summary["drawn"]
    assert [e["owner"] for e in result["effects"] if e["kind"] == "readabilityGradient"] == ["layout"]


def test_a_photo_owned_gradient_is_not_doubled_by_the_fallback(scene, concept):
    scene["layerPlan"] = layer_plan.build(scene, concept)

    result, summary = layer_plan.apply_fallback(scene, "x", "TEMPLATE_UNDECODABLE")
    assert not [s for s in result["shapeLayers"] if s["kind"] == "gradient"]
    assert "fallback-scrim" not in summary["drawn"]


# --- keeping the plan true ----------------------------------------------------


def test_moving_a_layer_bumps_the_revision_and_names_the_dependents(scene, concept):
    scene["layerPlan"] = layer_plan.build(scene, concept)
    for text in scene["textLayers"]:
        if text["id"] == "text-phone":
            text["x"] -= 10

    moved, changed = layer_plan.sync(scene)

    assert changed == ["text-phone"]
    plan = moved["layerPlan"]
    assert plan["revision"] == 2
    assert set(plan["corrections"][-1]["invalidates"]) >= {"phone-icon", "phone-circle"}
    assert "graphic-overlay" not in plan["corrections"][-1]["invalidates"]   # template stays valid


def test_an_unchanged_scene_keeps_its_plan_byte_identical(scene, concept):
    scene["layerPlan"] = layer_plan.build(scene, concept)
    before = json.dumps(scene["layerPlan"], sort_keys=True)
    _, changed = layer_plan.sync(scene)

    assert changed == [] and json.dumps(scene["layerPlan"], sort_keys=True) == before


def test_a_plan_with_a_duplicate_owner_or_unknown_dependency_is_refused(scene, concept):
    plan = layer_plan.build(scene, concept)
    duplicated = copy.deepcopy(plan)
    duplicated["elements"].append(copy.deepcopy(duplicated["elements"][0]))
    with pytest.raises(layer_plan.PlanError, match="more than once"):
        layer_plan.check(duplicated)

    dangling = copy.deepcopy(plan)
    dangling["elements"][0]["dependsOn"] = ["nothing"]
    with pytest.raises(layer_plan.PlanError, match="unknown"):
        layer_plan.check(dangling)


def test_the_plan_travels_inside_the_scene_schema(scene, concept):
    scene["layerPlan"] = layer_plan.build(scene, concept)
    scene["designBase"] = {"representation": "overlay", "note": "torn edges"}

    parsed = _parse(scene)
    assert parsed.layerPlan["revision"] == 1
    assert parsed.designBase.representation == "overlay"
    # And a scene written before the plan existed still parses.
    legacy = {k: v for k, v in scene.items() if k not in ("layerPlan", "designBase")}
    assert _parse(legacy).layerPlan is None
