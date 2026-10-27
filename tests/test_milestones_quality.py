"""FIRST_HTML_RENDERED, the delivered candidate's timing, the quality score and the
v2 result fields (addendum sections 9-11), with faked providers.

These prove what is recorded, when, and that it is consistent with one candidate. They do
not prove the score is calibrated (it is explicitly provisional) or that the real render
and image provider behave: that needs paid runs.
"""

import handler_api
import pytest

from lib import artifacts, jobs, milestones, quality
from lib.errors import Stage, Status
from lib.providers import image as image_provider
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    pipeline, run_stage, skip_external_probes,
)
from tests.test_pipeline_v2 import (  # noqa: F401
    _broken_template, job_of, through_validation, v2,
)


def poll(p):
    caller = {"ownerScope": "tenant-a"}
    job_of(p)["ownerScope"] = jobs.resolve_owner_scope(caller)
    return handler_api.get_layered_design_status(p["jobId"], caller)


def render(p):
    through_validation(p)
    run_stage(p["jobId"], Stage.ASSEMBLE)


# --- FIRST_HTML_RENDERED ----------------------------------------------------------


def test_nothing_is_recorded_until_the_html_has_been_stored_and_rendered(v2):
    render(v2)                                            # HTML stored, not yet rendered
    assert not job_of(v2).get("firstHtmlRenderedAt")
    assert poll(v2).get("draft") is None
    run_stage(v2["jobId"], Stage.RENDER)

    job = job_of(v2)
    assert job["firstHtmlRenderedAt"] == job["artifacts"][Stage.RENDER]["renderedAt"]
    assert job["firstCandidateId"] == "attempt-1" and job["firstAttempt"] == 1
    assert job["timeToFirstHtmlMs"] >= 0
    assert job["firstHtmlKey"] == job["artifacts"][Stage.ASSEMBLE]["html"]
    assert job["firstHtmlRecovered"] is False


def test_the_first_write_wins_and_a_later_candidate_never_moves_it(v2):
    render(v2)
    run_stage(v2["jobId"], Stage.RENDER)
    first = {k: job_of(v2)[k] for k in milestones.FIELDS}

    assert milestones.record_first_html(v2["jobId"], 2) == {k: first[k] for k in first}
    assert {k: job_of(v2)[k] for k in milestones.FIELDS} == first


def test_a_milestone_whose_write_was_interrupted_is_recovered_and_says_so(v2, monkeypatch):
    render(v2)
    with monkeypatch.context() as died:
        died.setattr(milestones, "record_first_html", lambda *a, **k: {})   # process died
        run_stage(v2["jobId"], Stage.RENDER)
    assert not job_of(v2).get("firstHtmlRenderedAt")

    assert milestones.ensure_first_html(v2["jobId"]) is True
    job = job_of(v2)
    assert job["firstHtmlRenderedAt"] and job["firstHtmlRecovered"] is True
    assert milestones.ensure_first_html(v2["jobId"]) is False       # idempotent


def test_a_milestone_failure_never_fails_the_render(v2, monkeypatch):
    render(v2)
    monkeypatch.setattr(jobs, "_update", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert milestones.record_first_html(v2["jobId"], 1) == {}


def test_a_running_job_exposes_the_draft_with_its_review_state_and_never_as_final(v2):
    render(v2)
    run_stage(v2["jobId"], Stage.RENDER)
    response = poll(v2)

    draft = response["draft"]
    assert response["status"] not in (Status.SUCCEEDED, Status.SUCCEEDED_WITH_WARNINGS)
    assert "result" not in response
    assert draft["state"] == "draft" and draft["final"] is False
    assert draft["publishReady"] is False and draft["needsReview"] is True
    assert draft["reviewState"] == "pending-review"
    assert "<html" in draft["html"].lower() or "<div" in draft["html"].lower()
    assert draft["htmlUrl"].startswith("https://") and draft["linkExpiresInSeconds"] <= 900
    assert response["milestones"]["firstCandidateId"] == "attempt-1"


def test_no_expiring_url_is_stored_on_the_job_or_in_the_milestone(v2):
    render(v2)
    run_stage(v2["jobId"], Stage.RENDER)
    poll(v2)
    assert "sig=" not in str(job_of(v2)) and "expires=" not in str(job_of(v2))


def test_a_v1_job_gets_no_milestone_and_no_draft(v2):
    job_of(v2).pop("pipelineVersion")
    render(v2)
    run_stage(v2["jobId"], Stage.RENDER)
    assert not job_of(v2).get("firstHtmlRenderedAt")
    assert "draft" not in poll(v2)


# --- the delivered result ---------------------------------------------------------


def finish(p):
    through_validation(p)
    for stage in (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW, Stage.FINALIZE):
        run_stage(p["jobId"], stage)
    return poll(p)["result"]


def test_the_result_carries_the_v2_fields_all_from_the_delivered_candidate(v2):
    result = finish(v2)
    job = job_of(v2)

    assert result["pipelineVersion"] == "layered-v2"
    assert result["templateTolerance"]["value"] == 20 and result["templateFlexibility"] == 20
    assert result["fallbacksUsed"] == []
    assert result["layerContract"]["checked"] is True and result["layerContract"]["compliant"]
    assert result["rubricVersion"] == quality.RUBRIC_VERSION
    timing = result["timing"]
    # The final render time is the delivered candidate's own, not the end of the job.
    assert timing["finalHtmlRenderedAt"] == job["artifacts"][Stage.RENDER]["renderedAt"]
    assert timing["finalHtmlRenderedAt"] <= timing["completedAt"]
    assert 0 <= timing["timeToFinalHtmlMs"] <= timing["totalDurationMs"]
    assert result["milestones"]["firstCandidateId"] == "attempt-1"
    assert result["milestones"]["timeToFirstHtmlMs"] <= timing["timeToFinalHtmlMs"]
    # Nothing in the stored result is an expiring link.
    assert "sig=" not in str(result) and "X-Amz" not in str(result)


def test_a_clean_run_is_not_marked_down_by_fields_that_do_not_apply(v2):
    result = finish(v2)
    assert result["status"] == Status.SUCCEEDED and result["publishReady"] is True


def test_a_template_that_could_not_be_used_delivers_with_warnings_not_failure(v2, monkeypatch):
    _broken_template(monkeypatch, lambda: b"junk")
    result = finish(v2)

    assert result["status"] == Status.SUCCEEDED_WITH_WARNINGS
    assert result["publishReady"] is False and result["needsReview"] is True
    assert [f["code"] for f in result["fallbacksUsed"]] == ["TEMPLATE_UNDECODABLE"]
    assert result["fallbacksUsed"][0]["kind"] == "template-css-fallback"
    assert any(w["code"] == "TEMPLATE_REPLACED_BY_CSS_FALLBACK" for w in result["warnings"])
    assert result["html"]                                      # the HTML is never discarded


def test_a_scoring_failure_does_not_discard_the_html(v2, monkeypatch):
    monkeypatch.setattr(quality, "assess", lambda **k: (_ for _ in ()).throw(ValueError("x")))
    result = finish(v2)
    assert result["html"] and result["qualityScore"] is None
    assert "failed" in result["qualityReason"]


def test_a_layer_contract_violation_needs_review_and_is_not_publish_ready(v2, monkeypatch):
    from lib import layer_contract

    monkeypatch.setattr(layer_contract, "assess", lambda scene, validation: {
        "checked": True, "version": "t", "compliant": False, "needsReview": True,
        "violations": [{"code": "OWNER_CONFLICT", "affected": ["badge-1"],
                        "message": "Two owners."}]})
    result = finish(v2)
    assert result["needsReview"] is True and result["publishReady"] is False
    assert any(w["code"] == "OWNER_CONFLICT" for w in result["warnings"])


# --- the rubric ----------------------------------------------------------------------

APPROVED = {"approved": True, "blockingIssues": [], "warnings": [], "mandatoryContentOk": True,
            "renderChecks": [{"name": "x", "status": "passed"}]}


def assess(review=APPROVED, fidelity=0.02, job=None, has_concept=True, contract=None):
    return quality.assess(review=review, candidate={"fidelity": fidelity}, job=job or {},
                          layer_contract=contract, has_concept=has_concept)


def test_the_weights_sum_to_one_hundred_and_the_rubric_is_labelled_provisional():
    assert sum(quality.WEIGHTS.values()) == 100
    assert assess()["provisional"] is True and "calibrated" in assess()["calibration"]


def test_a_clean_candidate_scores_full_marks():
    result = assess()
    assert result["qualityScore"] == 100 and result["qualityBand"] == "good"
    assert result["critical"] == [] and result["qualityReason"] is None


def test_open_findings_lower_the_dimension_they_belong_to():
    review = {**APPROVED, "warnings": [{"code": "LOW_CONTRAST_TITLE", "category": "visual"}]}
    result = assess(review)
    assert result["qualityBreakdown"]["readabilityTypography"]["score"] == 100 - quality.WARNING_PENALTY
    assert result["qualityBreakdown"]["content"]["score"] == 100
    assert 0 < 100 - result["qualityScore"] < 10


def test_missing_mandatory_text_is_critical_whatever_the_rest_scores():
    result = assess({**APPROVED, "mandatoryContentOk": False})
    assert result["qualityBreakdown"]["content"]["score"] == 0
    assert result["critical"][0]["code"] == "MANDATORY_CONTENT_MISSING"
    assert result["qualityScore"] >= 60                    # a high score is not a clearance


def test_distance_from_the_concept_is_judged_against_the_requested_flexibility():
    strict = assess(fidelity=0.15, job={"templateTolerance": {"value": 0}})
    loose = assess(fidelity=0.15, job={"templateTolerance": {"value": 100}})
    assert loose["qualityBreakdown"]["conceptAdherence"]["score"] > \
        strict["qualityBreakdown"]["conceptAdherence"]["score"]


def test_an_unmeasured_distance_gives_no_score_and_says_why():
    result = assess(fidelity=None)
    assert result["qualityScore"] is None and result["qualityBreakdown"] is None
    assert "conceptAdherence" in result["qualityReason"]


def test_an_incomplete_review_gives_no_score():
    result = assess({"approved": True})
    assert result["qualityScore"] is None and "incomplete" in result["qualityReason"]


def test_concept_adherence_does_not_apply_without_a_concept_and_its_weight_is_redistributed():
    result = assess(fidelity=None, has_concept=False)
    assert result["qualityScore"] == 100
    assert "conceptAdherence" not in result["qualityBreakdown"]


# --- timing with nothing rendered -----------------------------------------------------


def test_a_job_that_rendered_nothing_reports_null_not_a_guess():
    timing = milestones.final_timing({"createdAt": "2026-10-27T10:00:00+00:00"}, {})
    assert timing["finalHtmlRenderedAt"] is None and timing["timeToFinalHtmlMs"] is None
    assert milestones.first_html_summary({})["firstHtmlRenderedAt"] is None
