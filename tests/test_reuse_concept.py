"""Rebuilding a design from an earlier job's approved concept (`reuseConceptFromJobId`).

The use is re-running reconstruction after a fix, on the concept the customer approved:
no replacement concept, no concept image call, and nothing about the composition can change.
"""


import pytest

from lib import artifacts, jobs
from lib.errors import AssetInvalid, InvalidRequest, Stage, Status
from tests.test_pipeline import pipeline, run_stage, skip_external_probes  # noqa: F401
from tests.test_repair_pipeline import job_of

SOURCE = "job-italy"
REUSER = "job-again"


def source_with_concept(pipeline):
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.CONCEPT, Stage.REVIEW_CONCEPT):
        run_stage(SOURCE, stage)
    return jobs.get_job_unscoped(SOURCE)


def start_reuser(pipeline, *, owner="owner-1", source=SOURCE):
    request_key = artifacts.artifact_key(REUSER, Stage.ACCEPT_REQUEST, 1, "request.json")
    stored = artifacts.get_json(SOURCE, jobs.get_job_unscoped(SOURCE)["requestKey"])
    artifacts.put_json(request_key, stored)
    jobs.create_job(REUSER, owner, "hash-2", request_key, stored["canvas"],
                    reuse_concept_from=source)
    pipeline["table"].items[f"JOB#{REUSER}"]["artifacts"] = {}


def test_the_approved_concept_is_copied_and_frozen_without_any_image_call(pipeline):
    source = source_with_concept(pipeline)
    calls_before = len(pipeline["calls"]["image"])
    start_reuser(pipeline)
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE):
        run_stage(REUSER, stage)
    run_stage(REUSER, Stage.REUSE_CONCEPT)

    assert len(pipeline["calls"]["image"]) == calls_before
    frozen = jobs.approved_concept(jobs.get_job_unscoped(REUSER))
    assert frozen["sha256"] == source["approvedConcept"]["sha256"]
    assert frozen["selectedBy"] == "reused"
    assert frozen["reusedFrom"]["jobId"] == SOURCE
    # The copy lives in the new job's own namespace: it does not depend on the
    # source job's artifacts surviving.
    assert f"/{REUSER}/" in frozen["key"] or REUSER in frozen["key"]
    data = artifacts.get_bytes(REUSER, frozen["key"])
    assert artifacts.sha256_hex(data) == frozen["sha256"]
    review = artifacts.get_json(REUSER, frozen["reviewKey"])
    assert review["conceptSha256"] == frozen["sha256"]
    assert review["approved"] is True


def test_the_rest_of_the_pipeline_works_from_the_reused_concept(pipeline):
    source = source_with_concept(pipeline)
    start_reuser(pipeline)
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.REUSE_CONCEPT, Stage.EXTRACT_SCENE, Stage.DECORATION_OVERLAY,
                  Stage.CLEAN_PHOTO, Stage.VALIDATE_ASSETS, Stage.ASSEMBLE, Stage.RENDER,
                  Stage.REVIEW, Stage.FINALIZE):
        run_stage(REUSER, stage)
    job = jobs.get_job_unscoped(REUSER)
    assert job["status"] == Status.SUCCEEDED
    result = artifacts.get_json(REUSER, job["resultKey"])
    assert result["qa"]["conceptSelection"]["sha256"] == source["approvedConcept"]["sha256"]
    assert result["qa"]["conceptSelection"]["selectedBy"] == "reused"
    # No concept was generated for the new job; any image call is a reconstruction
    # call and is told apart by its operation id.
    assert not [c for c in pipeline["calls"]["image"]
                if f"{REUSER}:concept" in (c["operationId"] or "")]


def test_a_concept_of_another_owner_is_never_read(pipeline):
    source_with_concept(pipeline)
    start_reuser(pipeline, owner="owner-2")
    with pytest.raises(AssetInvalid):
        run_stage(REUSER, Stage.REUSE_CONCEPT)
    assert "approvedConcept" not in job_of({"table": pipeline["table"], "jobId": REUSER})


def test_a_job_that_was_not_started_from_a_concept_cannot_reuse_one(pipeline):
    source_with_concept(pipeline)
    with pytest.raises(AssetInvalid):
        run_stage(SOURCE, Stage.REUSE_CONCEPT)


def test_a_reused_concept_is_never_replaced_by_the_final_review():
    from lib.stages import review

    src = open(review.__file__).read()
    assert 'not job.get("reuseConceptFrom")' in src
