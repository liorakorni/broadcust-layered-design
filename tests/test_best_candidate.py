"""Returning the best usable HTML instead of failing on quality alone.

A design that exists, renders and can be edited is worth more to the caller than an
error. These tests cover when it is returned, what it is flagged with, which
candidate it is, and the case where there is nothing to return.
"""

import json

import pytest

import handler_api
import handler_stage
from lib import artifacts, bedrock, candidates, jobs
from lib.errors import RenderFailed, Stage
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    pipeline, run_stage, skip_external_probes,
)
from tests.test_repair_pipeline import (  # noqa: F401  (script is a fixture)
    OUTLINE, delivered, drive, first_pass, issue, job_of, op, script,
)


def poll(p):
    """The status call a client makes, as the job's own owner."""
    caller = {"ownerScope": "tenant-a"}
    job_of(p)["ownerScope"] = jobs.resolve_owner_scope(caller)
    return handler_api.get_layered_design_status(p["jobId"], caller)


def record_failure(p, error="RenderFailed", message="The render crashed."):
    """What the workflow's RecordFailure state sends the stage Lambda."""
    return handler_stage.handler({
        "jobId": p["jobId"], "stage": "FINALIZE", "attempt": 1, "recordFailure": True,
        "failure": {"Error": error, "Cause": json.dumps({"errorMessage": message})},
    }, None)["artifacts"]


# --- a later step fails while an earlier candidate is intact ---------------------


def test_failure_after_an_earlier_usable_candidate_returns_that_candidate(script, monkeypatch):
    from lib import render as render_module

    script["script"].append([OUTLINE])
    first_pass(script)                              # candidate 1: reviewed, blocking
    attempt = op(script, "REPAIR_LIGHT", 1)["nextAttempt"]
    run_stage(script["jobId"], Stage.ASSEMBLE, attempt=attempt)

    def crash(*a, **k):
        raise RenderFailed("The browser crashed while rendering attempt 2.")

    monkeypatch.setattr(render_module, "render", crash)
    with pytest.raises(RenderFailed):
        run_stage(script["jobId"], Stage.RENDER, attempt=attempt)

    outcome = record_failure(script, "RenderFailed",
                             "The browser crashed while rendering attempt 2.")

    assert outcome["salvaged"] is True               # the workflow ends Succeeded
    status, result = delivered(script)
    assert status == "SUCCEEDED_WITH_WARNINGS"
    assert result["selectedAttempt"] == 1
    assert result["publishReady"] is False and result["needsReview"] is True
    assert result["qualityPassed"] is False
    assert result["stopReason"] == "RENDER_FAILED"
    # The original technical error is preserved, not replaced by the salvage.
    original = result["diagnostics"]["original"]
    assert original["code"] == "RENDER_FAILED"
    assert "browser crashed" in original["message"]
    codes = {w["code"] for w in result["warnings"]}
    assert {"HEADLINE_OUTLINE_MISSING", "PROCESSING_ENDED_EARLY"} <= codes
    # And what is returned is candidate 1's own document.
    assert result["htmlHash"] == job_of(script)["designCandidates"]["1"]["htmlHash"]
    assert job_of(script).get("error") is None
    assert job_of(script)["outcome"]["publishReady"] is False


def test_no_usable_candidate_means_the_job_fails_with_the_original_error(pipeline):
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE, Stage.CONCEPT):
        run_stage(pipeline["jobId"], stage)

    outcome = record_failure(pipeline, "RenderFailed", "Nothing was ever assembled.")

    assert outcome["salvaged"] is False
    job = job_of(pipeline)
    assert job["status"] == "FAILED"
    assert job["error"]["code"] == "RENDER_FAILED"
    assert "resultKey" not in job


def test_salvage_never_replaces_the_original_error_when_it_cannot_complete(script, monkeypatch):
    """If delivering the earlier candidate itself goes wrong, the job fails with
    the error that actually happened, not with a secondary one."""
    from lib.stages import review

    script["script"].append([OUTLINE])
    first_pass(script)
    monkeypatch.setattr(review, "_publish", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("storage unavailable")))

    outcome = record_failure(script, "RenderFailed", "The original failure.")

    assert outcome["salvaged"] is False
    assert job_of(script)["status"] == "FAILED"
    assert job_of(script)["error"]["message"] == "The original failure."


# --- quality termination reaches best-candidate finalisation ----------------------


def test_exhausted_repairs_deliver_the_best_candidate_not_a_failure(script):
    script["script"].extend([[issue(
        "HEADLINE_OUTLINE_MISSING", target="typography", affected=["t_title"],
        constraints=[("t_title", "strokeColor", "#FFFFFF"),
                     ("t_title", "strokeWidth", str(3 + i))])] for i in range(8)])

    drive(script)

    status, result = delivered(script)
    assert status == "SUCCEEDED_WITH_WARNINGS"
    assert result["stopReason"] in ("CORRECTION_EXHAUSTED", "REPAIR_NO_PROGRESS")
    assert result["html"] and result["layerMap"]
    # Findings are those of the candidate returned, each with an action to take.
    blocking = [w for w in result["warnings"] if w["severity"] == "blocking"]
    assert blocking and all(w["suggestedAction"] for w in blocking)
    assert all("affectedElementIds" in w and "message" in w for w in blocking)


def test_the_polled_status_returns_the_result_for_a_delivery_with_warnings(script):
    script["script"].extend([[issue(
        "HEADLINE_OUTLINE_MISSING", target="typography", affected=["t_title"],
        constraints=[("t_title", "strokeColor", "#FFFFFF"),
                     ("t_title", "strokeWidth", str(3 + i))])] for i in range(8)])
    drive(script)
    response = poll(script)

    assert response["success"] is True
    assert response["status"] == "SUCCEEDED_WITH_WARNINGS"
    assert response["result"]["publishReady"] is False
    assert response["outcome"]["stopReason"] == response["result"]["stopReason"]
    compact = bedrock.compact_result(response)
    assert compact["result"]["publishReady"] is False
    assert compact["result"]["needsReview"] is True
    assert "html" not in compact["result"]


def test_a_delivered_status_whose_result_cannot_be_read_is_not_reported_as_success(script):
    script["script"].append([])
    drive(script)
    job_of(script)["resultKey"] = "layered-design/job-italy/FINALIZE/9/missing.json"

    response = poll(script)

    assert response["status"] == "FAILED"
    assert "could not be read" in response["error"]["message"]
    assert "result" not in response


# --- a clean success is unchanged ---------------------------------------------------


def test_an_approved_design_is_still_a_clean_success(script):
    script["script"].append([])
    drive(script)

    status, result = delivered(script)
    assert status == "SUCCEEDED"
    assert result["qualityPassed"] is True and result["publishReady"] is True
    assert result["needsReview"] is False and result["stopReason"] == "APPROVED"
    assert result["selectedAttempt"] == 1
    assert "diagnostics" not in result


# --- one coherent candidate ------------------------------------------------------------


def test_the_result_is_built_from_one_candidate_revision(script):
    script["script"].append([OUTLINE])
    drive(script)

    job = job_of(script)
    status, result = delivered(script)
    chosen = job["designCandidates"][str(result["selectedAttempt"])]
    pointers = chosen["pointers"]

    assert result["htmlHash"] == chosen["htmlHash"]
    assert result["selectedSnapshotKey"] == chosen["snapshotKey"]
    assert result["previewKey"] == pointers[Stage.RENDER]["native"]
    assert result["htmlKey"] == pointers[Stage.ASSEMBLE]["html"]
    assert result["qaKey"] == pointers[Stage.REVIEW]["review"]
    snapshot = artifacts.get_json(script["jobId"], chosen["snapshotKey"])
    assert snapshot["html"]["sha256"] == chosen["htmlHash"]
    # The layer map describes that candidate's scene, and every element it names is
    # in that candidate's document.
    scene = artifacts.get_json(script["jobId"], pointers[Stage.ASSEMBLE]["scene"])
    assert {l["id"] for l in result["layerMap"]} == {
        l["id"] for key in ("textLayers", "shapeLayers", "imageLayers")
        for l in scene.get(key) or []}
    assert all(l["elementId"] in result["html"] for l in result["layerMap"])
    # The delivered design is the job's current design, so edits start from it.
    assert job["designState"]["key"]


def test_a_candidate_whose_document_changed_is_not_returned(script):
    script["script"].append([OUTLINE])
    drive(script)
    # Two candidates; corrupt the stored document of the better one.
    job = job_of(script)
    best = candidates.select_best_usable(jobs.design_candidates(job))
    key = best["pointers"][Stage.ASSEMBLE]["html"]
    from lib.stages import review

    assert review._coherent(script["jobId"], best) is None          # intact first
    script["s3"].objects[key] = b"<html>tampered</html>"
    assert "no longer matches" in review._coherent(script["jobId"], best)


# --- ranking ---------------------------------------------------------------------------


def candidate(attempt, **overrides):
    base = {
        "attempt": attempt, "approved": False, "mandatoryContentOk": True,
        "blockingCount": 1, "warningCount": 0, "readabilityBlocking": 0,
        "editabilityBlocking": 0, "fidelity": 0.08, "progressVerdict": None,
        "route": "light", "htmlHash": f"h{attempt}", "snapshotKey": f"s{attempt}",
        "pointers": {"ASSEMBLE": {"html": "h", "scene": "s", "assets": [{}]},
                     "RENDER": {"native": "n"}},
    }
    base.update(overrides)
    return base


def test_the_better_picture_wins_over_the_lower_blocking_count():
    """Job cbf8474e: attempt 1 had one blocking finding and had lost the dark text
    field; attempt 2 had two and had restored most of it. Counting findings picks
    the worse picture; the pixel comparison against the concept does not."""
    one = candidate(1, blockingCount=1, readabilityBlocking=1, fidelity=0.0908)
    two = candidate(2, blockingCount=2, readabilityBlocking=1, fidelity=0.0646)

    chosen = candidates.select_best_usable([one, two])

    assert chosen["attempt"] == 2
    reason = candidates.selection_reason(chosen, [one, two])
    assert "fewer blocking findings" in reason and "further from the approved concept" in reason


def test_the_latest_attempt_is_not_chosen_for_being_latest():
    good = candidate(1, fidelity=0.03)
    worse = candidate(2, fidelity=0.09)
    assert candidates.select_best_usable([good, worse])["attempt"] == 1


def test_a_verified_regression_ranks_below_a_candidate_that_is_not():
    steady = candidate(1, fidelity=0.05)
    regressed = candidate(2, fidelity=0.049, progressVerdict="D")
    assert candidates.select_best_usable([steady, regressed])["attempt"] == 1


def test_mandatory_content_outranks_every_visual_criterion():
    complete = candidate(1, fidelity=0.2, blockingCount=4, readabilityBlocking=2)
    missing = candidate(2, mandatoryContentOk=False, fidelity=0.01, blockingCount=0)
    assert candidates.select_best_usable([missing, complete])["attempt"] == 1


def test_a_candidate_missing_text_is_still_returned_when_it_is_all_there_is():
    missing = candidate(1, mandatoryContentOk=False)
    assert candidates.select_best_usable([missing])["attempt"] == 1
    assert candidates.select_deliverable([missing]) is None       # but never as a pass


def test_an_unusable_candidate_is_never_selected():
    broken = candidate(1, snapshotKey=None)
    assert candidates.select_best_usable([broken]) is None


def test_missing_mandatory_text_is_disclosed_and_blocks_publication(script):
    from lib.stages import review

    script["script"].append([OUTLINE])
    first_pass(script)
    record = job_of(script)["designCandidates"]["1"]
    record["mandatoryContentOk"] = False

    review.finalize_best_effort(script["jobId"], 1, stop_reason="REPAIR_NO_PROGRESS",
                                detail={"code": "X", "message": "x"})

    _, result = delivered(script)
    assert result["publishReady"] is False
    first = result["warnings"][0]
    assert first["code"] == "MANDATORY_CONTENT_MISSING" and first["severity"] == "blocking"
    assert "must not be published" in first["message"]
    assert result["html"]                         # returned for manual editing
