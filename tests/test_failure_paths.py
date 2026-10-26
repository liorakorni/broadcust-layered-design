"""Failure scenarios from SPEC section 9.

Each one is a way the pipeline can produce something plausible but wrong. The
point of these tests is that the service notices and says so, naming the stage
that failed, rather than delivering a broken design.
"""

import pytest

import conf
from lib import artifacts, jobs
from lib.errors import (
    AssetInvalid,
    CapabilityMissing,
    CorrectionExhausted,
    InvalidScene,
    InvalidTransparency,
    ReviewBlocked,
    Stage,
    Status,
    TransparencyUnsupported,
)
from lib.providers import image as image_provider, vision as vision_provider
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    CANVAS,
    COPY_ITEMS,
    SCENE,
    fake_overlay,
    fake_photo,
    pipeline,
    png_bytes,
    run_stage,
    skip_external_probes,
)


def through(pipeline, last):
    """Run the pipeline up to and including `last`."""
    order = [
        Stage.PREFLIGHT,
        Stage.PREPARE_COPY,
        Stage.ANALYZE_REFERENCE,
        Stage.CONCEPT,
        Stage.REVIEW_CONCEPT,
        Stage.EXTRACT_SCENE,
        Stage.DECORATION_OVERLAY,
        Stage.CLEAN_PHOTO,
        Stage.VALIDATE_ASSETS,
        Stage.ASSEMBLE,
        Stage.RENDER,
        Stage.REVIEW,
        Stage.FINALIZE,
    ]
    for stage in order[: order.index(last) + 1]:
        run_stage(pipeline["jobId"], stage)


# --- 1. the model cannot produce transparency ------------------------------


def test_transparency_unsupported_is_terminal(pipeline, monkeypatch):
    """Never fall back to keying out white: it destroys light artwork and every
    soft shadow edge."""

    def refuse(*args, **kwargs):
        if kwargs.get("transparent"):
            raise TransparencyUnsupported("Model does not support transparency.")
        return {"bytes": fake_photo(), "mimeType": "image/png", "requestId": "r",
                "model": conf.IMAGE_MODEL}

    through(pipeline, Stage.EXTRACT_SCENE)
    monkeypatch.setattr(image_provider, "edit", refuse)

    with pytest.raises(TransparencyUnsupported) as exc:
        run_stage(pipeline["jobId"], Stage.DECORATION_OVERLAY)
    assert exc.value.code == "TRANSPARENCY_UNSUPPORTED"
    assert exc.value.retryable is False


# --- 2. the overlay comes back opaque --------------------------------------


def test_opaque_overlay_is_rejected_at_validation(pipeline, monkeypatch):
    from PIL import Image

    through(pipeline, Stage.EXTRACT_SCENE)

    def flat(*args, **kwargs):
        data = (
            png_bytes(Image.new("RGBA", (1024, 1024), (255, 255, 255, 254)))
            if kwargs.get("transparent")
            else fake_photo()
        )
        return {"bytes": data, "mimeType": "image/png", "requestId": "r",
                "model": conf.IMAGE_MODEL}

    monkeypatch.setattr(image_provider, "edit", flat)
    run_stage(pipeline["jobId"], Stage.DECORATION_OVERLAY)
    run_stage(pipeline["jobId"], Stage.CLEAN_PHOTO)

    with pytest.raises(InvalidTransparency, match="effectively opaque"):
        run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)


def test_overlay_repair_is_bounded_per_job(pipeline, monkeypatch):
    """The bound lives in a conditional write, so a Lambda retry cannot buy
    extra repairs."""
    from PIL import Image

    through(pipeline, Stage.EXTRACT_SCENE)

    def flat(*args, **kwargs):
        data = (
            png_bytes(Image.new("RGBA", (1024, 1024), (255, 255, 255, 254)))
            if kwargs.get("transparent")
            else fake_photo()
        )
        return {"bytes": data, "mimeType": "image/png", "requestId": "r",
                "model": conf.IMAGE_MODEL}

    monkeypatch.setattr(image_provider, "edit", flat)
    run_stage(pipeline["jobId"], Stage.DECORATION_OVERLAY)
    run_stage(pipeline["jobId"], Stage.CLEAN_PHOTO)

    seen = []
    for _ in range(conf.MAX_OVERLAY_REPAIRS + 1):
        with pytest.raises(InvalidTransparency) as exc:
            run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)
        seen.append(exc.value.retryable)

    assert seen[:conf.MAX_OVERLAY_REPAIRS] == [True] * conf.MAX_OVERLAY_REPAIRS
    assert seen[-1] is False


def test_photograph_surviving_in_the_overlay_is_caught_by_vision(pipeline, monkeypatch):
    """Alpha statistics cannot see this; only the composite inspection can."""
    through(pipeline, Stage.CLEAN_PHOTO)

    real = vision_provider.analyze

    def leftovers(prompt, images=(), *, schema_name, **kwargs):
        if schema_name == "overlay_inspection_v1":
            return {"photographicLeftovers": True, "textLeftovers": False,
                    "artworkPresent": True, "notes": "sky visible",
                    "leftoverDescription": "a strip of photographic sky along the top"}
        return real(prompt, images, schema_name=schema_name, **kwargs)

    monkeypatch.setattr(vision_provider, "analyze", leftovers)
    with pytest.raises(InvalidTransparency, match="photographic scene remains"):
        run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)


# --- 3. the cleaned photo has panel-shaped holes ---------------------------


def test_background_with_unreconstructed_gaps_is_rejected(pipeline, monkeypatch):
    through(pipeline, Stage.EXTRACT_SCENE)

    def holed(*args, **kwargs):
        if kwargs.get("transparent"):
            data = fake_overlay()
        else:
            from PIL import Image, ImageDraw
            import io

            img = Image.open(io.BytesIO(fake_photo())).convert("RGBA")
            ImageDraw.Draw(img).rectangle((100, 100, 500, 500), fill=(0, 0, 0, 0))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        return {"bytes": data, "mimeType": "image/png", "requestId": "r",
                "model": conf.IMAGE_MODEL}

    monkeypatch.setattr(image_provider, "edit", holed)
    run_stage(pipeline["jobId"], Stage.DECORATION_OVERLAY)
    run_stage(pipeline["jobId"], Stage.CLEAN_PHOTO)

    with pytest.raises(AssetInvalid, match="not reconstructed"):
        run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)


# --- 4. the scene invents or alters copy -----------------------------------


def test_scene_referencing_unknown_copy_is_rejected(pipeline, monkeypatch):
    through(pipeline, Stage.REVIEW_CONCEPT)

    real = vision_provider.analyze

    def invented(prompt, images=(), *, schema_name, **kwargs):
        if schema_name == "scene_v1":
            scene = real(prompt, images, schema_name=schema_name, **kwargs)
            scene["textLayers"][0]["contentKey"] = "title.invented"
            return scene
        return real(prompt, images, schema_name=schema_name, **kwargs)

    monkeypatch.setattr(vision_provider, "analyze", invented)
    with pytest.raises(InvalidScene, match="not in the approved copy manifest"):
        run_stage(pipeline["jobId"], Stage.EXTRACT_SCENE)


def test_scene_paraphrasing_approved_copy_is_rejected(pipeline, monkeypatch):
    through(pipeline, Stage.REVIEW_CONCEPT)

    real = vision_provider.analyze

    def paraphrased(prompt, images=(), *, schema_name, **kwargs):
        scene = real(prompt, images, schema_name=schema_name, **kwargs)
        if schema_name == "scene_v1":
            scene["textLayers"][0]["text"] = "חופשה באיטליה"
        return scene

    monkeypatch.setattr(vision_provider, "analyze", paraphrased)
    with pytest.raises(InvalidScene, match="altered the approved copy"):
        run_stage(pipeline["jobId"], Stage.EXTRACT_SCENE)


def test_scene_outside_the_canvas_is_rejected(pipeline, monkeypatch):
    through(pipeline, Stage.REVIEW_CONCEPT)

    real = vision_provider.analyze

    def overflowing(prompt, images=(), *, schema_name, **kwargs):
        scene = real(prompt, images, schema_name=schema_name, **kwargs)
        if schema_name == "scene_v1":
            scene["textLayers"][0]["x"] = 900
            scene["textLayers"][0]["width"] = 700
        return scene

    monkeypatch.setattr(vision_provider, "analyze", overflowing)
    with pytest.raises(InvalidScene, match="beyond the"):
        run_stage(pipeline["jobId"], Stage.EXTRACT_SCENE)


def test_scene_using_an_uninstalled_font_is_rejected(pipeline, monkeypatch):
    """Chromium substitutes silently, so an unapproved font would render in the
    wrong typeface rather than fail."""
    through(pipeline, Stage.REVIEW_CONCEPT)

    real = vision_provider.analyze

    def wrong_font(prompt, images=(), *, schema_name, **kwargs):
        scene = real(prompt, images, schema_name=schema_name, **kwargs)
        if schema_name == "scene_v1":
            scene["textLayers"][0]["fontFamily"] = "Comic Sans MS"
        return scene

    monkeypatch.setattr(vision_provider, "analyze", wrong_font)
    with pytest.raises(InvalidScene, match="installed, approved font"):
        run_stage(pipeline["jobId"], Stage.EXTRACT_SCENE)


# --- 5. review blocks and corrections run out ------------------------------


def test_render_overflow_blocks_review_even_when_the_model_passes(pipeline, monkeypatch):
    """The model says it looks fine; the DOM says the text did not fit. The
    measurement wins."""
    through(pipeline, Stage.ASSEMBLE)

    from lib import render as renderer

    def overflowing(html, canvas, scene):
        return {
            "native": fake_photo(),
            "feed": fake_photo(),
            "checks": {
                "problems": [
                    {"code": "TEXT_OVERFLOW", "layerId": "t_title",
                     "message": "Text in t_title is 220px tall in a 160px box"}
                ],
                "textLayers": len(scene["textLayers"]),
                "images": 2,
            },
        }

    monkeypatch.setattr(renderer, "render", overflowing)
    run_stage(pipeline["jobId"], Stage.RENDER)
    verdict = run_stage(pipeline["jobId"], Stage.REVIEW)

    assert verdict["approved"] is False
    assert verdict["blockingIssues"][0]["code"] == "TEXT_OVERFLOW"


def test_finalize_refuses_a_blocked_design(pipeline, monkeypatch):
    through(pipeline, Stage.RENDER)

    real = vision_provider.analyze

    def blocking(prompt, images=(), *, schema_name, **kwargs):
        if schema_name == "review_v2":
            return {
                "passed": False,
                "blockingIssues": [
                    {"code": "TEXT_UNREADABLE", "layerId": "t_title",
                     "assetId": None, "message": "White text on a white panel"}
                ],
                "suggestions": [],
            }
        return real(prompt, images, schema_name=schema_name, **kwargs)

    monkeypatch.setattr(vision_provider, "analyze", blocking)
    run_stage(pipeline["jobId"], Stage.REVIEW)

    with pytest.raises(ReviewBlocked):
        run_stage(pipeline["jobId"], Stage.FINALIZE)


def test_the_terminal_correct_step_never_regenerates_and_names_why(pipeline):
    """CORRECT no longer advances to another full pass, and no longer fails a design
    that exists. It ends the loop by delivering the best usable candidate with the
    reason recorded, and spends nothing, however often it is invoked."""
    through(pipeline, Stage.REVIEW)
    job_id = pipeline["jobId"]
    review_key = pipeline["table"].items[f"JOB#{job_id}"]["artifacts"][Stage.REVIEW]["review"]
    review = artifacts.get_json(job_id, review_key)
    stuck = {**review, "approved": False,
             "blockingIssues": [{"message": "bad", "code": "X"}],
             "routing": {"route": "exhausted", "reason": "the repair budget is spent",
                         "unresolved": []}}
    stuck_key = artifacts.artifact_key(job_id, Stage.REVIEW, 9, "review.json")
    artifacts.put_json(stuck_key, stuck)
    pipeline["table"].items[f"JOB#{job_id}"]["artifacts"][Stage.REVIEW]["review"] = stuck_key
    rounds = pipeline["table"].items[f"JOB#{job_id}"].get("correctionRounds", 0)
    calls = pipeline["table"].items[f"JOB#{job_id}"]["metrics"].get("imageCalls", 0)

    for attempt in (1, 2, 3):
        handler_correct(job_id, attempt)

    job = pipeline["table"].items[f"JOB#{job_id}"]
    assert job["correctionRounds"] == rounds if "correctionRounds" in job else True
    assert job["metrics"].get("imageCalls", 0) == calls
    result = artifacts.get_json(job_id, job["resultKey"])
    assert job["status"] == "SUCCEEDED_WITH_WARNINGS"
    assert result["stopReason"] == "CORRECTION_EXHAUSTED"
    assert "repair budget is spent" in result["diagnostics"]["original"]["message"]


def handler_correct(job_id, attempt):
    import handler_stage

    event = {"jobId": job_id, "stage": Stage.REVIEW, "attempt": attempt,
             "operation": "CORRECT"}
    return handler_stage.handler(event, None)["artifacts"]


# --- 6. configuration and capability ---------------------------------------


def test_preflight_rejects_a_model_that_cannot_do_transparency(pipeline, monkeypatch):
    monkeypatch.setattr(conf, "IMAGE_MODEL", "gpt-image-2")
    with pytest.raises(CapabilityMissing, match="transparent backgrounds"):
        run_stage(pipeline["jobId"], Stage.PREFLIGHT)


def test_preflight_rejects_a_retired_vision_model(pipeline, monkeypatch):
    monkeypatch.setattr(conf, "SCENE_MODEL", "gpt-4o")
    with pytest.raises(CapabilityMissing, match="retired"):
        run_stage(pipeline["jobId"], Stage.PREFLIGHT)


def test_preflight_reports_every_problem_at_once(pipeline, monkeypatch):
    """Three misconfigurations should take one invocation to diagnose."""
    monkeypatch.setattr(conf, "IMAGE_MODEL", "gpt-image-2")
    monkeypatch.setattr(conf, "SCENE_MODEL", "gpt-4o")
    monkeypatch.setattr(conf, "REVIEW_MODEL", "gpt-4-turbo")

    with pytest.raises(CapabilityMissing) as exc:
        run_stage(pipeline["jobId"], Stage.PREFLIGHT)
    assert exc.value.message.count(";") >= 2


def test_preflight_rejects_an_unauthorized_reference_origin(pipeline, monkeypatch):
    monkeypatch.setattr(conf, "AUTHORIZED_IMAGE_ORIGINS", ("other.example",))
    with pytest.raises(CapabilityMissing, match="not an authorized origin"):
        run_stage(pipeline["jobId"], Stage.PREFLIGHT)


# --- terminal failure is recorded on the job -------------------------------


def test_failure_is_persisted_with_the_real_stage_and_code(pipeline, monkeypatch):
    import handler_stage

    through(pipeline, Stage.EXTRACT_SCENE)
    monkeypatch.setattr(
        image_provider,
        "edit",
        lambda *a, **k: (_ for _ in ()).throw(
            TransparencyUnsupported("Model does not support transparency.")
        ),
    )
    with pytest.raises(TransparencyUnsupported):
        run_stage(pipeline["jobId"], Stage.DECORATION_OVERLAY)

    handler_stage.handler(
        {
            "jobId": pipeline["jobId"],
            "recordFailure": True,
            "failure": {
                "Error": "TransparencyUnsupported",
                "Cause": '{"errorMessage": "DECORATION_OVERLAY could not produce alpha"}',
            },
        },
        None,
    )
    job = jobs.get_job_unscoped(pipeline["jobId"])
    assert job["status"] == Status.FAILED
    assert job["error"]["code"] == "TRANSPARENCY_UNSUPPORTED"
    assert job["error"]["stage"] == Stage.DECORATION_OVERLAY


def test_failed_job_never_reports_a_result(pipeline):
    through(pipeline, Stage.REVIEW)
    jobs.mark_failed(
        pipeline["jobId"],
        {"stage": Stage.REVIEW, "code": "REVIEW_BLOCKED", "message": "blocked",
         "retryable": False},
    )
    job = jobs.get_job_unscoped(pipeline["jobId"])
    assert job["status"] == Status.FAILED
    assert not job.get("resultKey")


# --- bounded overlay repair (SPEC section 4) -------------------------------


def _repair(job_id, attempt=1):
    """Invoke the repair the way the workflow's RepairOverlay state does."""
    import handler_stage

    event = {
        "jobId": job_id,
        "stage": Stage.DECORATION_OVERLAY,
        "attempt": attempt,
        "assetId": None,
        "operation": "REPAIR_OVERLAY",
    }
    return handler_stage.handler(event, None)["artifacts"]


def test_a_rejected_overlay_is_repaired_and_then_passes(pipeline, monkeypatch):
    """The whole point of the bound: a repair must actually be able to succeed.

    Before this was wired the repair counter was consumed and the error marked
    retryable, but nothing ever regenerated the overlay, so every job with a
    leftover died at VALIDATE_ASSETS.
    """
    through(pipeline, Stage.CLEAN_PHOTO)
    job_id = pipeline["jobId"]

    real = vision_provider.analyze
    rejections = {"n": 0}

    def inspection(prompt, images=(), *, schema_name, **kwargs):
        if schema_name == "overlay_inspection_v1":
            rejections["n"] += 1
            leftover = rejections["n"] == 1
            return {
                "photographicLeftovers": False,
                "textLeftovers": leftover,
                "artworkPresent": True,
                "notes": "",
                "leftoverDescription": "a logo mark in the top-right corner"
                if leftover
                else "",
            }
        return real(prompt, images, schema_name=schema_name, **kwargs)

    monkeypatch.setattr(vision_provider, "analyze", inspection)

    with pytest.raises(InvalidTransparency):
        run_stage(job_id, Stage.VALIDATE_ASSETS)

    rejected_key = jobs.get_job_unscoped(job_id)["artifacts"][
        Stage.DECORATION_OVERLAY
    ]["decoration"]

    edits = []

    def capture_edit(prompt, data, width, height, **kwargs):
        edits.append(prompt)
        return {"bytes": fake_overlay(), "mimeType": "image/png",
                "requestId": "repair", "model": conf.IMAGE_MODEL}

    monkeypatch.setattr(image_provider, "edit", capture_edit)
    result = _repair(job_id)

    # The repair is told what to remove, rather than redesigning blind.
    assert "logo mark in the top-right corner" in edits[0]
    # The rejected overlay stays readable; artifacts are append-only.
    assert result["decoration"] != rejected_key
    assert artifacts.get_bytes(job_id, rejected_key)
    # Re-validation now reads the repaired bytes.
    assert jobs.get_job_unscoped(job_id)["artifacts"][Stage.DECORATION_OVERLAY][
        "decoration"
    ] == result["decoration"]

    run_stage(job_id, Stage.VALIDATE_ASSETS)


def test_a_repairable_overlay_raises_the_class_the_workflow_routes_on(
    pipeline, monkeypatch
):
    """Step Functions can branch on an exception class but not on a field."""
    from lib.errors import OverlayRepairNeeded

    through(pipeline, Stage.CLEAN_PHOTO)

    real = vision_provider.analyze

    def leftovers(prompt, images=(), *, schema_name, **kwargs):
        if schema_name == "overlay_inspection_v1":
            return {"photographicLeftovers": False, "textLeftovers": True,
                    "artworkPresent": True, "notes": "",
                    "leftoverDescription": "a word in the banner"}
        return real(prompt, images, schema_name=schema_name, **kwargs)

    monkeypatch.setattr(vision_provider, "analyze", leftovers)
    with pytest.raises(OverlayRepairNeeded):
        run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)


def test_an_exhausted_overlay_repair_is_terminal_not_repairable(
    pipeline, monkeypatch
):
    from lib.errors import OverlayRepairNeeded

    through(pipeline, Stage.CLEAN_PHOTO)
    job_id = pipeline["jobId"]

    real = vision_provider.analyze

    def leftovers(prompt, images=(), *, schema_name, **kwargs):
        if schema_name == "overlay_inspection_v1":
            return {"photographicLeftovers": False, "textLeftovers": True,
                    "artworkPresent": True, "notes": "",
                    "leftoverDescription": "a word in the banner"}
        return real(prompt, images, schema_name=schema_name, **kwargs)

    monkeypatch.setattr(vision_provider, "analyze", leftovers)

    for _ in range(conf.MAX_OVERLAY_REPAIRS):
        with pytest.raises(OverlayRepairNeeded):
            run_stage(job_id, Stage.VALIDATE_ASSETS)

    with pytest.raises(InvalidTransparency) as exc:
        run_stage(job_id, Stage.VALIDATE_ASSETS)
    assert not isinstance(exc.value, OverlayRepairNeeded)
    assert exc.value.retryable is False
    assert "without success" in exc.value.message


# --- the failing operation is named, not the step that happened to catch it ----


def _record(job_id, name, message):
    """Drive RecordFailure the way the workflow's terminal catch does."""
    import json as _json

    import handler_stage

    handler_stage.handler(
        {"jobId": job_id, "recordFailure": True,
         "failure": {"Error": name, "Cause": _json.dumps({"errorMessage": message})}},
        None,
    )
    return jobs.get_job_unscoped(job_id)["error"]


def test_a_repair_light_failure_is_recorded_against_repair_light(pipeline, monkeypatch):
    """The defect behind job 175103fd: an S3 fault inside REPAIR_LIGHT reported FINALIZE.

    The operation now writes its own identity into the message, which is the only
    thing that survives the hop through Step Functions.
    """
    import handler_stage
    from lib.errors import ArtifactAccessDenied
    from lib.stages import repair

    monkeypatch.setattr(repair, "light", lambda **kw: (_ for _ in ()).throw(
        ArtifactAccessDenied("Access denied reading the repair record.")))

    with pytest.raises(ArtifactAccessDenied) as raised:
        handler_stage.handler(
            {"jobId": pipeline["jobId"], "stage": Stage.REVIEW, "attempt": 1,
             "assetId": None, "operation": "REPAIR_LIGHT"},
            None,
        )
    assert "[REPAIR_LIGHT in REVIEW]" in raised.value.message

    error = _record(pipeline["jobId"], "ArtifactAccessDenied", raised.value.message)
    assert error["stage"] == Stage.REVIEW
    assert error["operation"] == "REPAIR_LIGHT"
    assert error["code"] == "ARTIFACT_ACCESS_DENIED"


def test_an_unexpected_error_in_an_operation_still_names_the_operation(pipeline, monkeypatch):
    """A raw boto or Python error used to escape with no stage and no code."""
    import handler_stage
    from lib.errors import LayeredDesignError
    from lib.stages import repair

    monkeypatch.setattr(repair, "light", lambda **kw: (_ for _ in ()).throw(
        RuntimeError("some provider payload that must not be surfaced")))

    with pytest.raises(LayeredDesignError) as raised:
        handler_stage.handler(
            {"jobId": pipeline["jobId"], "stage": Stage.REVIEW, "attempt": 1,
             "assetId": None, "operation": "REPAIR_LIGHT"},
            None,
        )
    assert "provider payload" not in raised.value.message
    error = _record(pipeline["jobId"], "LayeredDesignError", raised.value.message)
    assert (error["stage"], error["operation"]) == (Stage.REVIEW, "REPAIR_LIGHT")


def test_a_pipeline_failure_records_no_operation(pipeline):
    error = _record(pipeline["jobId"], "InvalidScene", "bad scene [EXTRACT_SCENE]")
    assert error["stage"] == Stage.EXTRACT_SCENE
    assert "operation" not in error


def test_a_telemetry_failure_does_not_mask_the_original_error(pipeline, monkeypatch):
    """Observability must not become a failure mode of its own."""
    import handler_stage
    from lib import telemetry
    from lib.stages import repair

    monkeypatch.setattr(telemetry, "record_stage", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("metrics write failed")))
    monkeypatch.setattr(repair, "light", lambda **kw: (_ for _ in ()).throw(
        InvalidScene("the patch named a layer that is not there.")))

    with pytest.raises(InvalidScene) as raised:
        handler_stage.handler(
            {"jobId": pipeline["jobId"], "stage": Stage.REVIEW, "attempt": 1,
             "assetId": None, "operation": "REPAIR_LIGHT"},
            None,
        )
    assert "named a layer" in raised.value.message


def test_telemetry_failure_does_not_fail_a_successful_operation(pipeline, monkeypatch):
    import handler_stage
    from lib import telemetry
    from lib.stages import repair

    monkeypatch.setattr(telemetry, "record_stage", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("metrics write failed")))
    monkeypatch.setattr(repair, "light", lambda **kw: {"route": "light", "nextAttempt": 2})

    out = handler_stage.handler(
        {"jobId": pipeline["jobId"], "stage": Stage.REVIEW, "attempt": 1,
         "assetId": None, "operation": "REPAIR_LIGHT"},
        None,
    )
    assert out["artifacts"]["nextAttempt"] == 2
