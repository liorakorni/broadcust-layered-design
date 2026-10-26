"""Repair progress, judged on evidence (lib/repair_progress.py).

The fixture is job cbf8474e..., which stopped with REPAIR_NO_PROGRESS: one blocking
finding in review 1, one gradient added, two blocking findings in review 2. The
files in tests/fixtures/job_cbf8474e are its real approved concept, its two
renders (reduced), its two reviews and its scenes.
"""

import io
import json
import pathlib

import pytest
from PIL import Image, ImageDraw

from lib import finding_identity, repair_progress, visual_compare

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "job_cbf8474e"
CANVAS = {"width": 1080, "height": 1080}


def read(name):
    return (FIXTURE / name).read_bytes()


def review(name):
    return json.loads(read(name))


def findings(r):
    return list(r["blockingIssues"]) + list(r["warnings"])


def png(color=(0, 0, 0), size=(360, 360), boxes=()):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    for box, fill in boxes:
        draw.rectangle(box, fill=fill)
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


# --- identity --------------------------------------------------------------------


def test_a_reworded_finding_is_the_same_finding():
    """Measured: MISSING_TEXT_SIDE_DARKENING and INSUFFICIENT_TEXT_FIELD_DARKENING,
    naming `add:effect-photo-darkening` and `shape-effect-photo-darkening`."""
    first = review("review1.json")["blockingIssues"][0]
    second = next(i for i in review("review2.json")["blockingIssues"]
                  if i["code"] == "INSUFFICIENT_TEXT_FIELD_DARKENING")

    assert first["code"] != second["code"]
    assert finding_identity.identity(first)[0] == "readability"
    assert finding_identity.same_finding(first, second)


def test_a_warning_that_became_blocking_is_matched_not_new():
    previous = findings(review("review1.json"))
    current = findings(review("review2.json"))

    matched = finding_identity.match(previous, current)

    assert matched["new"] == []                  # nothing in review 2 is a new defect
    assert matched["resolved"] == []
    pairs = {(old["code"], new["code"]) for old, new in matched["pairs"]}
    assert ("HEADLINES_LESS_PROMINENT", "HEADLINE_EMPHASIS_LOSS") in pairs
    assert ("MISSING_TEXT_SIDE_DARKENING", "INSUFFICIENT_TEXT_FIELD_DARKENING") in pairs


def test_findings_in_different_families_never_match():
    a = {"code": "TITLE_CLIPPED", "affected": "text-primary", "affectedElementIds": ["text-primary"]}
    b = {"code": "TITLE_TOO_SMALL", "affected": "text-primary", "affectedElementIds": ["text-primary"]}
    assert not finding_identity.same_finding(a, b)


def test_two_findings_about_different_elements_do_not_match():
    a = {"code": "HEADLINE_TOO_SMALL", "affectedElementIds": ["text-primary"]}
    b = {"code": "HEADLINE_TOO_SMALL", "affectedElementIds": ["text-cta"]}
    assert not finding_identity.same_finding(a, b)


def test_an_unrecognised_defect_matches_only_on_its_own_code():
    a = {"code": "ZEBRA_STRIPES", "affectedElementIds": ["x"]}
    b = {"code": "GIRAFFE_SPOTS", "affectedElementIds": ["x"]}
    assert not finding_identity.same_finding(a, b)
    assert finding_identity.same_finding(a, dict(a))


# --- the failing job -------------------------------------------------------------


def failing_job_verdict(**overrides):
    scene = json.loads(read("scene-repaired.json"))
    args = dict(
        previous_findings=findings(review("review1.json")),
        current_findings=findings(review("review2.json")),
        scene=scene,
        changed_ids={"effects", "shape-effect-photo-darkening"},
        canvas=CANVAS,
        previous_render=read("render1.png"),
        current_render=read("render2.png"),
        concept=read("concept.png"),
    )
    args.update(overrides)
    return repair_progress.evaluate(**args)


def test_the_failing_job_is_a_partial_improvement_not_a_regression():
    """The render moved measurably towards the approved concept where the gradient
    was added. The headline was only a warning that the reviewer promoted."""
    result = failing_job_verdict()

    assert result["verdict"] == repair_progress.PARTIAL_IMPROVEMENT, result["reason"]
    visual = result["evidence"]["visual"]
    assert visual["compared"]
    assert visual["patchGain"] >= repair_progress.MEANINGFUL_GAIN
    # The headline escalation is reported as such, not as damage the repair did.
    assert result["evidence"]["escalated"] == [
        {"from": "headline:primary,secondary", "to": "headline:primary,secondary"}]
    assert result["evidence"]["new"] == []
    assert "became blocking" in result["reason"]


def test_without_pixels_the_same_findings_cannot_prove_a_regression():
    result = failing_job_verdict(previous_render=None, current_render=None, concept=None)

    assert result["verdict"] != repair_progress.VERIFIED_REGRESSION
    assert result["evidence"]["visual"] == {"compared": False}


def test_an_unreadable_image_is_uncertain_not_a_regression():
    result = failing_job_verdict(current_render=b"not a png")

    assert result["verdict"] != repair_progress.VERIFIED_REGRESSION


# --- the five verdicts, on controlled pictures ------------------------------------


def blocking(code, element, **extra):
    return {"code": code, "severity": "blocking", "affected": element,
            "affectedElementIds": [element], **extra}


SCENE = {"canvas": CANVAS, "textLayers": [
    {"id": "text-title", "x": 100, "y": 100, "width": 500, "height": 150},
    {"id": "text-body", "x": 100, "y": 400, "width": 500, "height": 150}],
    "shapeLayers": [], "imageLayers": []}


def evaluate(previous, current, *, before, after, concept, changed=("text-title",)):
    return repair_progress.evaluate(
        previous_findings=previous, current_findings=current, scene=SCENE,
        changed_ids=set(changed), canvas=CANVAS, previous_render=before,
        current_render=after, concept=concept)


BOX = (30, 30, 200, 90)        # in the 360px picture: the title's area


def test_a_resolved_finding_with_nothing_new_is_a_verified_improvement():
    concept = png((255, 255, 255))
    result = evaluate([blocking("HEADLINE_TOO_SMALL", "text-title")], [],
                      before=png((0, 0, 0)), after=concept, concept=concept)
    assert result["verdict"] == "A"


def test_a_render_that_moved_towards_the_concept_with_findings_left_is_partial():
    concept = png((255, 255, 255))
    result = evaluate([blocking("HEADLINE_TOO_SMALL", "text-title")],
                      [blocking("HEADLINE_TOO_SMALL", "text-title")],
                      before=png((0, 0, 0)), after=png((140, 140, 140)), concept=concept)
    assert result["verdict"] == "B"


def test_an_identical_render_with_the_same_finding_is_no_meaningful_change():
    concept = png((255, 255, 255))
    same = png((0, 0, 0))
    result = evaluate([blocking("HEADLINE_TOO_SMALL", "text-title")],
                      [blocking("HEADLINE_TOO_SMALL", "text-title")],
                      before=same, after=same, concept=concept)
    assert result["verdict"] == "C"


def test_a_render_that_moved_away_from_the_concept_is_a_verified_regression():
    concept = png((255, 255, 255))
    result = evaluate([blocking("HEADLINE_TOO_SMALL", "text-title")],
                      [blocking("HEADLINE_TOO_SMALL", "text-title")],
                      before=png((200, 200, 200)), after=png((0, 0, 0)), concept=concept)
    assert result["verdict"] == "D"
    assert "further from the approved concept" in result["reason"]


def test_a_new_blocking_finding_the_pixels_do_not_support_is_uncertain():
    concept = png((255, 255, 255))
    same = png((0, 0, 0))
    result = evaluate([], [blocking("TEXT_OVERLAPS_PHOTO", "text-body")],
                      before=same, after=same, concept=concept, changed=("text-title",))
    assert result["verdict"] == "E"


def test_a_lost_mandatory_text_is_always_a_regression():
    concept = png((255, 255, 255))
    result = repair_progress.evaluate(
        previous_findings=[], current_findings=[], scene=SCENE, changed_ids=set(),
        canvas=CANVAS, previous_render=concept, current_render=concept, concept=concept,
        mandatory_lost=["title.primary"])
    assert result["verdict"] == "D"
    assert "title.primary" in result["reason"]


def test_visual_distance_is_independent_of_canvas_size():
    a, b = png((0, 0, 0), (360, 360)), png((255, 255, 255), (360, 360))
    small = visual_compare.distance(a, b, {"width": 540, "height": 540})
    large = visual_compare.distance(a, b, {"width": 2160, "height": 2160})
    assert small == pytest.approx(large) == pytest.approx(1.0)
