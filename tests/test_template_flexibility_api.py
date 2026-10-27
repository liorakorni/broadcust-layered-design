"""Acceptance of templateFlexibility, and that a new request reaches the layered-v2 path
(addendum sections 4, 7 and 11). Jobs accepted before either existed are left alone."""

import copy

import pytest

import conf
from lib import jobs, repair_budget, schemas, template_policy
from lib.errors import InvalidRequest
from tests.test_api import CALLER, start, wired  # noqa: F401  (fixture used by pytest)


def _job(res):
    return jobs.get_job_unscoped(res["jobId"])


def test_an_omitted_flexibility_is_twenty_and_is_persisted_resolved(start_payload):
    job = _job(start(start_payload))

    assert job["templateTolerance"]["value"] == 20
    assert job["templateTolerance"]["profile"] == "small"
    assert job["templateTolerance"]["policyVersion"] == template_policy.POLICY_VERSION
    assert job["templateTolerance"]["tolerances"]["contourShift"] == 0.01


def test_an_explicit_value_selects_its_profile_and_is_independent_of_design_freedom(
        start_payload):
    payload = copy.deepcopy(start_payload)
    payload["templateFlexibility"] = 70
    payload["designFreedom"] = 0
    job = _job(start(payload))

    assert job["templateTolerance"]["profile"] == "broad"
    assert job["designFreedom"] == 0                # the concept dial is untouched


@pytest.mark.parametrize("bad", [-1, 101, 12.5, "20", True, None])
def test_an_invalid_flexibility_is_rejected_at_acceptance(start_payload, bad, wired):
    payload = copy.deepcopy(start_payload)
    payload["templateFlexibility"] = bad
    res = start(payload)

    assert res["success"] is False and res["accepted"] is False
    assert "templateFlexibility" in res["error"]["message"]
    assert wired["sfn"].started == []               # nothing was created or started


def test_omitting_the_field_and_sending_the_default_are_the_same_request(start_payload):
    """A request accepted before the field existed must replay without an idempotency
    conflict, and an explicit 20 means what omission means."""
    omitted = schemas.parse_start_request(copy.deepcopy(start_payload))
    explicit = schemas.parse_start_request({**start_payload, "templateFlexibility": 20})
    other = schemas.parse_start_request({**start_payload, "templateFlexibility": 21})

    assert schemas.canonical_request_hash(omitted) == schemas.canonical_request_hash(explicit)
    assert schemas.canonical_request_hash(omitted) != schemas.canonical_request_hash(other)


def test_a_new_request_is_stamped_with_the_updated_execution_path(start_payload):
    job = _job(start(start_payload))

    assert job["pipelineVersion"] == conf.PIPELINE_VERSION == "layered-v2"
    assert jobs.is_v2(job)
    # The shared repair budget is persisted with the job, counters seeded at zero.
    assert job["repairBudget"]["limits"] == {"templateImage": 1, "domCss": 2, "photoImage": 0}
    assert all(job[repair_budget.counter(k)] == 0 for k in repair_budget.KINDS)


def test_a_job_without_the_attributes_is_read_as_the_legacy_path_and_not_migrated():
    stored = {"pk": "JOB#old", "jobId": "old", "status": "SUCCEEDED"}   # accepted before

    assert jobs.pipeline_version(stored) == conf.LEGACY_PIPELINE_VERSION
    assert not jobs.is_v2(stored)
    assert template_policy.for_job(stored) is None
    assert repair_budget.limits(stored)["domCss"] == 2     # defaults only; nothing stored


def test_the_agent_passes_the_new_field_through_and_an_old_request_unchanged(
        start_payload, wired):
    """The Action Group hands `requestJson` to the same validation as a direct call, so the
    field needs no Agent change to be accepted: it is optional and defaulted."""
    import json

    from tests.test_bedrock import agent_start, body

    plain = body(agent_start(start_payload))
    assert plain["accepted"] is True
    assert jobs.get_job_unscoped(plain["jobId"])["templateTolerance"]["value"] == 20

    flexible = {**start_payload, "requestId": "req-flex-agent", "templateFlexibility": 60}
    accepted = body(agent_start(flexible))
    assert jobs.get_job_unscoped(accepted["jobId"])["templateTolerance"]["profile"] == "broad"
