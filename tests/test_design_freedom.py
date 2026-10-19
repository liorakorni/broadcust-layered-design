"""designFreedom: caller-controlled reference adherence (SPEC section 2.7).

Covers request validation and hashing, the policy bands, the two new stages, the
bounded concept-repair budget, the frozen approved concept, and the result and
Bedrock fields that carry the effective value.
"""

import copy
import json

import pytest

import handler_api
import handler_stage
from lib import artifacts, bedrock, jobs, prompts, reference_policy
from lib.errors import (
    AssetInvalid,
    InvalidRequest,
    ReferenceAdherenceFailed,
    ReferenceConstraintConflict,
    ReviewBlocked,
    Stage,
)
from lib.providers import image as image_provider, vision as vision_provider
from lib.schemas import canonical_request_hash, parse_start_request
from lib.stages import concept as concept_stage
from lib.stages import review_concept
from tests.fakes import FakeStepFunctions
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    concept_verdict,
    layout_issue,
    pipeline,
    run_pipeline,
    run_stage,
    skip_external_probes,
)

# --- request validation ------------------------------------------------------


def test_omitted_defaults_to_zero(start_payload):
    start_payload.pop("designFreedom", None)
    assert parse_start_request(start_payload).designFreedom == 0


@pytest.mark.parametrize("value", [0, 1, 50, 100])
def test_explicit_values_are_preserved(start_payload, value):
    start_payload["designFreedom"] = value
    assert parse_start_request(start_payload).designFreedom == value


@pytest.mark.parametrize("value", [-1, 101, None, "10", 10.5, True, False, [], {}])
def test_invalid_values_are_rejected(start_payload, value):
    start_payload["designFreedom"] = value
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_omitted_and_explicit_zero_hash_identically(start_payload):
    omitted = copy.deepcopy(start_payload)
    omitted.pop("designFreedom", None)
    explicit = copy.deepcopy(start_payload)
    explicit["designFreedom"] = 0
    assert canonical_request_hash(parse_start_request(omitted)) == canonical_request_hash(
        parse_start_request(explicit)
    )


def test_a_different_value_changes_the_hash(start_payload):
    zero = copy.deepcopy(start_payload)
    zero["designFreedom"] = 0
    fifty = copy.deepcopy(start_payload)
    fifty["designFreedom"] = 50
    assert canonical_request_hash(parse_start_request(zero)) != canonical_request_hash(
        parse_start_request(fifty)
    )


# --- policy bands ------------------------------------------------------------


@pytest.mark.parametrize(
    "value,mode",
    [
        (0, "strict-adherence"),
        (1, "close-adaptation"),
        (25, "close-adaptation"),
        (26, "guided-adaptation"),
        (50, "guided-adaptation"),
        (51, "broad-inspiration"),
        (75, "broad-inspiration"),
        (76, "free-composition"),
        (100, "free-composition"),
    ],
)
def test_band_boundaries(value, mode):
    policy = reference_policy.derive_reference_policy(value)
    assert policy["mode"] == mode
    assert policy["designFreedom"] == value
    assert policy["policyVersion"] == "1.0"


@pytest.mark.parametrize("value", [-1, 101, True, 10.5, "10", None])
def test_derive_refuses_values_the_request_would_have_rejected(value):
    with pytest.raises(ValueError):
        reference_policy.derive_reference_policy(value)


def test_every_policy_carries_the_documented_fields():
    for value in (0, 10, 50, 75, 100):
        policy = reference_policy.derive_reference_policy(value)
        # mustPreserve is empty at 100 on purpose; see the next test.
        for field in ("mayChange", "documentedExceptions", "instruction"):
            assert policy[field], (value, field)


def test_free_composition_mandates_no_preservation():
    assert reference_policy.derive_reference_policy(100)["mustPreserve"] == []
    assert reference_policy.derive_reference_policy(0)["mustPreserve"]


# --- idempotency and invocation paths ----------------------------------------


@pytest.fixture
def api(monkeypatch):
    from tests.fakes import FakeS3, FakeTable

    table, sfn, s3 = FakeTable(), FakeStepFunctions(), FakeS3()
    monkeypatch.setattr(jobs, "_tbl", lambda: table)
    monkeypatch.setattr(handler_api, "_states", lambda: sfn)
    monkeypatch.setattr(artifacts, "_client", lambda: s3)
    return {"table": table, "sfn": sfn, "s3": s3}


def direct_start(payload, caller="tenant-a"):
    return handler_api.handler(
        {"operation": "runLayeredDesign", "request": payload,
         "trustedCallerContext": {"ownerScope": caller}},
        None,
    )


def agent_start(payload, caller="tenant-a"):
    event = {
        "messageVersion": "1.0",
        "actionGroup": "LayeredDesign",
        "function": "runLayeredDesign",
        "parameters": [{"name": "requestJson", "type": "string",
                        "value": json.dumps(payload)}],
        "sessionAttributes": {"ownerScope": caller},
        "promptSessionAttributes": {},
    }
    envelope = handler_api.handler(event, None)
    return json.loads(envelope["response"]["functionResponse"]["responseBody"]["TEXT"]["body"])


def test_changing_the_value_under_an_existing_request_id_conflicts(start_payload, api):
    start_payload["designFreedom"] = 10
    assert direct_start(start_payload)["success"] is True
    changed = copy.deepcopy(start_payload)
    changed["designFreedom"] = 60
    res = direct_start(changed)
    assert res["success"] is False
    assert res["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_direct_and_bedrock_paths_preserve_the_same_effective_value(start_payload, api):
    direct = copy.deepcopy(start_payload)
    direct["requestId"] = "req-direct"
    direct["designFreedom"] = 40
    agent = copy.deepcopy(direct)
    agent["requestId"] = "req-agent"

    direct_id = direct_start(direct)["jobId"]
    agent_id = agent_start(agent)["jobId"]
    for job_id in (direct_id, agent_id):
        job = api["table"].items[f"JOB#{job_id}"]
        assert job["designFreedom"] == 40
        assert job["policyVersion"] == "1.0"


def test_an_omitted_value_is_persisted_as_zero(start_payload, api):
    start_payload.pop("designFreedom", None)
    job_id = direct_start(start_payload)["jobId"]
    job = api["table"].items[f"JOB#{job_id}"]
    assert job["designFreedom"] == 0
    assert job["conceptRounds"] == 0


def test_an_invalid_value_is_rejected_on_both_paths(start_payload, api):
    start_payload["designFreedom"] = 101
    assert direct_start(start_payload)["error"]["code"] == "INVALID_REQUEST"
    assert agent_start(start_payload)["error"]["code"] == "INVALID_REQUEST"


def test_compact_result_keeps_a_design_freedom_of_zero():
    response = {"success": True, "status": "SUCCEEDED",
                "result": {"resultKey": "k", "designFreedom": 0}}
    assert bedrock.compact_result(response)["result"] == {
        "resultKey": "k", "designFreedom": 0}


# --- helpers for stage tests -------------------------------------------------


def set_freedom(pipeline, value):
    job_id = pipeline["jobId"]
    job = pipeline["table"].items[f"JOB#{job_id}"]
    request = artifacts.get_json(job_id, job["requestKey"])
    request["designFreedom"] = value
    artifacts.put_json(job["requestKey"], request, overwrite=True)
    job["designFreedom"] = value


def capture_concept_prompts(monkeypatch):
    seen = []
    real = image_provider.edit

    def spy(prompt, source, *args, **kwargs):
        seen.append(prompt)
        return real(prompt, source, *args, **kwargs)

    monkeypatch.setattr(image_provider, "edit", spy)
    return seen


def run_to_concept_review(pipeline, attempt=1):
    job_id = pipeline["jobId"]
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE):
        run_stage(job_id, stage)
    run_stage(job_id, Stage.CONCEPT, attempt=attempt)
    return run_stage(job_id, Stage.REVIEW_CONCEPT, attempt=attempt)


def job_of(pipeline):
    return pipeline["table"].items[f"JOB#{pipeline['jobId']}"]


# --- ANALYZE_REFERENCE -------------------------------------------------------


def test_analyze_reference_persists_policy_and_analysis(pipeline):
    set_freedom(pipeline, 10)
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY):
        run_stage(pipeline["jobId"], stage)
    out = run_stage(pipeline["jobId"], Stage.ANALYZE_REFERENCE)
    assert out["mode"] == "close-adaptation"

    policy = artifacts.get_json(pipeline["jobId"], out["policy"])
    assert policy["designFreedom"] == 10
    assert policy["mode"] == "close-adaptation"
    analysis = artifacts.get_json(pipeline["jobId"], out["analysis"])
    assert {a["id"] for a in analysis["anchors"]} == {"hero", "panel"}
    assert "font" not in json.dumps(analysis).lower()


def test_analyze_reference_sends_real_reference_pixels_and_policy(pipeline):
    set_freedom(pipeline, 0)
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE):
        run_stage(pipeline["jobId"], stage)
    call = [c for c in pipeline["calls"]["vision"]
            if c["schema"] == "reference_analysis_v1"][0]
    assert call["images"] == 1
    assert call["detail"] == "original"
    assert "strict-adherence" in call["prompt"]


def test_analysis_rejects_a_box_outside_the_unit_range(pipeline, monkeypatch):
    from tests.test_pipeline import REFERENCE_ANALYSIS
    from lib.errors import InvalidModelJson

    bad = json.loads(json.dumps(REFERENCE_ANALYSIS))
    bad["anchors"][0]["box"]["width"] = 4.0
    real = vision_provider.analyze

    def broken(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "reference_analysis_v1":
            return bad
        return real(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", broken)
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY):
        run_stage(pipeline["jobId"], stage)
    with pytest.raises(InvalidModelJson):
        run_stage(pipeline["jobId"], Stage.ANALYZE_REFERENCE)


# --- concept prompt ----------------------------------------------------------


@pytest.mark.parametrize("value", [0, 10, 50, 75, 100])
def test_concept_prompt_states_the_policy_in_english(pipeline, monkeypatch, value):
    set_freedom(pipeline, value)
    seen = capture_concept_prompts(monkeypatch)
    run_to_concept_review(pipeline)
    prompt = seen[0]
    policy = reference_policy.derive_reference_policy(value)
    assert policy["instruction"] in prompt
    assert policy["mode"] in prompt
    for phrase in policy["mustPreserve"] + policy["mayChange"]:
        assert phrase in prompt


@pytest.mark.parametrize("value", [0, 10])
def test_low_freedom_never_unconditionally_requests_a_new_layout(
        pipeline, monkeypatch, value):
    set_freedom(pipeline, value)
    seen = capture_concept_prompts(monkeypatch)
    run_to_concept_review(pipeline)
    prompt = seen[0].lower()
    assert "not a template" not in prompt
    assert "do not copy its section count" not in prompt
    assert "freely redesign" not in prompt
    assert "preserve" in prompt


def test_the_concept_prompt_template_has_no_standing_anti_template_rule():
    text = prompts.load("concept").lower()
    assert "not a template" not in text
    assert "{policy_block}" in text


def test_strict_prompt_carries_the_reference_anchors(pipeline, monkeypatch):
    set_freedom(pipeline, 0)
    seen = capture_concept_prompts(monkeypatch)
    run_to_concept_review(pipeline)
    assert '"id": "hero"' in seen[0]


def test_free_composition_prompt_does_not_lock_geometry(pipeline, monkeypatch):
    set_freedom(pipeline, 100)
    seen = capture_concept_prompts(monkeypatch)
    run_to_concept_review(pipeline)
    assert "Reference anchors" not in seen[0]


# --- REVIEW_CONCEPT ----------------------------------------------------------


def test_an_approved_concept_is_frozen_with_its_review(pipeline):
    out = run_to_concept_review(pipeline)
    assert out["approved"] is True
    frozen = job_of(pipeline)["approvedConcept"]
    assert frozen["key"] == job_of(pipeline)["artifacts"][Stage.CONCEPT]["concept"]
    assert frozen["reviewKey"] == out["review"]
    assert frozen["designFreedom"] == 0
    assert frozen["policyVersion"] == "1.0"
    review = artifacts.get_json(pipeline["jobId"], out["review"])
    assert review["conceptSha256"] == frozen["sha256"]


def test_review_concept_sends_reference_and_concept_pixels(pipeline):
    run_to_concept_review(pipeline)
    call = [c for c in pipeline["calls"]["vision"]
            if c["schema"] == "concept_review_v1"][0]
    assert call["images"] == 2


def test_strict_mode_unjustified_displacement_fails_review(pipeline):
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=False, issues=[layout_issue()])
    )
    out = run_to_concept_review(pipeline)
    assert out["approved"] is False
    assert out["blockingIssues"][0]["category"] == "layoutAdherence"
    assert "approvedConcept" not in job_of(pipeline)


def test_justified_brand_and_contact_replacement_does_not_fail(pipeline):
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=True, issues=[])  # exceptions reported, no issues
    )
    out = run_to_concept_review(pipeline)
    assert out["approved"] is True
    review = artifacts.get_json(pipeline["jobId"], out["review"])
    assert review["documentedExceptions"] == ["business name replaced"]


def test_a_model_passed_flag_cannot_override_blocking_issues(pipeline):
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=True, issues=[layout_issue()])
    )
    assert run_to_concept_review(pipeline)["approved"] is False


def test_layout_differences_are_not_blockers_at_one_hundred(pipeline):
    set_freedom(pipeline, 100)
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=False, issues=[layout_issue()], mode="free-composition")
    )
    assert run_to_concept_review(pipeline)["approved"] is True


def test_invented_content_still_blocks_at_one_hundred(pipeline):
    set_freedom(pipeline, 100)
    invented = {"code": "INVENTED_PRICE", "category": "content",
                "message": "Shows a price that is not in the brief."}
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=False, issues=[invented, layout_issue()],
                        mode="free-composition")
    )
    out = run_to_concept_review(pipeline)
    assert out["approved"] is False
    assert [i["code"] for i in out["blockingIssues"]] == ["INVENTED_PRICE"]


def test_an_unresolvable_conflict_is_terminal_and_never_relaxed(pipeline):
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=False, conflict="The required text cannot fit the panel.")
    )
    with pytest.raises(ReferenceConstraintConflict) as exc:
        run_to_concept_review(pipeline)
    assert exc.value.code == "REFERENCE_CONSTRAINT_CONFLICT"
    assert exc.value.stage == Stage.REVIEW_CONCEPT
    assert exc.value.retryable is False
    assert "cannot fit" in exc.value.message
    # designFreedom is never raised to get past a conflict.
    assert job_of(pipeline)["designFreedom"] == 0


# --- concept repair budget ---------------------------------------------------


def test_concept_repair_is_bounded_and_separate_from_html_rounds(pipeline):
    job_id = pipeline["jobId"]
    for _ in range(2):
        pipeline["concept_verdicts"].append(
            concept_verdict(passed=False, issues=[layout_issue()])
        )
    run_to_concept_review(pipeline)

    first = handler_stage.handler(
        {"jobId": job_id, "stage": Stage.REVIEW_CONCEPT, "attempt": 1,
         "assetId": None, "operation": "CORRECT_CONCEPT"}, None)
    assert first["artifacts"] == {"round": 1, "nextAttempt": 2}
    second = handler_stage.handler(
        {"jobId": job_id, "stage": Stage.REVIEW_CONCEPT, "attempt": 2,
         "assetId": None, "operation": "CORRECT_CONCEPT"}, None)
    assert second["artifacts"] == {"round": 2, "nextAttempt": 3}
    assert job_of(pipeline)["correctionRounds"] == 0

    with pytest.raises(ReferenceAdherenceFailed) as exc:
        handler_stage.handler(
            {"jobId": job_id, "stage": Stage.REVIEW_CONCEPT, "attempt": 3,
             "assetId": None, "operation": "CORRECT_CONCEPT"}, None)
    assert exc.value.code == "REFERENCE_ADHERENCE_FAILED"
    assert "Hero image moved" in exc.value.message
    assert Stage.REVIEW_CONCEPT in exc.value.message
    assert job_of(pipeline)["conceptRounds"] == 2


def test_the_effective_value_does_not_change_during_repairs(pipeline):
    set_freedom(pipeline, 30)
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=False, issues=[layout_issue()], mode="guided-adaptation")
    )
    run_to_concept_review(pipeline)
    handler_stage.handler(
        {"jobId": pipeline["jobId"], "stage": Stage.REVIEW_CONCEPT, "attempt": 1,
         "assetId": None, "operation": "CORRECT_CONCEPT"}, None)
    assert job_of(pipeline)["designFreedom"] == 30


def test_a_regenerated_concept_is_prompted_with_the_targeted_repair(
        pipeline, monkeypatch):
    seen = capture_concept_prompts(monkeypatch)
    pipeline["concept_verdicts"].append(
        concept_verdict(passed=False, issues=[layout_issue("Panel sits too high.")])
    )
    run_to_concept_review(pipeline)
    handler_stage.handler(
        {"jobId": pipeline["jobId"], "stage": Stage.REVIEW_CONCEPT, "attempt": 1,
         "assetId": None, "operation": "CORRECT_CONCEPT"}, None)
    run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=2)
    assert "FIX THESE PROBLEMS" not in seen[0]
    assert "Panel sits too high." in seen[1]


def test_regenerating_the_concept_supersedes_the_frozen_one(pipeline):
    first = run_to_concept_review(pipeline)
    frozen_first = dict(job_of(pipeline)["approvedConcept"])
    out = run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=2)
    run_stage(pipeline["jobId"], Stage.REVIEW_CONCEPT, attempt=2)
    frozen_second = job_of(pipeline)["approvedConcept"]
    assert frozen_second["key"] == out["concept"] != frozen_first["key"]
    assert frozen_second["reviewKey"] != first["review"]


def test_stages_after_the_review_refuse_an_unreviewed_concept(pipeline):
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.CONCEPT):
        run_stage(pipeline["jobId"], stage)
    with pytest.raises(AssetInvalid):
        run_stage(pipeline["jobId"], Stage.EXTRACT_SCENE)


def test_separation_edits_the_frozen_concept_not_the_latest(pipeline):
    job_id = pipeline["jobId"]
    run_to_concept_review(pipeline)
    frozen = job_of(pipeline)["approvedConcept"]["key"]
    # A later, unreviewed concept must not be picked up by the branches.
    run_stage(job_id, Stage.CONCEPT, attempt=2)
    assert job_of(pipeline)["artifacts"][Stage.CONCEPT]["concept"] != frozen
    run_stage(job_id, Stage.EXTRACT_SCENE)
    run_stage(job_id, Stage.CLEAN_PHOTO)
    run_stage(job_id, Stage.DECORATION_OVERLAY)
    sources = [c["source"] for c in pipeline["calls"]["image"]
               if c["operationId"] and ":concept:" not in c["operationId"]]
    assert sources and all(
        s == artifacts.get_bytes(job_id, frozen) for s in sources)


# --- failure attribution -----------------------------------------------------


def test_review_concept_failures_are_not_attributed_to_concept():
    assert handler_stage._stage_from_message("x [REVIEW_CONCEPT]") == Stage.REVIEW_CONCEPT
    assert handler_stage._stage_from_message("x [CONCEPT]") == Stage.CONCEPT


def test_a_concept_repair_failure_is_recorded_against_review_concept(pipeline):
    job_id = pipeline["jobId"]
    run_to_concept_review(pipeline)
    job_of(pipeline)["conceptRounds"] = 2
    with pytest.raises(ReferenceAdherenceFailed) as exc:
        handler_stage.handler(
            {"jobId": job_id, "stage": Stage.REVIEW_CONCEPT, "attempt": 1,
             "assetId": None, "operation": "CORRECT_CONCEPT"}, None)
    cause = json.dumps({"errorMessage": exc.value.message})
    handler_stage.handler(
        {"jobId": job_id, "recordFailure": True,
         "failure": {"Error": "ReferenceAdherenceFailed", "Cause": cause}}, None)
    error = job_of(pipeline)["error"]
    assert error["stage"] == Stage.REVIEW_CONCEPT
    assert error["code"] == "REFERENCE_ADHERENCE_FAILED"
    assert error["retryable"] is False


# --- final review and result -------------------------------------------------


def test_the_final_review_receives_the_concept_review_as_evidence(pipeline):
    set_freedom(pipeline, 50)
    pipeline["concept_verdicts"].append(
        concept_verdict(mode="guided-adaptation")
    )
    run_pipeline(pipeline)
    call = [c for c in pipeline["calls"]["vision"] if c["schema"] == "review_v1"][0]
    assert "guided-adaptation" in call["prompt"]
    assert "designFreedom 50" in call["prompt"]
    assert "business name replaced" in call["prompt"]
    # The final review judges the approved concept, not the original reference.
    assert "source-layout similarity" in call["prompt"]
    assert call["images"] == 3


def test_result_and_qa_carry_the_effective_value_and_policy(pipeline):
    set_freedom(pipeline, 75)
    pipeline["concept_verdicts"].append(concept_verdict(mode="broad-inspiration"))
    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    assert result["designFreedom"] == 75
    assert result["policyVersion"] == "1.0"
    qa = result["qa"]
    assert qa["designFreedom"] == 75
    assert qa["policyVersion"] == "1.0"
    assert qa["conceptRounds"] == 0
    assert qa["conceptReview"]["passed"] is True
    assert qa["conceptReview"]["mode"] == "broad-inspiration"
    assert qa["conceptReview"]["documentedExceptions"] == ["business name replaced"]
    assert artifacts.get_json(pipeline["jobId"], qa["conceptReview"]["key"])


def test_a_default_job_reports_zero_in_the_result(pipeline):
    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    assert result["designFreedom"] == 0
    assert "reviewKey" not in result
    assert result["qaKey"] == job["artifacts"][Stage.REVIEW]["review"]


def test_concept_review_and_policy_are_retained_with_the_design(pipeline):
    run_pipeline(pipeline)
    job = job_of(pipeline)
    tags = pipeline["s3"].tags
    for key in (job["approvedConcept"]["reviewKey"],
                job["artifacts"][Stage.ANALYZE_REFERENCE]["policy"]):
        assert tags[key] == "lifecycle=retained", key


def test_a_drifting_render_fails_even_at_one_hundred(pipeline, monkeypatch):
    set_freedom(pipeline, 100)
    real = vision_provider.analyze

    def drifting(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "review_v1":
            return {"passed": False, "conceptDefect": False, "suggestions": [],
                    "blockingIssues": [{
                        "code": "DRIFT", "layerId": None, "assetId": None,
                        "message": "The render replaces the approved panel with a "
                                   "generic layout."}]}
        return real(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", drifting)
    with pytest.raises(ReviewBlocked):
        run_pipeline(pipeline)


# --- concept defect routing --------------------------------------------------


def _defect_review(monkeypatch, *, defect=True):
    real = vision_provider.analyze

    def reviewer(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "review_v1":
            return {"passed": False, "conceptDefect": defect, "suggestions": [],
                    "blockingIssues": [{
                        "code": "CONCEPT_VIOLATES_POLICY", "layerId": None,
                        "assetId": None, "message": "The concept itself is wrong."}]}
        return real(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", reviewer)


def _run_through_review(pipeline):
    job_id = pipeline["jobId"]
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.CONCEPT, Stage.REVIEW_CONCEPT, Stage.EXTRACT_SCENE,
                  Stage.CLEAN_PHOTO, Stage.DECORATION_OVERLAY,
                  Stage.VALIDATE_ASSETS, Stage.ASSEMBLE, Stage.RENDER):
        run_stage(job_id, stage)
    return run_stage(job_id, Stage.REVIEW)


def test_a_concept_defect_is_reported_while_a_round_remains(pipeline, monkeypatch):
    _defect_review(monkeypatch)
    out = _run_through_review(pipeline)
    assert out["approved"] is False
    assert out["conceptDefect"] is True


def test_a_concept_defect_falls_back_to_html_correction_when_rounds_are_spent(
        pipeline, monkeypatch):
    job_of(pipeline)["conceptRounds"] = 2
    _defect_review(monkeypatch)
    out = _run_through_review(pipeline)
    assert out["approved"] is False
    assert out["conceptDefect"] is False


def test_an_ordinary_blocking_issue_is_not_a_concept_defect(pipeline, monkeypatch):
    _defect_review(monkeypatch, defect=False)
    assert _run_through_review(pipeline)["conceptDefect"] is False


def test_an_approved_review_is_never_a_concept_defect(pipeline):
    assert _run_through_review(pipeline)["conceptDefect"] is False


# --- VALIDATE_ASSETS fidelity ------------------------------------------------


def test_displaced_layers_route_to_overlay_repair_not_scene_edits(
        pipeline, monkeypatch):
    from lib.errors import OverlayRepairNeeded

    real = vision_provider.analyze

    def displaced(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "composite_fidelity_v1":
            return {"displaced": True,
                    "description": "the ribbon is 200px lower than in the concept"}
        return real(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", displaced)
    job_id = pipeline["jobId"]
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.CONCEPT, Stage.REVIEW_CONCEPT, Stage.EXTRACT_SCENE,
                  Stage.CLEAN_PHOTO, Stage.DECORATION_OVERLAY):
        run_stage(job_id, stage)
    with pytest.raises(OverlayRepairNeeded):
        run_stage(job_id, Stage.VALIDATE_ASSETS)
    assert "ribbon" in job_of(pipeline)["overlayDefect"]


def test_the_fidelity_check_compares_the_concept_with_the_stacked_layers(pipeline):
    run_pipeline(pipeline)
    call = [c for c in pipeline["calls"]["vision"]
            if c["schema"] == "composite_fidelity_v1"][0]
    assert call["images"] == 2


def test_an_exhausted_html_correction_is_recorded_against_review(pipeline):
    from lib.errors import CorrectionExhausted

    job_id = pipeline["jobId"]
    # Drive REVIEW to an unapproved verdict, then exhaust the HTML rounds.
    pipeline["table"].items[f"JOB#{job_id}"]["correctionRounds"] = 2
    review_key = artifacts.artifact_key(job_id, Stage.REVIEW, 1, "review.json")
    artifacts.put_json(review_key, {"blockingIssues": [{"message": "bad"}]})
    pipeline["table"].items[f"JOB#{job_id}"]["artifacts"] = {
        Stage.REVIEW: {"review": review_key}}
    with pytest.raises(CorrectionExhausted) as exc:
        handler_stage.handler(
            {"jobId": job_id, "stage": Stage.REVIEW, "attempt": 1,
             "assetId": None, "operation": "CORRECT"}, None)
    assert handler_stage._stage_from_message(exc.value.message) == Stage.REVIEW
