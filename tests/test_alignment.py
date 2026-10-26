"""Alignment groups, shared fills, sampled shape colours and geometric validation."""

import copy
import io
import json
import pathlib

import pytest
from PIL import Image, ImageDraw

from lib import alignment, assemble_html, concept_sample, geometry, scene_patch, schemas
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    pipeline, run_stage, skip_external_probes,
)
from tests.test_repair_pipeline import (  # noqa: F401
    FIRST_PASS, job_of, review_pass, script,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "job_cbf8474e"
CANVAS = {"width": 1000, "height": 1000}


def text(layer_id, x, y, width, *, align="right", direction="rtl", height=60):
    return {"id": layer_id, "x": x, "y": y, "width": width, "height": height,
            "align": align, "direction": direction, "color": "#FFFFFF",
            "contentKey": layer_id, "text": layer_id, "fontFamily": "Heebo",
            "fontSize": 40, "fontWeight": 400, "zIndex": 5, "role": "headline"}


def concept(bars, size=1000, background=20):
    """A concept whose 'text' is a bright bar per entry: (left, top, right, bottom)."""
    image = Image.new("RGB", (size, size), (background,) * 3)
    draw = ImageDraw.Draw(image)
    for left, top, right, bottom in bars:
        draw.rectangle((left, top, right - 1, bottom - 1), fill=(240, 240, 240))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def scene_of(*layers):
    return {"canvas": dict(CANVAS), "textLayers": list(layers), "shapeLayers": [],
            "imageLayers": [], "effects": [], "alignmentGroups": [], "fillGroups": []}


# --- relationships come from the ink, not from the boxes ------------------------------


def test_lines_that_share_a_right_edge_in_the_concept_are_one_group():
    # Three lines of different widths, flush on the right at x=900; the boxes the
    # model drew are a few pixels off and differently sized.
    image = concept([(500, 100, 900, 160), (300, 200, 902, 260), (620, 300, 898, 360)])
    scene = scene_of(text("a", 480, 100, 430), text("b", 280, 200, 620), text("c", 600, 300, 310))

    groups = alignment.detect_groups(image, scene)

    assert len(groups) == 1
    group = groups[0]
    assert group["edge"] == "right" and group["layerIds"] == ["a", "b", "c"]
    assert 898 <= group["anchor"] <= 902
    assert group["gaps"] == [40, 40]            # the concept's own vertical rhythm


def test_an_intentional_offset_is_not_pulled_onto_the_line():
    image = concept([(500, 100, 900, 160), (300, 200, 900, 260), (400, 300, 760, 360)])
    scene = scene_of(text("a", 480, 95, 430), text("b", 290, 195, 620), text("c", 380, 295, 400))

    group = alignment.detect_groups(image, scene)[0]

    assert group["layerIds"] == ["a", "b"] and "c" not in group["inkEdges"]
    moved, moves = alignment.apply({**scene, "alignmentGroups": [group]})
    assert next(l for l in moved["textLayers"] if l["id"] == "c")["x"] == 380
    assert {m["layerId"] for m in moves} <= {"a", "b"}


def test_a_flush_left_column_is_left_aligned_whatever_the_model_declared():
    """The model wrote align=right; the concept's lines start on one x."""
    image = concept([(100, 100, 500, 160), (100, 200, 700, 260), (102, 300, 400, 360)])
    scene = scene_of(text("a", 90, 95, 430), text("b", 90, 195, 640), text("c", 90, 295, 330))

    group = alignment.detect_groups(image, scene)[0]
    moved, _ = alignment.apply({**scene, "alignmentGroups": [group]})

    assert group["edge"] == "left"
    assert {l["align"] for l in moved["textLayers"]} == {"left"}


def test_centred_lines_are_grouped_only_when_declared_centred():
    image = concept([(300, 100, 700, 160), (400, 200, 600, 260)])
    centred = scene_of(text("a", 290, 95, 420, align="center"),
                       text("b", 390, 195, 220, align="center"))
    other = scene_of(text("a", 290, 95, 420), text("b", 390, 195, 220))

    assert [g["edge"] for g in alignment.detect_groups(image, centred)] == ["center"]
    assert alignment.detect_groups(image, other) == []   # a coincidence, not a design


# --- applying them -----------------------------------------------------------------------


def test_boxes_of_different_widths_share_one_right_edge_after_applying():
    group = {"id": "g", "edge": "right", "layerIds": ["a", "b", "c"], "anchor": 900,
             "inkEdges": {}, "gaps": [], "note": None}
    scene = {**scene_of(text("a", 480, 95, 430), text("b", 280, 195, 620),
                        text("c", 600, 295, 330)), "alignmentGroups": [group]}

    moved, moves = alignment.apply(scene)

    edges = {l["id"]: l["x"] + l["width"] for l in moved["textLayers"]}
    assert set(edges.values()) == {900}
    assert [l["width"] for l in moved["textLayers"]] == [430, 620, 330]    # widths untouched
    assert {m["layerId"] for m in moves} == {"a", "c"}                   # b was already there


def test_applying_is_deterministic_and_leaves_direction_alone():
    group = {"id": "g", "edge": "left", "layerIds": ["a", "b"], "anchor": 160,
             "inkEdges": {}, "gaps": [], "note": None}
    scene = {**scene_of(text("a", 150, 95, 200, align="left", direction="ltr"),
                        text("b", 170, 195, 300, align="left", direction="ltr")),
             "alignmentGroups": [group]}

    once, _ = alignment.apply(scene)
    twice, again = alignment.apply(once)

    assert once == twice and again == []
    assert {l["direction"] for l in once["textLayers"]} == {"ltr"}      # a phone stays LTR
    assert {l["x"] for l in once["textLayers"]} == {160}


def test_a_layer_that_would_leave_the_canvas_is_not_moved_there():
    group = {"id": "g", "edge": "right", "layerIds": ["a", "b"], "anchor": 900,
             "inkEdges": {}, "gaps": [], "note": None}
    scene = {**scene_of(text("a", 100, 95, 800), text("b", 50, 195, 960)),
             "alignmentGroups": [group]}

    moved, _ = alignment.apply(scene)

    assert next(l for l in moved["textLayers"] if l["id"] == "b")["x"] == 50


def test_moving_a_member_on_purpose_leaves_its_group_but_resizing_does_not():
    group = {"id": "g", "edge": "right", "layerIds": ["a", "b", "c"], "anchor": 900,
             "inkEdges": {"a": 900, "b": 900, "c": 900}, "gaps": [], "note": None}
    scene = {**scene_of(text("a", 470, 95, 430), text("b", 280, 195, 620),
                        text("c", 570, 295, 330)), "alignmentGroups": [group]}

    resized = scene_patch.apply_properties(scene, "a", {"width": "300"}, target="layout")
    moved = scene_patch.apply_properties(scene, "a", {"x": "200"}, target="layout")

    assert resized["alignmentGroups"][0]["layerIds"] == ["a", "b", "c"]
    assert moved["alignmentGroups"][0]["layerIds"] == ["b", "c"]
    only_two = scene_patch.apply_properties(moved, "b", {"x": "100"}, target="layout")
    assert only_two["alignmentGroups"] == []                  # one member is not a group
    assert scene["alignmentGroups"][0]["layerIds"] == ["a", "b", "c"]   # input untouched


def test_a_group_may_only_name_text_layers_that_exist_once():
    doc = json.loads((FIXTURES / "scene-initial.json").read_text())
    ok = {"id": "g", "edge": "left", "layerIds": ["text-phone", "text-email"], "anchor": 160,
          "inkEdges": {}, "gaps": [], "note": None}

    def parse(groups, fills=()):
        d = copy.deepcopy(doc)
        d["alignmentGroups"], d["fillGroups"] = groups, list(fills)
        return schemas.parse_scene(
            d, 1080, 1080,
            allowed_content_keys={t["contentKey"] for t in d["textLayers"]},
            allowed_fonts={t["fontFamily"] for t in d["textLayers"]})

    parse([ok])
    with pytest.raises(schemas.InvalidScene, match="not a text layer"):
        parse([{**ok, "layerIds": ["text-phone", "cta-pill"]}])
    with pytest.raises(schemas.InvalidScene, match="one shared edge"):
        parse([ok, {**ok, "id": "h", "layerIds": ["text-email", "text-business"]}])
    with pytest.raises(schemas.InvalidScene, match="not a shape layer"):
        parse([], [{"id": "f", "layerIds": ["cta-pill", "text-phone"], "color": "#FFDC29",
                    "note": None}])


def test_the_failing_jobs_concept_yields_its_real_columns():
    image = (FIXTURES / "concept.png").read_bytes()
    scene = json.loads((FIXTURES / "scene-initial.json").read_text())

    groups = alignment.detect_groups(image, scene)
    by_edge = {tuple(g["layerIds"]): g for g in groups}

    # The headline, subhead and body lines are one flush column; the contact values
    # are another, further in. Both are measured, neither is assumed.
    column = next(g for g in groups if "text-primary" in g["layerIds"])
    contact = next(g for g in groups if "text-phone" in g["layerIds"])
    assert column["edge"] == "left" and {"text-primary", "text-secondary"} <= set(column["layerIds"])
    assert contact["edge"] == "left" and set(contact["layerIds"]) == {"text-phone", "text-email"}
    assert abs(column["anchor"] - 113) <= 6 and abs(contact["anchor"] - 162) <= 6
    assert len(by_edge) == len(groups)


# --- colours: the circle's own fill, not its icon --------------------------------------------


def disc_concept(color, icon):
    image = Image.new("RGB", (1000, 1000), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.ellipse((100, 100, 140, 140), fill=color)
    draw.rectangle((109, 109, 131, 131), fill=icon)       # the pictogram
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


CIRCLE = {"id": "c", "kind": "circle", "x": 100, "y": 100, "width": 40, "height": 40,
          "color": "#FFDB29"}


def test_a_circles_colour_is_read_around_its_icon_not_through_it():
    image = disc_concept((255, 220, 40), icon=(10, 8, 2))

    sample = concept_sample.fill_color(image, CIRCLE, CANVAS)

    assert concept_sample.differs("#FFDC28", sample["color"]) is False
    assert sample["coverage"] >= 0.85
    # The old median of the middle landed on the pictogram.
    assert concept_sample.median_color(image, CIRCLE, CANVAS) != sample["color"]


def test_the_email_circle_of_the_failing_job_is_yellow_not_the_envelope():
    image = (FIXTURES / "concept.png").read_bytes()
    scene = json.loads((FIXTURES / "scene-initial.json").read_text())
    email = next(s for s in scene["shapeLayers"] if s["id"] == "email-circle")
    assert email["color"] == "#2B2105"                       # what the old sampler produced

    sample = concept_sample.fill_color(image, email, scene["canvas"])

    r, g, b = (int(sample["color"][i:i + 2], 16) for i in (1, 3, 5))
    assert r > 230 and g > 190 and b < 80                    # yellow


def test_a_dark_sample_never_replaces_a_plausible_bright_colour():
    doc = {"canvas": dict(CANVAS), "shapeLayers": [dict(CIRCLE)]}
    image = disc_concept((12, 10, 4), icon=(12, 10, 4))        # reads dark all over

    result = concept_sample.measure_shape_colors(image, doc)

    assert doc["shapeLayers"][0]["color"] == "#FFDB29"          # kept
    assert result["corrections"] == []
    assert "icon, border or shadow" in result["ignored"][0]["reason"]


def test_an_area_that_is_not_a_flat_fill_is_not_trusted():
    image = Image.new("RGB", (1000, 1000))
    for x in range(1000):
        for y in range(1000):
            image.putpixel((x, y), ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256))
    out = io.BytesIO()
    image.save(out, "PNG")
    doc = {"canvas": dict(CANVAS), "shapeLayers": [dict(CIRCLE)]}

    result = concept_sample.measure_shape_colors(out.getvalue(), doc)

    assert result["corrections"] == [] and doc["shapeLayers"][0]["color"] == "#FFDB29"
    assert "not a flat fill" in result["ignored"][0]["reason"]


def test_a_panels_colour_is_read_beside_its_words():
    image = Image.new("RGB", (1000, 1000), (30, 30, 30))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((100, 100, 400, 172), radius=36, fill=(255, 220, 40))
    draw.rectangle((160, 120, 340, 152), fill=(0, 0, 0))     # black words on the button
    out = io.BytesIO()
    image.save(out, "PNG")
    panel = {"id": "p", "kind": "panel", "x": 100, "y": 100, "width": 300, "height": 72,
             "radius": 36, "color": "#FFDC29"}

    sample = concept_sample.fill_color(out.getvalue(), panel, CANVAS)

    assert not concept_sample.differs("#FFDC28", sample["color"])


def test_a_ring_keeps_its_border_colour():
    doc = {"canvas": dict(CANVAS), "shapeLayers": [
        {"id": "r", "kind": "ring", "x": 100, "y": 100, "width": 40, "height": 40,
         "color": None, "borderColor": "#FF0000", "borderWidth": 3}]}

    concept_sample.measure_shape_colors(disc_concept((0, 255, 0), (0, 0, 0)), doc)

    assert doc["shapeLayers"][0]["borderColor"] == "#FF0000"


def test_shared_fills_are_recorded_without_forcing_one_colour():
    shapes = [
        {"id": "cta", "kind": "panel", "color": "#FFDC29"},
        {"id": "phone", "kind": "circle", "color": "#FADA2A"},
        {"id": "badge", "kind": "circle", "color": "#D01010"},      # a deliberate second colour
    ]

    groups = alignment.fill_groups({"shapeLayers": shapes})

    assert len(groups) == 1 and groups[0]["layerIds"] == ["cta", "phone"]
    assert [s["color"] for s in shapes] == ["#FFDC29", "#FADA2A", "#D01010"]   # untouched


# --- geometric validation -----------------------------------------------------------------------


def rendered(scene, **boxes):
    """Rects as the renderer reports them: id -> {"ink": box}, keyed by element id."""
    element_of = assemble_html.element_ids(scene)
    return {element_of[i]: {"ink": dict(zip(("left", "top", "right", "bottom"), b))}
            for i, b in boxes.items()}


def group_scene():
    scene = scene_of(text("a", 100, 100, 400), text("b", 100, 200, 300), text("c", 100, 300, 350))
    scene["alignmentGroups"] = [{"id": "g", "edge": "right", "layerIds": ["a", "b", "c"],
                                 "anchor": 500, "inkEdges": {}, "gaps": [40, 40], "note": None}]
    return scene


def codes(problems):
    return sorted((p["code"], p["severity"]) for p in problems)


def test_a_line_that_drifted_off_the_shared_edge_is_a_warning():
    scene = group_scene()
    rects = rendered(scene, a=(100, 100, 500, 160), b=(100, 200, 491, 260), c=(100, 300, 500, 360))

    problems = geometry.check_render(scene, rects, CANVAS)

    assert codes(problems) == [("ALIGNMENT_DRIFT", "warning")]
    assert problems[0]["measured"]["drift"] == -9.0


def test_tolerance_is_relative_to_the_canvas():
    scene = group_scene()
    rects = rendered(scene, a=(100, 100, 500, 160), b=(100, 200, 496, 260), c=(100, 300, 500, 360))
    assert geometry.check_render(scene, rects, CANVAS) == []                    # 4px of 1000
    big = {"width": 4000, "height": 4000}
    scaled = {k: {"ink": {e: v * 4 for e, v in r["ink"].items()}} for k, r in rects.items()}
    assert geometry.check_render(scene, scaled, big) == []                      # 16px of 4000


def test_text_over_text_blocks_when_it_is_heavy_and_warns_when_slight():
    scene = group_scene()
    heavy = rendered(scene, a=(100, 100, 500, 160), b=(100, 130, 500, 190), c=(100, 300, 500, 360))
    slight = rendered(scene, a=(100, 100, 500, 160), b=(100, 155, 500, 215), c=(100, 300, 500, 360))

    assert ("TEXT_OVERLAP", "blocking") in codes(geometry.check_render(scene, heavy, CANVAS))
    assert ("TEXT_OVERLAP", "warning") in codes(geometry.check_render(scene, slight, CANVAS))
    touching = rendered(scene, a=(100, 100, 500, 160), b=(100, 160, 500, 220), c=(100, 300, 500, 360))
    assert not [p for p in geometry.check_render(scene, touching, CANVAS)
                if p["code"] == "TEXT_OVERLAP"]


def test_text_running_off_the_canvas_is_clipping():
    scene = group_scene()
    rects = rendered(scene, a=(100, 100, 500, 160), b=(100, 200, 1060, 260), c=(100, 300, 500, 360))

    assert ("TEXT_CLIPPED", "blocking") in codes(geometry.check_render(scene, rects, CANVAS))


def test_an_even_rhythm_that_rendered_uneven_is_reported_but_an_uneven_one_is_not():
    scene = group_scene()
    uneven = rendered(scene, a=(100, 100, 500, 160), b=(100, 170, 500, 230), c=(100, 330, 500, 390))
    even = rendered(scene, a=(100, 100, 500, 160), b=(100, 200, 500, 260), c=(100, 300, 500, 360))
    assert ("SPACING_UNEVEN", "warning") in codes(geometry.check_render(scene, uneven, CANVAS))
    assert not [p for p in geometry.check_render(scene, even, CANVAS) if p["code"] == "SPACING_UNEVEN"]
    scene["alignmentGroups"][0]["gaps"] = [10, 120]        # the concept was uneven on purpose
    assert not [p for p in geometry.check_render(scene, uneven, CANVAS)
                if p["code"] == "SPACING_UNEVEN"]


def test_no_measurement_means_no_finding():
    assert geometry.check_render(group_scene(), None, CANVAS) == []
    assert geometry.check_render(group_scene(), {}, CANVAS) == []


# --- through the pipeline ----------------------------------------------------------------------------


def test_extraction_records_the_relationships_and_assembly_applies_them(script):
    for stage in FIRST_PASS[:6]:
        run_stage(script["jobId"], stage, attempt=1)

    from lib import artifacts
    from lib.errors import Stage

    job = job_of(script)
    scene = artifacts.get_json(script["jobId"], job["artifacts"][Stage.EXTRACT_SCENE]["scene"])
    assert isinstance(scene["alignmentGroups"], list) and isinstance(scene["fillGroups"], list)
    assert job["artifacts"][Stage.EXTRACT_SCENE]["alignmentGroups"] == len(scene["alignmentGroups"])


def test_assembly_applies_groups_and_records_what_it_moved(script, monkeypatch):
    from lib import artifacts
    from lib.errors import Stage

    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)
    job = job_of(script)
    scene = artifacts.get_json(script["jobId"], job["artifacts"][Stage.EXTRACT_SCENE]["scene"])
    first, second = scene["textLayers"][0], scene["textLayers"][1]
    anchor = max(first["x"] + first["width"], second["x"] + second["width"])
    scene["alignmentGroups"] = [{
        "id": "g", "edge": "right", "layerIds": [first["id"], second["id"]],
        "anchor": anchor, "inkEdges": {}, "gaps": [], "note": None}]
    stored = artifacts.put_json(
        artifacts.artifact_key(script["jobId"], Stage.EXTRACT_SCENE, 1, "scene-grouped.json"), scene)
    job["artifacts"][Stage.EXTRACT_SCENE]["scene"] = stored["key"]

    review_pass(script, 1)

    job = job_of(script)
    assembled = artifacts.get_json(script["jobId"], job["artifacts"][Stage.ASSEMBLE]["scene"])
    members = [l for l in assembled["textLayers"] if l["id"] in (first["id"], second["id"])]
    assert {l["x"] + l["width"] for l in members} == {anchor}
    assert all(m["layerId"] in (first["id"], second["id"])
               for m in job["artifacts"][Stage.ASSEMBLE]["aligned"])


def test_a_drift_measured_by_the_browser_is_reported_without_blocking_approval(script, monkeypatch):
    from lib import render as renderer
    from lib import artifacts
    from lib.errors import Stage

    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)
    job = job_of(script)
    scene = artifacts.get_json(script["jobId"], job["artifacts"][Stage.EXTRACT_SCENE]["scene"])
    ids = [l["id"] for l in scene["textLayers"][:2]]
    scene["alignmentGroups"] = [{"id": "g", "edge": "right", "layerIds": ids, "anchor": 500,
                                 "inkEdges": {}, "gaps": [], "note": None}]
    scene["canvas"] = scene["canvas"]
    stored = artifacts.put_json(
        artifacts.artifact_key(script["jobId"], Stage.EXTRACT_SCENE, 1, "scene-grouped.json"), scene)
    job["artifacts"][Stage.EXTRACT_SCENE]["scene"] = stored["key"]
    previous = renderer.render

    def render(html, canvas, scene_):
        result = previous(html, canvas, scene_)
        element_of = assemble_html.element_ids(scene_)
        result["checks"]["rects"] = {
            element_of[ids[0]]: {"ink": {"left": 100, "top": 100, "right": 500, "bottom": 160}},
            element_of[ids[1]]: {"ink": {"left": 100, "top": 200, "right": 470, "bottom": 260}},
        }
        return result

    monkeypatch.setattr(renderer, "render", render)
    review_pass(script, 1)

    from lib import artifacts as a
    job = job_of(script)
    render_out = job["artifacts"][Stage.RENDER]
    assert [p["code"] for p in render_out["problems"]] == ["ALIGNMENT_DRIFT"]
    review = a.get_json(script["jobId"], job["artifacts"][Stage.REVIEW]["review"])
    assert "ALIGNMENT_DRIFT" in {w["code"] for w in review["warnings"]}
    assert "ALIGNMENT_DRIFT" not in {i["code"] for i in review["blockingIssues"]}
    assert review["approved"] is True


def test_copy_inside_a_button_is_not_pulled_to_the_column_edge():
    """Measured live: a call-to-action centred in its pill was clustered with the
    paragraphs above it (its ink edge was 8px from theirs) and then moved 8px off the
    pill's centre."""
    from lib import alignment

    scene = {
        "canvas": {"width": 1000, "height": 1000},
        "shapeLayers": [{"id": "pill", "kind": "panel", "color": "#FFD526", "x": 600, "y": 700,
                         "width": 300, "height": 70}],
        "textLayers": [
            {"id": "a", "x": 480, "y": 100, "width": 420, "height": 40, "align": "right"},
            {"id": "b", "x": 580, "y": 200, "width": 320, "height": 40, "align": "right"},
            {"id": "cta", "x": 620, "y": 715, "width": 270, "height": 40, "align": "center"},
        ],
    }
    from PIL import Image, ImageDraw
    import io

    image = Image.new("RGB", (1000, 1000), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for box in ((480, 110, 880, 130), (580, 210, 880, 230), (640, 725, 885, 745)):
        draw.rectangle(box, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    groups = alignment.detect_groups(buffer.getvalue(), scene)

    assert [g["layerIds"] for g in groups] == [["a", "b"]]
