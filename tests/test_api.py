"""Job-plane tests: acceptance, idempotency, ownership, status and reconciliation."""

import copy

import pytest

import handler_api
from lib import artifacts, jobs
from lib.errors import Status
from tests.fakes import FakeS3, FakeStepFunctions, FakeTable

CALLER = {"ownerScope": "tenant-a"}
OTHER_CALLER = {"ownerScope": "tenant-b"}


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    table, sfn, s3 = FakeTable(), FakeStepFunctions(), FakeS3()
    monkeypatch.setattr(jobs, "_tbl", lambda: table)
    monkeypatch.setattr(handler_api, "_states", lambda: sfn)
    monkeypatch.setattr(artifacts, "_client", lambda: s3)
    return {"table": table, "sfn": sfn, "s3": s3}


def start(payload, caller=CALLER):
    return handler_api.handler(
        {"operation": "runLayeredDesign", "request": payload, "trustedCallerContext": caller},
        None,
    )


def status(job_id, caller=CALLER):
    return handler_api.handler(
        {"operation": "getLayeredDesignStatus", "jobId": job_id, "trustedCallerContext": caller},
        None,
    )


# --- acceptance ------------------------------------------------------------


def test_accepts_and_returns_job_id(start_payload, wired):
    res = start(start_payload)
    assert res["success"] is True
    assert res["accepted"] is True
    assert res["status"] == Status.QUEUED
    assert res["jobId"]
    # Acceptance starts exactly one execution.
    assert len(wired["sfn"].started) == 1


def test_accepted_response_carries_no_html(start_payload):
    # Acceptance is not completion: no result may be present.
    res = start(start_payload)
    assert "result" not in res and "html" not in res


def test_request_persisted_before_execution(start_payload, wired):
    res = start(start_payload)
    key = artifacts.artifact_key(res["jobId"], "ACCEPT_REQUEST", 1, "request.json")
    assert key in wired["s3"].objects


def test_invalid_request_rejected_with_stage_and_code(start_payload):
    del start_payload["referenceImageUrl"]
    res = start(start_payload)
    assert res["success"] is False
    assert res["accepted"] is False
    assert res["error"]["stage"] == "ACCEPT_REQUEST"
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert res["error"]["retryable"] is False


def test_missing_operation_rejected(start_payload):
    res = handler_api.handler({"request": start_payload, "trustedCallerContext": CALLER}, None)
    assert res["success"] is False
    assert res["error"]["code"] == "INVALID_REQUEST"


def test_owner_scope_cannot_come_from_request_body(start_payload):
    # A tenantId in the payload must not establish ownership.
    start_payload["tenantId"] = "tenant-a"
    res = handler_api.handler(
        {"operation": "runLayeredDesign", "request": start_payload, "trustedCallerContext": None},
        None,
    )
    assert res["success"] is False
    assert res["error"]["code"] == "UNAUTHORIZED"


# --- idempotency -----------------------------------------------------------


def test_replay_with_identical_content_returns_same_job(start_payload, wired):
    first = start(start_payload)
    second = start(copy.deepcopy(start_payload))
    assert first["jobId"] == second["jobId"]
    # And crucially does not start a second paid execution.
    assert len(wired["sfn"].started) == 1


def test_replay_with_different_content_conflicts(start_payload):
    start(start_payload)
    changed = copy.deepcopy(start_payload)
    changed["brief"]["post"] += " Now with a different offer."
    res = start(changed)
    assert res["success"] is False
    assert res["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_idempotency_is_scoped_per_caller(start_payload, wired):
    a = start(start_payload, CALLER)
    b = start(copy.deepcopy(start_payload), OTHER_CALLER)
    # Same requestId, different tenants: two independent jobs, no conflict.
    assert a["jobId"] != b["jobId"]
    assert b["success"] is True
    assert len(wired["sfn"].started) == 2


# --- reconciliation --------------------------------------------------------


def test_execution_name_is_deterministic():
    assert jobs.execution_name("abc") == jobs.execution_name("abc")
    assert len(jobs.execution_name("x" * 200)) <= 80


def test_crash_before_start_execution_is_reconciled(start_payload, wired):
    # Simulate dying between job creation and StartExecution. Patch the fake
    # directly rather than via monkeypatch.undo(), which would also revert the
    # autouse fixture and let boto3 reach the network.
    sfn = wired["sfn"]
    original = sfn.start_execution

    def explode(**_):
        raise RuntimeError("crashed before StartExecution")

    sfn.start_execution = explode
    with pytest.raises(RuntimeError):
        handler_api.run_layered_design(start_payload, CALLER)

    job_ids = [
        v["jobId"] for k, v in wired["table"].items.items() if k.startswith("IDEM#")
    ]
    assert len(job_ids) == 1
    assert sfn.started == []

    # Retrying adopts the same job and starts exactly one execution.
    sfn.start_execution = original
    res = start(start_payload)
    assert res["jobId"] == job_ids[0]
    assert len(sfn.started) == 1


def test_existing_execution_is_adopted_not_duplicated(start_payload, wired):
    res = start(start_payload)
    arn = wired["table"].items[f"JOB#{res['jobId']}"]["executionArn"]
    # A second ensure must not start another execution nor change the ARN.
    handler_api._ensure_execution(res["jobId"])
    assert len(wired["sfn"].started) == 1
    assert wired["table"].items[f"JOB#{res['jobId']}"]["executionArn"] == arn


# --- status ----------------------------------------------------------------


def test_status_reports_real_stage_not_a_percentage(start_payload):
    job_id = start(start_payload)["jobId"]
    jobs.set_stage(job_id, "DECORATION_OVERLAY")
    res = status(job_id)
    assert res["status"] == Status.RUNNING
    assert res["stage"] == "DECORATION_OVERLAY"
    assert "progress" not in res and "percent" not in res


def test_failed_job_returns_success_true_with_error(start_payload):
    # success:true means the lookup worked; the design job itself failed.
    job_id = start(start_payload)["jobId"]
    jobs.mark_failed(
        job_id,
        {
            "stage": "DECORATION_OVERLAY",
            "code": "INVALID_TRANSPARENCY",
            "message": "The decoration image is fully opaque",
            "retryable": False,
        },
    )
    res = status(job_id)
    assert res["success"] is True
    assert res["status"] == Status.FAILED
    assert res["error"]["code"] == "INVALID_TRANSPARENCY"
    assert res["error"]["stage"] == "DECORATION_OVERLAY"


def test_unimplemented_stage_surfaces_as_not_implemented(start_payload):
    job_id = start(start_payload)["jobId"]
    from lib.errors import NotImplementedStage, as_error_dict

    jobs.mark_failed(job_id, as_error_dict(NotImplementedStage("CONCEPT")))
    res = status(job_id)
    assert res["status"] == Status.FAILED
    assert res["error"]["code"] == "NOT_IMPLEMENTED"
    assert res["error"]["stage"] == "CONCEPT"


def test_other_callers_job_is_not_found(start_payload):
    # Must be indistinguishable from a nonexistent job.
    job_id = start(start_payload, CALLER)["jobId"]
    res = status(job_id, OTHER_CALLER)
    assert res["success"] is False
    assert res["error"]["code"] == "JOB_NOT_FOUND"


def test_unknown_job_is_not_found():
    res = status("does-not-exist")
    assert res["success"] is False
    assert res["error"]["code"] == "JOB_NOT_FOUND"


def test_succeeded_status_returns_stored_result(start_payload, wired):
    job_id = start(start_payload)["jobId"]
    result_key = artifacts.artifact_key(job_id, "FINALIZE", 1, "result.json")
    artifacts.put_json(result_key, {"html": "<div></div>", "layers": []})
    jobs.mark_succeeded(job_id, result_key, "sha256-html", "sha256-manifest")
    res = status(job_id)
    assert res["status"] == Status.SUCCEEDED
    assert res["result"]["html"] == "<div></div>"


# --- bounded counters ------------------------------------------------------


def test_correction_rounds_are_bounded_per_job(start_payload):
    job_id = start(start_payload)["jobId"]
    assert jobs.bump_counter(job_id, "correctionRounds", 2) == 1
    assert jobs.bump_counter(job_id, "correctionRounds", 2) == 2
    # Third attempt is refused: the bound is per job, not per task retry.
    assert jobs.bump_counter(job_id, "correctionRounds", 2) is None


# --- interrupted acceptance ------------------------------------------------


def test_job_creation_resumes_when_it_was_interrupted_after_the_claim(
    start_payload, wired
):
    """A failure between claiming idempotency and creating the job must not
    poison the requestId.

    The claim is durable the moment it is written, so if the next step fails the
    retry sees created=False. Without a completeness check it would skip creation
    entirely and leave a job that has no requestKey, which every stage and every
    status lookup then rejects - permanently, since the claim never lets a second
    creation through.
    """
    original = artifacts.put_json
    calls = {"n": 0}

    def fail_once(key, obj, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("s3 is down")
        return original(key, obj, **kwargs)

    artifacts.put_json = fail_once
    try:
        first = start(start_payload)
        assert first["success"] is False

        second = start(start_payload)
    finally:
        artifacts.put_json = original

    assert second["success"] is True
    job_id = second["jobId"]
    assert jobs.job_exists(job_id)
    # The recovered job is fully usable: the pipeline can find its request and
    # the owner can read its status.
    assert jobs.get_job(job_id, jobs.resolve_owner_scope(CALLER))["requestKey"]
    readable = status(job_id)
    assert readable["success"] is True
    assert readable["status"] in (Status.QUEUED, Status.RUNNING)


def test_a_stub_written_by_the_workflow_is_not_mistaken_for_a_job(wired):
    """UpdateItem creates a bare item when none exists; that is not a job."""
    jobs.set_stage("ghost", "PREFLIGHT")
    assert jobs.job_exists("ghost") is False


# --- artifact recording ----------------------------------------------------


def test_every_stage_can_record_artifacts_on_a_fresh_job(start_payload, wired):
    """create_job must initialise the maps that record_artifacts writes into.

    DynamoDB rejects `SET artifacts.#stage` when `artifacts` is absent instead of
    creating it, so an uninitialised map fails the very first stage of every job.
    """
    from lib.errors import Stage

    job_id = start(start_payload)["jobId"]
    for stage in Stage.PIPELINE:
        jobs.record_artifacts(job_id, stage, 1, {"photo": "some/key.png"})

    stored = wired["table"].items[f"JOB#{job_id}"]
    assert set(stored["artifacts"]) == set(Stage.PIPELINE)


def test_nested_write_without_its_parent_map_is_rejected(wired):
    """Guards the fake itself: silently creating the parent would hide the bug."""
    from botocore.exceptions import ClientError

    jobs.set_stage("stub", "PREFLIGHT")
    with pytest.raises(ClientError, match="document path"):
        jobs.record_artifacts("stub", "PREFLIGHT", 1, {"photo": "k"})
