"""Bedrock Action Group integration (SPEC section 7).

Drives the real handler with agent-shaped events against the same fakes the
direct-invoke tests use, so both invocation paths are proven to share one
validation, authorization and service implementation.
"""

import json

import pytest

import handler_api
from lib import artifacts, bedrock, jobs
from lib.errors import Stage
from tests.fakes import FakeS3, FakeStepFunctions, FakeTable

TENANT_A = {"ownerScope": "tenant-a"}
TENANT_B = {"ownerScope": "tenant-b"}


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    table, sfn, s3 = FakeTable(), FakeStepFunctions(), FakeS3()
    monkeypatch.setattr(jobs, "_tbl", lambda: table)
    monkeypatch.setattr(handler_api, "_states", lambda: sfn)
    monkeypatch.setattr(artifacts, "_client", lambda: s3)
    return {"table": table, "sfn": sfn, "s3": s3}


def agent_event(function, params, session=None):
    return {
        "messageVersion": "1.0",
        "actionGroup": "LayeredDesign",
        "function": function,
        "parameters": [
            {"name": k, "type": "string", "value": v} for k, v in params.items()
        ],
        "sessionAttributes": dict(TENANT_A) if session is None else session,
        "promptSessionAttributes": {"locale": "he-IL"},
    }


def body(envelope):
    return json.loads(
        envelope["response"]["functionResponse"]["responseBody"]["TEXT"]["body"]
    )


def state(envelope):
    return envelope["response"]["functionResponse"].get("responseState")


def agent_start(payload, session=None):
    return handler_api.handler(
        agent_event("runLayeredDesign", {"requestJson": json.dumps(payload)}, session),
        None,
    )


def agent_status(job_id, session=None):
    return handler_api.handler(
        agent_event("getLayeredDesignStatus", {"jobId": job_id}, session), None
    )


def direct_status(job_id, caller=TENANT_A):
    return handler_api.handler(
        {
            "operation": "getLayeredDesignStatus",
            "jobId": job_id,
            "trustedCallerContext": caller,
        },
        None,
    )


FULL_RESULT = {
    "jobId": None,
    "status": "SUCCEEDED",
    "html": "<div>full design</div>",
    "htmlHash": "h" * 64,
    "assets": [{"assetId": "background", "url": "https://x/y.png", "sha256": "a" * 64}],
    "assetManifestHash": "m" * 64,
    "layerMap": [{"id": "l_bg", "kind": "background"}],
    "preview": "https://res.cloudinary.com/demo/preview.png",
    "previewUrl": "https://res.cloudinary.com/demo/preview.png",
    "canvas": {"width": 1080, "height": 1080},
    "resultKey": None,
    "htmlKey": "layered-design/J/ASSEMBLE/1/design.html",
    "previewKey": "layered-design/J/RENDER/1/native.png",
    "qaKey": "layered-design/J/REVIEW/1/review.json",
    "designFreedom": 0,
    "qa": {"passed": True, "blockingIssues": [], "renderChecks": []},
}


def succeed(job_id, result):
    """Put a finished job in place without running the pipeline."""
    key = artifacts.artifact_key(job_id, Stage.FINALIZE, 1, "result.json")
    result = {**result, "jobId": job_id, "resultKey": key}
    artifacts.put_json(key, result)
    jobs.mark_succeeded(job_id, key, result["htmlHash"], result["assetManifestHash"])
    return result


# --- start -------------------------------------------------------------------


def test_start_returns_acceptance_only(start_payload, wired):
    envelope = agent_start(start_payload)
    res = body(envelope)
    assert res["success"] is True
    assert res["accepted"] is True
    assert res["status"] == "QUEUED"
    assert res["jobId"]
    assert "html" not in res and "result" not in res
    assert len(wired["sfn"].started) == 1


def test_envelope_shape_and_preservation(start_payload):
    event = agent_event("runLayeredDesign", {"requestJson": json.dumps(start_payload)})
    event["sessionAttributes"] = {"ownerScope": "tenant-a", "keep": "me"}
    envelope = handler_api.handler(event, None)

    assert envelope["messageVersion"] == "1.0"
    assert envelope["response"]["actionGroup"] == "LayeredDesign"
    assert envelope["response"]["function"] == "runLayeredDesign"
    assert envelope["sessionAttributes"] == {"ownerScope": "tenant-a", "keep": "me"}
    assert envelope["promptSessionAttributes"] == {"locale": "he-IL"}
    # The API Gateway shape is wrong for Bedrock.
    assert "statusCode" not in envelope and "body" not in envelope


def test_success_carries_no_failure_state(start_payload):
    assert state(agent_start(start_payload)) is None


# --- malformed input ---------------------------------------------------------


def test_malformed_request_json(start_payload):
    envelope = handler_api.handler(
        agent_event("runLayeredDesign", {"requestJson": "{not json"}), None
    )
    res = body(envelope)
    assert res["success"] is False
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert state(envelope) == "FAILURE"


def test_request_json_must_be_an_object():
    envelope = handler_api.handler(
        agent_event("runLayeredDesign", {"requestJson": "[1, 2]"}), None
    )
    assert body(envelope)["error"]["code"] == "INVALID_REQUEST"


def test_request_json_is_validated_against_the_start_schema(start_payload):
    del start_payload["referenceImageUrl"]
    envelope = agent_start(start_payload)
    assert body(envelope)["error"]["code"] == "INVALID_REQUEST"
    assert state(envelope) == "FAILURE"


@pytest.mark.parametrize("function", ["runLayeredDesign", "getLayeredDesignStatus"])
def test_missing_parameter_is_an_invalid_request(function):
    envelope = handler_api.handler(agent_event(function, {}), None)
    res = body(envelope)
    assert res["success"] is False
    assert res["error"]["code"] == "INVALID_REQUEST"
    assert state(envelope) == "FAILURE"


def test_blank_parameter_is_missing():
    envelope = handler_api.handler(
        agent_event("getLayeredDesignStatus", {"jobId": "   "}), None
    )
    assert body(envelope)["error"]["code"] == "INVALID_REQUEST"


def test_unknown_function_is_rejected():
    envelope = handler_api.handler(agent_event("deleteEverything", {}), None)
    assert body(envelope)["error"]["code"] == "INVALID_REQUEST"
    assert state(envelope) == "FAILURE"
    # Still a valid envelope, so the agent can read the refusal.
    assert envelope["response"]["function"] == "deleteEverything"


def test_duplicate_parameter_is_rejected():
    event = agent_event("getLayeredDesignStatus", {"jobId": "a"})
    event["parameters"].append({"name": "jobId", "type": "string", "value": "b"})
    assert body(handler_api.handler(event, None))["error"]["code"] == "INVALID_REQUEST"


# --- ownership ---------------------------------------------------------------


def test_missing_session_attribute_is_unauthorized(start_payload, wired):
    envelope = agent_start(start_payload, session={})
    res = body(envelope)
    assert res["error"]["code"] == "UNAUTHORIZED"
    assert state(envelope) == "FAILURE"
    assert wired["sfn"].started == []


def test_owner_scope_is_not_an_action_group_parameter(start_payload, wired):
    # A model-supplied ownerScope parameter must not establish identity.
    event = agent_event(
        "runLayeredDesign",
        {"requestJson": json.dumps(start_payload), "ownerScope": "tenant-a"},
        session={},
    )
    assert body(handler_api.handler(event, None))["error"]["code"] == "UNAUTHORIZED"
    assert wired["sfn"].started == []


def test_prompt_session_attributes_never_establish_identity(start_payload):
    event = agent_event(
        "runLayeredDesign", {"requestJson": json.dumps(start_payload)}, session={}
    )
    event["promptSessionAttributes"] = {"ownerScope": "tenant-a"}
    assert body(handler_api.handler(event, None))["error"]["code"] == "UNAUTHORIZED"


def test_another_tenant_cannot_read_the_job(start_payload):
    job_id = body(agent_start(start_payload))["jobId"]
    envelope = agent_status(job_id, session=dict(TENANT_B))
    res = body(envelope)
    assert res["success"] is False
    assert state(envelope) == "FAILURE"
    assert res["error"]["code"] in ("UNAUTHORIZED", "JOB_NOT_FOUND")
    assert "html" not in json.dumps(res)


def test_the_owner_can_read_the_job_through_either_path(start_payload):
    job_id = body(agent_start(start_payload))["jobId"]
    assert body(agent_status(job_id))["success"] is True
    # The same scope, supplied by a trusted direct caller, sees the same job.
    assert direct_status(job_id)["success"] is True
    assert direct_status(job_id, TENANT_B)["success"] is False


# --- status ------------------------------------------------------------------


def test_in_flight_status_reports_the_stage(start_payload):
    job_id = body(agent_start(start_payload))["jobId"]
    jobs.set_stage(job_id, Stage.CONCEPT)
    res = body(agent_status(job_id))
    assert res["success"] is True
    assert res["status"] == "RUNNING"
    assert res["stage"] == Stage.CONCEPT


def test_failed_job_is_not_an_envelope_failure(start_payload):
    job_id = body(agent_start(start_payload))["jobId"]
    jobs.mark_failed(
        job_id,
        {
            "stage": Stage.RENDER,
            "code": "RENDER_FAILED",
            "message": "The design could not be rendered",
            "retryable": False,
        },
    )
    envelope = agent_status(job_id)
    res = body(envelope)
    assert res["success"] is True
    assert res["status"] == "FAILED"
    assert res["error"]["stage"] == Stage.RENDER
    assert state(envelope) is None


def test_compact_result_carries_references_and_hashes_only(start_payload):
    job_id = body(agent_start(start_payload))["jobId"]
    stored = succeed(job_id, FULL_RESULT)

    res = body(agent_status(job_id))
    assert res["success"] is True
    assert res["status"] == "SUCCEEDED"
    assert res["result"] == {
        "resultKey": stored["resultKey"],
        "previewKey": FULL_RESULT["previewKey"],
        "previewUrl": FULL_RESULT["previewUrl"],
        "qaKey": FULL_RESULT["qaKey"],
        # 0 is the default and must survive: a falsy filter would drop it.
        "designFreedom": 0,
        "htmlHash": FULL_RESULT["htmlHash"],
        "assetManifestHash": FULL_RESULT["assetManifestHash"],
    }
    flattened = json.dumps(res)
    for forbidden in ("full design", "layerMap", "assets", "layersKey", "htmlKey"):
        assert forbidden not in flattened, forbidden
    assert "qa" not in res["result"]


def test_unavailable_fields_are_omitted_not_null(start_payload):
    job_id = body(agent_start(start_payload))["jobId"]
    # A result written before the reference fields existed.
    succeed(
        job_id,
        {
            "status": "SUCCEEDED",
            "html": "<div/>",
            "htmlHash": "h" * 64,
            "assetManifestHash": "m" * 64,
        },
    )
    result = body(agent_status(job_id))["result"]
    assert set(result) == {"resultKey", "htmlHash", "assetManifestHash"}
    assert None not in result.values()


def test_direct_invoke_still_returns_the_complete_result(start_payload):
    job_id = body(agent_start(start_payload))["jobId"]
    succeed(job_id, FULL_RESULT)

    full = direct_status(job_id)["result"]
    assert full["html"] == FULL_RESULT["html"]
    assert full["layerMap"] == FULL_RESULT["layerMap"]
    assert full["assets"] == FULL_RESULT["assets"]
    assert full["qa"]["passed"] is True
    # And the direct response is bare: no Bedrock envelope.
    assert "messageVersion" not in direct_status(job_id)


# --- adapter unit behaviour --------------------------------------------------


def test_direct_events_are_not_mistaken_for_agent_events():
    assert not bedrock.is_bedrock_event({"operation": "runLayeredDesign"})
    assert not bedrock.is_bedrock_event({"function": "runLayeredDesign"})
    assert not bedrock.is_bedrock_event(None)


def test_compact_result_does_not_mutate_its_input():
    response = {"success": True, "result": dict(FULL_RESULT)}
    before = json.dumps(response, sort_keys=True)
    bedrock.compact_result(response)
    assert json.dumps(response, sort_keys=True) == before


def test_the_owner_scope_attribute_name_is_configurable(monkeypatch, start_payload):
    monkeypatch.setattr("conf.BEDROCK_OWNER_SCOPE_ATTRIBUTE", "tenant")
    ok = agent_start(start_payload, session={"tenant": "tenant-a"})
    assert body(ok)["success"] is True
    assert body(agent_start(start_payload, session=dict(TENANT_A)))["success"] is False


def test_a_compact_status_keeps_the_draft_as_references_never_the_html():
    response = {"success": True, "jobId": "j", "status": "RUNNING", "draft": {
        "state": "draft", "candidateId": "attempt-1", "attempt": 1, "final": False,
        "publishReady": False, "needsReview": True, "html": "<div>" * 5000,
        "htmlUrl": "https://example.invalid/x?sig=1", "previewUrl": "https://example.invalid/y"}}
    compact = bedrock.compact_result(response)

    assert compact["draft"]["candidateId"] == "attempt-1" and compact["draft"]["final"] is False
    assert not {"html", "htmlUrl", "previewUrl"} & set(compact["draft"])
    assert "html" in response["draft"]                      # the argument is not mutated


def test_a_compact_result_keeps_the_v2_fields_and_a_null_score_with_its_reason():
    response = {"status": "SUCCEEDED", "result": {
        "html": "<x>", "pipelineVersion": "layered-v2", "templateFlexibility": 20,
        "fallbacksUsed": [], "qualityScore": None, "qualityReason": "Not assessed.",
        "qualityBreakdown": {"big": 1}}}
    compact = bedrock.compact_result(response)["result"]

    assert compact["pipelineVersion"] == "layered-v2" and compact["fallbacksUsed"] == []
    assert compact["qualityScore"] is None and compact["qualityReason"] == "Not assessed."
    assert "html" not in compact and "qualityBreakdown" not in compact


def test_the_agent_passes_template_flexibility_and_pipeline_version_through_requestjson(
        start_payload, monkeypatch):
    job_id = body(agent_start({**start_payload, "templateFlexibility": 40}))["jobId"]
    job = jobs.get_job_unscoped(job_id)

    assert job["templateTolerance"]["value"] == 40
    assert job["pipelineVersion"] == "layered-v2"      # new requests reach the updated path
