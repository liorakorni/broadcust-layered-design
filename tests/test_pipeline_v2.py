"""The layered-v2 execution path, end to end, with faked providers.

What these prove: the ORDER and COUNT of calls, which bytes each call was given, what is
persisted, and what the stages decide. They do not prove that the real image model
produces a faithful design base: that needs a paid run (see the change report).
"""

import hashlib
import json

import pytest

import handler_stage
from lib import artifacts, jobs, repair_budget, template_check
from lib.errors import Stage, Status
from lib.providers import image as image_provider
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    CANVAS, REQUEST, fake_overlay, fake_photo, pipeline, png_bytes, run_stage,
    skip_external_probes,
)

JOB = "job-v2"

QUIET = {"checked": True, "version": "t", "tolerance": {"value": 20, "profile": "small",
                                                          "policyVersion": "p"},
         "measured": {}, "regions": [], "findings": [], "major": False}
MAJOR = {**QUIET, "major": True, "regions": [{"box": {"x": .7, "y": 0, "width": .02,
                                                      "height": 1}, "span": 1.0, "area": .02,
                                                "color": "#ffd400"}],
         "findings": [{"code": "TEMPLATE_ADDED_STRUCTURE", "severity": "major",
                       "message": "The template has a vertical border the concept does not.",
                       "region": {"x": .7, "y": 0, "width": .02, "height": 1}}]}


@pytest.fixture
def v2(pipeline, monkeypatch):
    """The shared fixture's providers, with the job replaced by a layered-v2 job."""
    table = pipeline["table"]
    table.items.pop("JOB#job-italy")
    key = artifacts.artifact_key(JOB, Stage.ACCEPT_REQUEST, 1, "request.json")
    artifacts.put_json(key, REQUEST)
    jobs.create_job(JOB, "owner-1", "hash-v2", key, CANVAS, pipeline_version="layered-v2",
                    template_flexibility=20)
    table.items[f"JOB#{JOB}"]["artifacts"] = {}
    pipeline["jobId"] = JOB
    pipeline["check"] = {"result": QUIET}
    monkeypatch.setattr(template_check, "compare",
                        lambda concept, template, scene, tolerance=None: pipeline["check"]["result"])
    monkeypatch.setattr(template_check, "added_artwork_mask",
                        lambda *a, **k: __import__("PIL.Image", fromlist=["Image"]).new(
                            "L", k["size"], 0))
    return pipeline


def job_of(p):
    return p["table"].items[f"JOB#{p['jobId']}"]


def through_validation(p, first="PREFLIGHT"):
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE, Stage.CONCEPT,
                  Stage.REVIEW_CONCEPT, Stage.EXTRACT_SCENE, Stage.DECORATION_OVERLAY,
                  Stage.CLEAN_PHOTO, Stage.VALIDATE_ASSETS):
        run_stage(p["jobId"], stage)


def operation(p, name, attempt=1, stage=Stage.REVIEW):
    event = {"jobId": p["jobId"], "stage": stage, "attempt": attempt, "assetId": None,
             "operation": name}
    return handler_stage.handler(event, None)["artifacts"]


def schemas_called(p):
    return [c["schema"] for c in p["calls"]["vision"]]


def validation_of(p):
    return artifacts.get_json(p["jobId"], job_of(p)["artifacts"][Stage.VALIDATE_ASSETS]["validation"])


# --- the first HTML does not wait on a visual verdict about the template ---------


def test_the_first_html_needs_no_standalone_template_review(v2):
    through_validation(v2)
    for stage in (Stage.ASSEMBLE, Stage.RENDER):
        run_stage(v2["jobId"], stage)

    called = schemas_called(v2)
    assert "overlay_inspection_v1" not in called and "composite_fidelity_v1" not in called
    assert job_of(v2)["artifacts"][Stage.ASSEMBLE]["html"]
    validation = validation_of(v2)
    assert validation["modelCalls"] == 0 and validation["imageGenerationCalls"] == 0
    assert validation["pipelineVersion"] == "layered-v2"


def test_a_clean_run_makes_exactly_three_image_calls_and_succeeds(v2):
    through_validation(v2)
    for stage in (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW, Stage.FINALIZE):
        run_stage(v2["jobId"], stage)

    assert len(v2["calls"]["image"]) == 3                      # concept, design base, photo
    assert job_of(v2)["status"] == Status.SUCCEEDED


def test_the_design_base_request_carries_the_approved_concept_and_the_focused_prompt(v2):
    through_validation(v2)
    approved = jobs.approved_concept(job_of(v2))
    concept = artifacts.get_bytes(v2["jobId"], approved["key"])
    call = next(c for c in v2["calls"]["image"] if c["operationId"].endswith(":DECORATION_OVERLAY:1"))

    assert hashlib.sha256(call["source"]).hexdigest() == approved["sha256"] == \
        hashlib.sha256(concept).hexdigest()
    assert call["mask"] is None and call["transparent"] is True
    assert call["prompt"].startswith("Extract only the structural design base")
    assert "Do not add shapes, borders, contours, ornaments or new design features." in call["prompt"]
    assert "Template tolerance (small)" in call["prompt"]
    assert "brief" not in call["prompt"].lower()                # the brief is not reintroduced
    trace = [k for k in v2["s3"].objects if k.endswith("provider-trace-decoration.json")]
    recorded = json.loads(v2["s3"].objects[trace[0]])
    assert recorded["inputs"][0]["sha256"] == approved["sha256"]
    assert recorded["prompt"]["name"] == "design-base"


def test_the_photograph_request_never_receives_the_design_base_prompt(v2):
    through_validation(v2)
    photo = next(c for c in v2["calls"]["image"] if c["operationId"].endswith(":CLEAN_PHOTO:1"))
    assert not photo["prompt"].startswith("Extract only the structural design base")
    assert photo["mask"]


# --- an unusable template: CSS fallback, no HTML lost, nothing else touched ------


def _broken_template(monkeypatch, make):
    original = image_provider.edit

    def edit(prompt, source, w, h, *, transparent=False, **kw):
        result = original(prompt, source, w, h, transparent=transparent, **kw)
        if transparent:
            return {**result, "bytes": make()}
        return result

    monkeypatch.setattr(image_provider, "edit", edit)


def test_an_undecodable_template_is_replaced_by_css_and_the_first_html_still_exists(
        v2, monkeypatch):
    _broken_template(monkeypatch, lambda: b"not an image")
    through_validation(v2)
    photo_before = job_of(v2)["artifacts"][Stage.CLEAN_PHOTO]["sha256"]

    validation = validation_of(v2)
    assert validation["fallback"]["code"] == "TEMPLATE_UNDECODABLE"
    assert validation["template"]["problems"][0]["category"] == "technical"
    assert validation["template"]["present"] is False
    for stage in (Stage.ASSEMBLE, Stage.RENDER):
        run_stage(v2["jobId"], stage)
    scene = artifacts.get_json(v2["jobId"], job_of(v2)["artifacts"][Stage.ASSEMBLE]["scene"])

    assert [l["type"] for l in scene["imageLayers"]] == ["background"]
    assert {e["assetId"] for e in job_of(v2)["artifacts"][Stage.ASSEMBLE]["assets"]} == \
        {"background"}
    assert job_of(v2)["artifacts"][Stage.CLEAN_PHOTO]["sha256"] == photo_before
    assert len(v2["calls"]["image"]) == 3                       # no extra call for the fallback
    assert [f["code"] for f in jobs.fallbacks_used(job_of(v2))] == ["TEMPLATE_UNDECODABLE"]
    assert any(w["code"] == "TEMPLATE_REPLACED_BY_CSS_FALLBACK"
               for w in jobs.stage_warnings(job_of(v2)))
    assert job_of(v2)["artifacts"][Stage.DECORATION_OVERLAY]["omitted"] is True


def test_a_template_that_hides_the_photograph_is_a_technical_obstruction_not_a_transparency_fault(
        v2, monkeypatch):
    from PIL import Image

    _broken_template(monkeypatch, lambda: png_bytes(Image.new("RGBA", (1024, 1024),
                                                              (10, 10, 10, 255))))
    through_validation(v2)

    problems = {p["code"]: p for p in validation_of(v2)["template"]["problems"]}
    assert "TEMPLATE_OBSTRUCTS_PHOTO" in problems
    assert problems["TEMPLATE_OBSTRUCTS_PHOTO"]["category"] == "obstruction"
    assert "INVALID_TRANSPARENCY" not in json.dumps(validation_of(v2))
    assert validation_of(v2)["fallback"]["kind"] == "template-css-fallback"


def test_an_undecodable_template_does_not_fail_the_job_to_warnings_by_itself(v2, monkeypatch):
    _broken_template(monkeypatch, lambda: b"junk")
    through_validation(v2)
    for stage in (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW, Stage.FINALIZE):
        run_stage(v2["jobId"], stage)

    assert job_of(v2)["status"] in (Status.SUCCEEDED, "SUCCEEDED_WITH_WARNINGS")
    assert job_of(v2)["status"] != Status.FAILED


# --- one template repair, after the first HTML, and the photograph is untouched --


def test_a_major_finding_keeps_the_template_for_the_first_html_and_is_judged_on_the_composite(v2):
    v2["check"]["result"] = MAJOR
    through_validation(v2)

    validation = validation_of(v2)
    assert validation["fallback"] is None                      # the budget is not spent yet
    assert validation["template"]["major"][0]["code"] == "TEMPLATE_ADDED_STRUCTURE"
    for stage in (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW):
        run_stage(v2["jobId"], stage)

    review = artifacts.get_json(v2["jobId"], job_of(v2)["artifacts"][Stage.REVIEW]["review"])
    codes = [i["code"] for i in review["blockingIssues"]]
    assert "TEMPLATE_ADDED_STRUCTURE" in codes
    assert review["routing"]["route"] == "asset"
    assert review["routing"]["issues"][0]["assetTarget"] == "decoration"
    assert review["routing"]["budget"]["remaining"]["templateImage"] == 1


def test_the_one_template_repair_does_not_rebuild_the_photograph(v2):
    v2["check"]["result"] = MAJOR
    through_validation(v2)
    for stage in (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW):
        run_stage(v2["jobId"], stage)
    photo_before = job_of(v2)["artifacts"][Stage.CLEAN_PHOTO]
    images_before = len(v2["calls"]["image"])

    planned = operation(v2, "REPAIR_ASSET", attempt=1)
    assert planned["next"] == "overlay"
    repaired = operation(v2, "REPAIR_OVERLAY", attempt=2, stage=Stage.DECORATION_OVERLAY)

    assert repaired["rebuildPhoto"] is False                  # the workflow then skips RebuildPhoto
    assert len(v2["calls"]["image"]) == images_before + 1     # the template call alone
    assert job_of(v2)["artifacts"][Stage.CLEAN_PHOTO] == photo_before
    repair_call = v2["calls"]["image"][-1]
    assert "CORRECTION" in repair_call["prompt"] and repair_call["mask"] is None
    assert repair_budget.used(job_of(v2), "templateImage") == 1
    assert repair_budget.used(job_of(v2), "photoImage") == 0


def test_a_second_template_repair_is_refused_and_a_still_major_template_goes_to_css(v2):
    v2["check"]["result"] = MAJOR
    through_validation(v2)
    for stage in (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW):
        run_stage(v2["jobId"], stage)
    operation(v2, "REPAIR_ASSET", attempt=1)
    operation(v2, "REPAIR_OVERLAY", attempt=2, stage=Stage.DECORATION_OVERLAY)

    run_stage(v2["jobId"], Stage.VALIDATE_ASSETS, attempt=2)      # still major, budget now spent
    validation = validation_of(v2)
    assert validation["fallback"]["code"] == "TEMPLATE_ADDED_STRUCTURE"
    assert job_of(v2)["artifacts"][Stage.DECORATION_OVERLAY]["omitted"] is True

    assert repair_budget.consume(JOB, "templateImage") is None   # nothing left to spend
    assert len(v2["calls"]["image"]) == 4                        # concept, base, photo, repair


def test_the_total_image_calls_for_the_fixture_failure_shape_are_four_not_seven(v2):
    """The recorded job spent 1 concept + 3 overlay + 3 photo calls to reach no HTML."""
    v2["check"]["result"] = MAJOR
    through_validation(v2)
    for stage in (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW):
        run_stage(v2["jobId"], stage)
    operation(v2, "REPAIR_ASSET", attempt=1)
    operation(v2, "REPAIR_OVERLAY", attempt=2, stage=Stage.DECORATION_OVERLAY)
    run_stage(v2["jobId"], Stage.VALIDATE_ASSETS, attempt=2)

    assert len(v2["calls"]["image"]) == 4
    assert sum(1 for c in v2["calls"]["image"] if c["operationId"].endswith("CLEAN_PHOTO:1")) == 1


def test_a_photograph_repair_needs_its_own_documented_grant(v2):
    through_validation(v2)
    assert repair_budget.consume(JOB, "photoImage") is None       # default: none
    assert repair_budget.grant_photo_repair(JOB, "The headline is still visible in the photo.")
    assert repair_budget.consume(JOB, "photoImage") == 1
    assert repair_budget.consume(JOB, "photoImage") is None       # exactly one
    assert job_of(v2)["repairBudget"]["photoGrant"]["reason"].startswith("The headline")


# --- a gradient-only base is never sent to the image model ---------------------


def test_a_gradient_only_design_base_is_drawn_in_css_and_makes_no_raster_call(
        v2, monkeypatch):
    scene = json.loads(json.dumps(__import__("tests.test_pipeline", fromlist=["SCENE"]).SCENE))
    scene["designBase"] = {"representation": "css-only", "note": "a plain fade"}
    scene["overlaySamples"] = [s for s in scene["overlaySamples"] if s["kind"] == "transparent"]
    from lib.providers import vision as vision_provider

    original = vision_provider.analyze

    def analyze(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "scene_v1":
            v2["calls"]["vision"].append({"schema": schema_name, "images": len(images),
                                          "detail": kw.get("detail"), "model": None,
                                          "prompt": prompt})
            return json.loads(json.dumps(scene))
        return original(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", analyze)
    through_validation(v2)

    pointer = job_of(v2)["artifacts"][Stage.DECORATION_OVERLAY]
    assert pointer["omitted"] is True and pointer["representation"] == "css-only"
    assert len(v2["calls"]["image"]) == 2                        # concept and photograph only
    assert all(not c["transparent"] for c in v2["calls"]["image"])
    assert validation_of(v2)["template"]["present"] is False
    assert validation_of(v2)["fallback"] is None                 # deliberate, not a fallback
    assert jobs.fallbacks_used(job_of(v2)) == []
    stored = artifacts.get_json(v2["jobId"], job_of(v2)["artifacts"][Stage.EXTRACT_SCENE]["scene"])
    assert stored["layerPlan"]["template"]["gradientOnly"] is True


# --- the legacy path is left alone ----------------------------------------------


def test_a_job_without_a_pipeline_version_still_takes_the_legacy_validation(pipeline):
    through_validation(pipeline)

    assert "overlay_inspection_v1" in schemas_called(pipeline)    # the standalone review ran
    pointer = job_of(pipeline)["artifacts"][Stage.DECORATION_OVERLAY]
    assert pointer.get("decoration") and "omitted" not in pointer
