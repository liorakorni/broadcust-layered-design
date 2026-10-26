"""The decomposition plan: what the concept is made of and how it is delivered.

The plan is the contract for editability. If a CTA background or a contact circle
is not in it, it is still part of a picture, and the customer cannot move or
restyle it. These are pure tests: the plan is validated and the colours are
sampled without a pipeline.
"""

import copy
import io

import pytest
from PIL import Image

from lib import concept_sample, schemas
from lib.errors import InvalidScene
from lib.stages.extract_scene import SCENE_SCHEMA
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    CANVAS,
    SCENE,
    pipeline,
    skip_external_probes,
)

FONTS = {"Heebo"}

CIRCLE = {
    "id": "s_phone_circle", "type": "shape", "kind": "circle", "effect": "e_badge",
    "x": 80, "y": 900, "width": 64, "height": 64, "zIndex": 8,
    "color": "#FFD43B", "opacity": 1.0, "radius": 0, "angle": 180, "stops": [],
    "borderColor": None, "borderWidth": 0.0, "rotation": 0,
}
PANEL = {
    "id": "s_cta_panel", "type": "shape", "kind": "panel", "effect": "e_cta",
    "x": 100, "y": 700, "width": 400, "height": 120, "zIndex": 8,
    "color": "#1E6F5C", "opacity": 1.0, "radius": 60, "angle": 180, "stops": [],
    "borderColor": None, "borderWidth": 0.0, "rotation": 0,
}
ICON = {
    "id": "i_phone", "type": "image", "assetId": "icon-phone",
    "x": 96, "y": 916, "width": 32, "height": 32, "zIndex": 9,
    "objectFit": "contain",
}
EFFECTS = [
    {"id": "e_badge", "kind": "badge", "owner": "layout", "note": None},
    {"id": "e_cta", "kind": "ctaBackground", "owner": "layout", "note": None},
]


def parse(doc):
    return schemas.parse_scene(doc, 1024, 1024, allowed_fonts=FONTS)


def decomposed():
    """The fixture scene with a CTA and its phone row fully decomposed."""
    doc = copy.deepcopy(SCENE)
    phone = next(l for l in doc["textLayers"]
                 if l["contentKey"].startswith(schemas.CONTACT_KEY_PREFIX))
    cta = {**copy.deepcopy(doc["textLayers"][1]), "id": "t_cta", "role": "cta",
           "contentKey": "cta.primary", "text": "להרשמה",
           "x": 120, "y": 730, "width": 360, "height": 60, "zIndex": 10}
    doc["textLayers"].append(cta)

    doc["shapeLayers"] = [copy.deepcopy(CIRCLE), copy.deepcopy(PANEL)]
    doc["effects"] = copy.deepcopy(EFFECTS)
    doc["imageLayers"].append(copy.deepcopy(ICON))
    doc["components"] = [
        {"id": "c_photo", "kind": "photo", "layerId": "l_bg", "pairsWith": None,
         "note": None},
        {"id": "c_overlay", "kind": "retainedRaster", "layerId": "l_deco",
         "pairsWith": None, "note": "brush texture and corner flourish"},
        {"id": "c_panel", "kind": "editableShape", "layerId": "s_cta_panel",
         "pairsWith": cta["id"], "note": None},
        {"id": "c_circle", "kind": "editableShape", "layerId": "s_phone_circle",
         "pairsWith": phone["id"], "note": None},
        {"id": "c_icon", "kind": "rasterIcon", "layerId": "i_phone",
         "pairsWith": phone["id"], "note": None},
    ]
    return doc


# --- the plan is optional for old scenes, complete for new ones ----------------


def test_a_scene_written_before_the_plan_existed_is_still_valid():
    plain = copy.deepcopy(SCENE)
    assert "components" not in plain
    assert parse(plain).components == []


def test_a_fully_decomposed_scene_is_accepted():
    parsed = parse(decomposed())
    assert {c.kind for c in parsed.components} == {
        "photo", "retainedRaster", "editableShape", "rasterIcon"}


def test_a_shape_left_out_of_the_plan_is_refused():
    doc = decomposed()
    doc["components"] = [c for c in doc["components"] if c["layerId"] != "s_cta_panel"]
    with pytest.raises(InvalidScene, match="no entry in the decomposition plan"):
        parse(doc)


def test_an_icon_left_out_of_the_plan_is_refused():
    doc = decomposed()
    doc["components"] = [c for c in doc["components"] if c["layerId"] != "i_phone"]
    with pytest.raises(InvalidScene, match="no entry in the decomposition plan"):
        parse(doc)


def test_a_component_must_match_the_layer_it_names():
    doc = decomposed()
    next(c for c in doc["components"] if c["layerId"] == "i_phone")["kind"] = "editableShape"
    with pytest.raises(InvalidScene, match="is a image layer"):
        parse(doc)


def test_a_component_naming_a_layer_that_is_not_there_is_refused():
    doc = decomposed()
    doc["components"].append({"id": "c_ghost", "kind": "editableShape",
                              "layerId": "s_missing", "pairsWith": None, "note": None})
    with pytest.raises(InvalidScene, match="does not contain"):
        parse(doc)


# --- the pairings that prove a component really was separated ------------------


def test_a_cta_without_a_background_shape_is_refused():
    doc = decomposed()
    doc["shapeLayers"] = [s for s in doc["shapeLayers"] if s["id"] != "s_cta_panel"]
    doc["effects"] = [e for e in doc["effects"] if e["id"] != "e_cta"]
    doc["components"] = [c for c in doc["components"] if c["layerId"] != "s_cta_panel"]
    with pytest.raises(InvalidScene, match="neither an editable background"):
        parse(doc)


def test_a_cta_whose_button_cannot_be_separated_must_say_so():
    doc = decomposed()
    cta = next(l for l in doc["textLayers"] if l["role"] == "cta")
    doc["shapeLayers"] = [s for s in doc["shapeLayers"] if s["id"] != "s_cta_panel"]
    doc["effects"] = [e for e in doc["effects"] if e["id"] != "e_cta"]
    doc["components"] = [c for c in doc["components"] if c["layerId"] != "s_cta_panel"]
    doc["components"].append(
        {"id": "c_cta_art", "kind": "retainedRaster", "layerId": "l_deco",
         "pairsWith": cta["id"], "note": "hand-painted brush button, not reproducible"})
    parse(doc)                                  # declared, so accepted


def test_a_contact_line_without_a_circle_and_an_icon_is_refused():
    doc = decomposed()
    doc["imageLayers"] = [l for l in doc["imageLayers"] if l["id"] != "i_phone"]
    doc["components"] = [c for c in doc["components"] if c["layerId"] != "i_phone"]
    with pytest.raises(InvalidScene, match="missing rasterIcon"):
        parse(doc)


def test_a_cta_background_may_be_recognised_by_geometry_alone():
    """A shape drawn under the words counts even without an explicit pairing."""
    doc = decomposed()
    next(c for c in doc["components"]
         if c["layerId"] == "s_cta_panel")["pairsWith"] = None
    parse(doc)


# --- colours are measured off the concept, not taken on trust -----------------


def concept(colors):
    """A synthetic concept: a white canvas with one filled box per entry."""
    image = Image.new("RGB", (1024, 1024), "#FFFFFF")
    for box, color in colors:
        for x in range(box["x"], box["x"] + box["width"]):
            for y in range(box["y"], box["y"] + box["height"]):
                image.putpixel((x, y), color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_median_color_reads_the_colour_in_the_middle_of_a_box():
    box = {"x": 100, "y": 100, "width": 200, "height": 200}
    image = concept([(box, (255, 212, 59))])
    assert concept_sample.median_color(image, box, CANVAS) == "#FFD43B"


def test_median_color_ignores_what_sits_on_top_of_the_shape():
    """A dark pictogram inside a circle must not drag the sampled colour down."""
    box = {"x": 100, "y": 100, "width": 200, "height": 200}
    icon = {"x": 170, "y": 170, "width": 60, "height": 60}
    image = concept([(box, (255, 212, 59)), (icon, (0, 0, 0))])
    assert concept_sample.median_color(image, box, CANVAS) == "#FFD43B"


def test_a_reported_colour_that_disagrees_with_the_concept_is_corrected():
    doc = decomposed()
    panel = next(s for s in doc["shapeLayers"] if s["id"] == "s_cta_panel")
    panel["color"] = "#1E6F5C"
    image = concept([({k: panel[k] for k in ("x", "y", "width", "height")},
                      (255, 212, 59))])

    corrections = concept_sample.correct_shape_colors(image, doc)

    assert panel["color"] == "#FFD43B"
    assert {"layerId": "s_cta_panel", "property": "color",
            "reported": "#1E6F5C", "sampled": "#FFD43B"} in corrections
    parse(doc)                                  # still a valid scene


def test_a_colour_within_tolerance_is_left_as_reported():
    doc = decomposed()
    panel = next(s for s in doc["shapeLayers"] if s["id"] == "s_cta_panel")
    circle = next(s for s in doc["shapeLayers"] if s["id"] == "s_phone_circle")
    panel["color"] = "#FFD43B"
    box = lambda s: {k: s[k] for k in ("x", "y", "width", "height")}
    # A few units off on each: sampling noise, not a different colour.
    image = concept([(box(panel), (250, 210, 62)), (box(circle), (252, 214, 55))])

    assert concept_sample.correct_shape_colors(image, doc) == []
    assert panel["color"] == "#FFD43B"


def test_geometry_is_never_adjusted_by_sampling():
    """A wrong box is REVIEW's problem and a cheap layout repair, not guesswork."""
    doc = decomposed()
    before = [{k: s[k] for k in ("x", "y", "width", "height")} for s in doc["shapeLayers"]]
    image = concept([({"x": 0, "y": 0, "width": 1024, "height": 1024}, (12, 34, 56))])

    concept_sample.correct_shape_colors(image, doc)

    assert [{k: s[k] for k in ("x", "y", "width", "height")}
            for s in doc["shapeLayers"]] == before


# --- the extractor asks for all of it -----------------------------------------


def test_the_extractor_schema_requests_shapes_effects_and_components():
    """Strict mode: every property has to be listed in `required`."""
    for name in ("shapeLayers", "effects", "components"):
        assert name in SCENE_SCHEMA["required"]
        item = SCENE_SCHEMA["properties"][name]["items"]
        assert set(item["required"]) == set(item["properties"])
        assert item["additionalProperties"] is False

    kinds = SCENE_SCHEMA["properties"]["components"]["items"]["properties"]["kind"]
    assert kinds["enum"] == list(schemas.COMPONENT_KINDS)
    shapes = SCENE_SCHEMA["properties"]["shapeLayers"]["items"]["properties"]["kind"]
    assert shapes["enum"] == list(schemas.SHAPE_KINDS)


def test_the_extractor_prompt_asks_for_the_separated_components():
    from lib import prompts

    prompt = prompts.load("extract-scene")
    for required in ("shapeLayers", "components", "rasterIcon", "retainedRaster",
                     "icon-phone", "pairsWith", "#RRGGBB"):
        assert required in prompt


def test_the_extractor_prompt_states_what_each_shape_kind_must_carry():
    """Measured on a live run: a fifth gradient stop and a chevron without a
    border were the two ways a described scene failed validation."""
    from lib import prompts

    prompt = prompts.load("extract-scene")
    assert 'two to four "stops" (never five)' in prompt
    assert '"ring", "border" and "chevron" need "borderColor"' in prompt
    # And a way out that costs editability rather than the job.
    assert "leave it out of" in prompt or "part of the decoration layer" in prompt


# --- one bounded re-ask -------------------------------------------------------


def scene_with_a_bad_shape():
    doc = copy.deepcopy(SCENE)
    doc["effects"] = []
    doc["shapeLayers"] = []
    bad = {**copy.deepcopy(CIRCLE), "id": "s_ring", "kind": "ring",
           "effect": "e_ring", "color": None}
    doc["shapeLayers"].append(bad)             # a ring with no borderColor
    doc["effects"].append(
        {"id": "e_ring", "kind": "other", "owner": "layout", "note": None})
    doc["components"] = []
    return doc


def extract(pipeline, monkeypatch, scenes):
    """Run EXTRACT_SCENE with scripted descriptions, served in order."""
    from tests.test_pipeline import run_stage
    from lib.errors import Stage
    from lib.providers import vision as vision_provider

    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.CONCEPT, Stage.REVIEW_CONCEPT):
        run_stage(pipeline["jobId"], stage)

    served = []
    inner = vision_provider.analyze

    def analyze(prompt, images=(), **kw):
        if kw.get("schema_name") == "scene_v1":
            served.append(prompt)
            return copy.deepcopy(scenes[min(len(served), len(scenes)) - 1])
        return inner(prompt, images, **kw)

    monkeypatch.setattr(vision_provider, "analyze", analyze)
    try:
        run_stage(pipeline["jobId"], Stage.EXTRACT_SCENE)
        failure = None
    except InvalidScene as exc:
        failure = exc
    return served, failure


def test_a_rejected_description_is_re_asked_once_with_the_complaint(
        pipeline, monkeypatch):
    served, failure = extract(
        pipeline, monkeypatch, [scene_with_a_bad_shape(), copy.deepcopy(SCENE)])

    assert failure is None
    assert len(served) == 2
    assert "CORRECTION" in served[1]
    assert "needs a borderColor and borderWidth" in served[1]
    assert "CORRECTION" not in served[0]


def test_a_description_rejected_twice_fails_with_the_first_complaint(
        pipeline, monkeypatch):
    bad = scene_with_a_bad_shape()
    served, failure = extract(pipeline, monkeypatch, [bad, bad])

    assert len(served) == 2                     # bounded: not a loop
    assert failure is not None
    assert "s_ring" in failure.message and "borderColor" in failure.message
