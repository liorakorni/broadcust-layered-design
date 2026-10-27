"""Balanced review, candidate selection and regression-safe corrections.

Covers reviewStrictness and the versioned policy, the severity model, the
eligibility rules, candidate storage and selection, correction requests as the
image provider actually receives them, and the downstream guarantees (warnings in
the result, no success on missing checks, every stage on the selected concept).

The diagnostic case is job c419e88204e1423fbc761304a0333b54: attempt 1 and 3 lacked
the business name, attempt 2 had it with a different photograph and an added rule.
`tests/fixtures/c419e882` holds the real review model's verdicts for those three
attempts under policy 2.0; the tests below replay them.
"""

import base64
import copy
import io
import json
import pathlib
from types import SimpleNamespace

import pytest
from PIL import Image

import conf
import handler_stage
from lib import (
    artifacts, edit_mask, jobs, policy_context, prompts, reference_policy,
    review_policy, schemas,
)
from lib.errors import (
    AssetInvalid,
    FinalHashMismatch,
    InvalidModelJson,
    InvalidRequest,
    InvalidTransparency,
    PolicyVersionMismatch,
    ReferenceAdherenceFailed,
    RequiredCopyMissing,
    ReviewBlocked,
    Stage,
)
from lib.providers import image as image_provider, vision as vision_provider
from lib.schemas import canonical_request_hash, parse_start_request
from lib.stages import analyze_reference, concept as concept_stage, review as review_stage
from lib.stages import review_concept
from tests.test_design_freedom import (  # noqa: F401  (fixtures)
    api,
    direct_start,
    job_of,
)
from tests.test_pipeline import (  # noqa: F401  (fixtures)
    COPY_ITEMS,
    REFERENCE_ANALYSIS,
    REQUEST,
    concept_verdict,
    content_issue,
    copy_checks,
    layout_issue,
    pipeline,
    png_bytes,
    run_pipeline,
    run_stage,
    skip_external_probes,
)

#: Captured at import, before the pipeline fixture replaces it with a fake: the
#: correction tests drive the REAL adapter against a fake OpenAI client so they
#: can see the request the provider would get.
REAL_EDIT = image_provider.edit

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "c419e882"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


C419_COPY = fixture("copy.json")["items"]
C419_ANALYSIS = fixture("analysis.json")
C419_VERDICTS = {n: fixture(f"verdict-attempt{n}.json") for n in (1, 2, 3)}

POLICY = reference_policy.derive_reference_policy(0)


# --- helpers -----------------------------------------------------------------


def placements_for(items):
    return [
        {"contentKey": item["contentKey"], "basis": "planned",
         "box": {"x": 0.1, "y": round(0.05 + 0.1 * i, 3), "width": 0.5, "height": 0.05}}
        for i, item in enumerate(items)
    ]


def seed(pipeline, items=COPY_ITEMS, analysis=None, *, design_freedom=0,
         policy_version=None):
    """Write the copy, policy and reference analysis a concept stage reads.

    Used instead of running PREPARE_COPY and ANALYZE_REFERENCE when a test needs a
    particular manifest (the c419e882 one) rather than the fixture's default.
    """
    job_id = pipeline["jobId"]
    strictness = job_of(pipeline).get("reviewStrictness", "balanced")
    policy = reference_policy.derive_reference_policy(design_freedom, strictness)
    if policy_version:
        policy["policyVersion"] = policy_version
    if analysis is None:
        analysis = copy.deepcopy(REFERENCE_ANALYSIS)
        analysis["copyPlacements"] = placements_for(items)
    analysis = copy.deepcopy(analysis)
    analysis["policyVersion"] = policy["policyVersion"]

    copy_key = artifacts.artifact_key(job_id, Stage.PREPARE_COPY, 1, "copy.json")
    artifacts.put_json(copy_key, {"items": items})
    jobs.record_artifacts(job_id, Stage.PREPARE_COPY, 1, {"copy": copy_key})
    policy_key = artifacts.artifact_key(job_id, Stage.ANALYZE_REFERENCE, 1, "policy.json")
    analysis_key = artifacts.artifact_key(
        job_id, Stage.ANALYZE_REFERENCE, 1, "reference-analysis.json"
    )
    artifacts.put_json(policy_key, policy)
    artifacts.put_json(analysis_key, analysis)
    jobs.record_artifacts(
        job_id, Stage.ANALYZE_REFERENCE, 1,
        {"policy": policy_key, "analysis": analysis_key},
    )
    return policy, analysis


def varied(seed_value):
    """A distinct, decodable 1024px image per seed, so candidates differ in hash."""
    img = Image.new("RGB", (1024, 1024), ((seed_value * 53) % 256, 60, (200 - seed_value * 31) % 256))
    for x in range(0, 1024, 64):
        for y in range(0, 1024, 64):
            if (x // 64 + y // 64 + seed_value) % 3 == 0:
                img.paste((255, 255, 255), (x, y, x + 8, y + 8))
    return png_bytes(img)


@pytest.fixture
def distinct_concepts(pipeline, monkeypatch):
    """Concept generation that returns a different image for every attempt."""
    previous = image_provider.edit

    def edit(prompt, source, w, h, *, transparent=False, operation_id=None,
             quality="high", mime="image/png", mask=None):
        if operation_id and ":concept:" in operation_id:
            attempt = int(operation_id.rsplit(":", 1)[1])
            pipeline["calls"]["image"].append(
                {"op": "concept", "source": source, "mask": mask, "prompt": prompt,
                 "operationId": operation_id})
            return {"bytes": varied(attempt), "mimeType": "image/png",
                    "requestId": "req_c", "model": conf.IMAGE_MODEL}
        return previous(prompt, source, w, h, transparent=transparent,
                        operation_id=operation_id, quality=quality, mime=mime, mask=mask)

    monkeypatch.setattr(image_provider, "edit", edit)
    return pipeline


def review(pipeline, attempt, verdict):
    pipeline["concept_verdicts"].append(verdict)
    return run_stage(pipeline["jobId"], Stage.REVIEW_CONCEPT, attempt=attempt)


def correct(pipeline, attempt):
    return handler_stage.handler(
        {"jobId": pipeline["jobId"], "stage": Stage.REVIEW_CONCEPT, "attempt": attempt,
         "assetId": None, "operation": "CORRECT_CONCEPT"}, None)["artifacts"]


def select(pipeline, attempt):
    return handler_stage.handler(
        {"jobId": pipeline["jobId"], "stage": Stage.REVIEW_CONCEPT, "attempt": attempt,
         "assetId": None, "operation": "SELECT_CONCEPT"}, None)["artifacts"]


def concept_bytes(pipeline, attempt):
    key = artifacts.artifact_key(pipeline["jobId"], Stage.CONCEPT, attempt, "concept.png")
    return artifacts.get_bytes(pipeline["jobId"], key)


def c419(pipeline):
    """A job carrying the c419e882 copy manifest and reference analysis."""
    seed(pipeline, C419_COPY, C419_ANALYSIS)
    # The request is the fixture's, but its brief is irrelevant to review logic.
    return pipeline


class FakeImages:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def edit(self, **params):
        self.calls.append(params)
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(self.output).decode())],
            _request_id="req_fake",
        )


@pytest.fixture
def provider(monkeypatch):
    """The REAL image adapter wired to a fake OpenAI client that records requests.

    Call `install()` once the base candidate exists: from then on every concept
    edit goes through the real adapter, so `calls` holds exactly what the provider
    would have been sent. Its output is solid red everywhere, so anything the
    correction keeps from the base that is not red was put back by the pipeline,
    not by the "provider".
    """
    red = png_bytes(Image.new("RGB", (1024, 1024), (255, 0, 0)))
    images = FakeImages(red)

    def install():
        monkeypatch.setattr(image_provider, "edit", REAL_EDIT)
        monkeypatch.setattr(
            image_provider, "_openai", lambda: SimpleNamespace(images=images))

    images.install = install
    return images


def pixel(data, xy):
    with Image.open(io.BytesIO(data)) as img:
        return img.convert("RGB").getpixel(xy)


# --- 1. reviewStrictness ------------------------------------------------------


def test_review_strictness_defaults_to_balanced(start_payload):
    start_payload.pop("reviewStrictness", None)
    assert parse_start_request(start_payload).reviewStrictness == "balanced"


@pytest.mark.parametrize("value", ["strict", "lenient", "", None, 1, True, [], {}])
def test_unsupported_review_strictness_is_rejected(start_payload, value):
    start_payload["reviewStrictness"] = value
    with pytest.raises(InvalidRequest) as exc:
        parse_start_request(start_payload)
    assert "balanced" in exc.value.message


def test_omitted_and_explicit_balanced_hash_identically_and_like_before(start_payload):
    omitted = copy.deepcopy(start_payload)
    omitted.pop("reviewStrictness", None)
    explicit = copy.deepcopy(start_payload)
    explicit["reviewStrictness"] = "balanced"
    h_omitted = canonical_request_hash(parse_start_request(omitted))
    assert h_omitted == canonical_request_hash(parse_start_request(explicit))

    # A request accepted before the field existed must hash as it did, or replaying
    # it would read as an idempotency conflict.
    body = parse_start_request(omitted).model_dump(mode="json")
    body.pop("reviewStrictness")
    body.pop("designFlow")
    # Added later still: a request accepted before they existed carried none of
    # them, and their defaults mean what its absence meant.
    for later in ("layoutDirection", "textDirection", "referenceLayoutPolicy",
                  "templateFlexibility"):
        body.pop(later)
    import hashlib
    legacy = hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert h_omitted == legacy


def test_review_strictness_is_persisted_with_the_job(start_payload, api):
    start_payload["reviewStrictness"] = "balanced"
    job_id = direct_start(start_payload)["jobId"]
    job = api["table"].items[f"JOB#{job_id}"]
    assert job["reviewStrictness"] == "balanced"
    assert job["conceptCandidates"] == {}
    request = artifacts.get_json(job_id, job["requestKey"])
    assert request["reviewStrictness"] == "balanced"


def test_an_omitted_review_strictness_is_persisted_as_balanced(start_payload, api):
    start_payload.pop("reviewStrictness", None)
    job_id = direct_start(start_payload)["jobId"]
    assert api["table"].items[f"JOB#{job_id}"]["reviewStrictness"] == "balanced"


def test_generation_freedom_and_review_strictness_are_independent():
    for freedom in (0, 30, 100):
        policy = reference_policy.derive_reference_policy(freedom, "balanced")
        assert policy["designFreedom"] == freedom
        assert policy["reviewStrictness"] == "balanced"
        assert policy["mode"] == reference_policy.mode_for(freedom)


def test_the_policy_carries_the_versioned_review_policy():
    policy = reference_policy.derive_reference_policy(0)
    assert policy["policyVersion"] == reference_policy.POLICY_VERSION
    review = policy["reviewPolicy"]
    assert review["reviewStrictness"] == "balanced"
    assert set(review["severityRules"]) == {
        "warning", "blockingVisual", "blockingContent", "blockingTechnical"}
    assert review["requiredConceptChecks"] == [
        "mandatoryCopy", "referenceStructure", "compositionDirection"]


def test_analyze_reference_stores_the_strictness_and_version(pipeline):
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY):
        run_stage(pipeline["jobId"], stage)
    out = run_stage(pipeline["jobId"], Stage.ANALYZE_REFERENCE)
    policy = artifacts.get_json(pipeline["jobId"], out["policy"])
    analysis = artifacts.get_json(pipeline["jobId"], out["analysis"])
    assert policy["reviewStrictness"] == "balanced"
    assert policy["policyVersion"] == analysis["policyVersion"] == reference_policy.POLICY_VERSION


def test_there_is_no_fixed_overlap_threshold_anywhere():
    text = json.dumps(POLICY).lower() + prompts.load("review-concept").lower()
    assert "90%" not in text and "overlap threshold" not in text
    assert "similarity percentage" in prompts.load("review-concept").lower()  # "Do NOT report..."


# --- 3. reference structure is separate from content --------------------------

_CONTENT_WORDS = ("athlete", "barbell", "dumbbell", "weight", " man ", "woman", "gym")


@pytest.mark.parametrize("value", [0, 10, 40, 60, 100])
def test_the_policy_never_requires_a_subject_pose_or_object(value):
    policy = reference_policy.derive_reference_policy(value)
    text = json.dumps(policy).lower()
    for word in _CONTENT_WORDS:
        assert word not in text, word
    boundary = " ".join(policy["contentBoundary"]).lower()
    assert "brief decides subject matter" in boundary
    assert "pose" in boundary and "object category" in boundary


def test_one_policy_text_feeds_generation_review_and_correction(pipeline, distinct_concepts):
    policy, analysis = seed(pipeline)
    block = concept_stage.policy_block(policy, analysis)
    # The same rules appear in the generation/correction block ...
    assert review_policy.severity_block(policy["reviewPolicy"]) in block
    assert "Reference boundary" in block
    # ... and the review prompt is rendered from the same severity text.
    review_policy_text = review_policy.severity_block(policy["reviewPolicy"])
    assert "{severity_block}" in prompts.load("review-concept")
    assert "{severity_block}" in prompts.load("review")
    assert "BLOCKING CONTENT" in review_policy_text


def test_the_analysis_prompt_demands_content_neutral_anchors():
    text = prompts.load("analyze-reference").lower()
    assert "content-neutral" in text
    assert "never by what it depicts" in text


def test_the_review_prompt_does_not_require_what_the_reference_shows():
    text = " ".join(prompts.load("review-concept").lower().split())
    assert "do not require any person, pose, object" in text
    assert "exact subject bounds" in text


def test_an_old_analysis_is_not_silently_reused(pipeline, distinct_concepts):
    seed(pipeline, policy_version="1.0")
    with pytest.raises(PolicyVersionMismatch) as exc:
        run_stage(pipeline["jobId"], Stage.CONCEPT)
    assert exc.value.code == "POLICY_VERSION_MISMATCH"
    assert "'1.0'" in exc.value.message and f"'{reference_policy.POLICY_VERSION}'" in exc.value.message


def test_an_analysis_stamped_with_another_version_is_refused(pipeline, distinct_concepts):
    seed(pipeline)
    job = job_of(pipeline)
    key = job["artifacts"][Stage.ANALYZE_REFERENCE]["analysis"]
    analysis = artifacts.get_json(pipeline["jobId"], key)
    analysis["policyVersion"] = "1.0"
    artifacts.put_json(key, analysis, overwrite=True)
    with pytest.raises(PolicyVersionMismatch):
        policy_context.load(pipeline["jobId"], job_of(pipeline))


def test_copy_placements_must_cover_every_approved_item():
    analysis = {"copyPlacements": placements_for(COPY_ITEMS[:2])}
    with pytest.raises(InvalidModelJson) as exc:
        analyze_reference.check_placements(analysis, COPY_ITEMS)
    assert "contact.phone" in exc.value.message


def test_copy_placements_reject_unknown_duplicate_and_offcanvas_items():
    base = placements_for(COPY_ITEMS)
    with pytest.raises(InvalidModelJson):
        analyze_reference.check_placements(
            {"copyPlacements": base + [dict(base[0], contentKey="invented.key")]}, COPY_ITEMS)
    with pytest.raises(InvalidModelJson):
        analyze_reference.check_placements(
            {"copyPlacements": base + [dict(base[0])]}, COPY_ITEMS)
    off = copy.deepcopy(base)
    off[0]["box"]["x"] = 0.9
    with pytest.raises(InvalidModelJson):
        analyze_reference.check_placements({"copyPlacements": off}, COPY_ITEMS)


def test_the_real_reference_analysis_keeps_the_business_name_in_a_fixed_slot():
    slots = {p["contentKey"]: p["box"] for p in C419_ANALYSIS["copyPlacements"]}
    assert set(slots) == {i["contentKey"] for i in C419_COPY}
    assert slots["business.name"]["y"] < slots["title.primary"]["y"]
    anchor_text = json.dumps(C419_ANALYSIS["anchors"]).lower()
    for word in _CONTENT_WORDS:
        assert word not in anchor_text, word


# --- 2. severity policy -------------------------------------------------------


def evaluate(verdict, items=COPY_ITEMS, policy=POLICY, placements=None):
    return review_policy.evaluate_concept(verdict, policy, items, placements or {}, ["hero"])


@pytest.mark.parametrize("what", [
    "A small shift in position.",
    "A minor decorative variation.",
    "An extra subtle rule above the business name.",
    "A different photograph that keeps the visual balance.",
    "A localized Hebrew typography adjustment.",
])
def test_minor_differences_alone_do_not_block(what):
    warning = layout_issue(severity="warning", code="MINOR", impact=what)
    result = evaluate(concept_verdict(issues=[warning]))
    assert result["eligible"] is True
    assert result["blockingIssues"] == []
    assert [w["code"] for w in result["warnings"]] == ["MINOR"]


def test_a_material_visual_problem_blocks():
    result = evaluate(concept_verdict(issues=[layout_issue()]))
    assert result["eligible"] is False
    assert result["blockingIssues"][0]["category"] == "visual"


def test_a_blocking_visual_claim_without_an_impact_is_only_a_warning():
    unexplained = layout_issue(impact="  ")
    result = evaluate(concept_verdict(issues=[unexplained]))
    assert result["eligible"] is True
    warning = result["warnings"][0]
    assert warning["severity"] == "warning"
    assert "impact" in warning["severityAdjustment"]


def test_a_content_issue_cannot_be_argued_down_to_a_warning():
    soft = dict(content_issue("title.primary", code="WRONG_TITLE"), severity="warning")
    result = evaluate(concept_verdict(issues=[soft]))
    assert result["eligible"] is False
    assert result["blockingIssues"][0]["severity"] == "blocking"


def test_a_technical_issue_is_always_blocking():
    technical = {"code": "BROKEN_ASSET", "category": "technical", "severity": "warning",
                 "affected": "photo", "impact": "The photo is missing.",
                 "suggestion": None, "region": None}
    assert evaluate(concept_verdict(issues=[technical]))["eligible"] is False


def test_every_issue_carries_the_required_fields():
    verdict = concept_verdict(issues=[layout_issue(), layout_issue(severity="warning", code="W")],
                              missing_copy=("contact.phone",))
    for issue in evaluate(verdict)["issues"]:
        assert set(issue) >= {"category", "code", "severity", "affected", "impact",
                              "suggestion", "region", "message"}
        assert issue["affected"] and issue["impact"]
        assert issue["category"] in ("visual", "content", "technical")


def test_a_missing_approved_text_blocks_even_when_the_model_reports_no_issue():
    result = evaluate(concept_verdict(missing_copy=("contact.phone",)))
    assert result["eligible"] is False
    synthesized = result["blockingIssues"][0]
    assert synthesized["code"] == "MISSING_REQUIRED_COPY"
    assert synthesized["category"] == "content"
    assert synthesized["affected"] == "contact.phone"


def test_wrong_or_unreadable_approved_text_blocks():
    wrong = evaluate(concept_verdict(copy=copy_checks(wrong=("title.primary",))))
    unreadable = evaluate(concept_verdict(copy=copy_checks(unreadable=("contact.phone",))))
    assert [i["code"] for i in wrong["blockingIssues"]] == ["INCORRECT_REQUIRED_COPY"]
    assert [i["code"] for i in unreadable["blockingIssues"]] == ["UNREADABLE_REQUIRED_COPY"]


def test_visual_similarity_cannot_outvote_missing_content():
    """A near-perfect structural match with one missing line is still ineligible."""
    result = evaluate(concept_verdict(missing_copy=("title.primary",)))
    assert result["structuralFailures"] == 0
    assert result["eligible"] is False


def test_a_required_check_that_did_not_run_never_counts_as_passing():
    skipped = concept_verdict(copy=copy_checks()[:-1])  # one item never reported on
    result = evaluate(skipped)
    statuses = {c["name"]: c["status"] for c in result["requiredChecks"]}
    assert statuses["mandatoryCopy"] == "missing"
    assert result["eligible"] is False

    no_structure = evaluate(concept_verdict(checks=[]))
    statuses = {c["name"]: c["status"] for c in no_structure["requiredChecks"]}
    assert statuses["referenceStructure"] == "missing"
    assert no_structure["eligible"] is False


def test_the_review_stage_refuses_an_incomplete_review(pipeline):
    seed(pipeline)
    run_stage(pipeline["jobId"], Stage.CONCEPT)
    pipeline["concept_verdicts"].append(concept_verdict(copy=copy_checks()[:-1]))
    with pytest.raises(InvalidModelJson) as exc:
        run_stage(pipeline["jobId"], Stage.REVIEW_CONCEPT)
    assert "mandatoryCopy" in exc.value.message
    assert "approvedConcept" not in job_of(pipeline)


# --- 7. the diagnostic case: job c419e882, real verdicts ----------------------


def decide(attempt):
    return review_concept.build_review(
        C419_VERDICTS[attempt], POLICY, C419_COPY,
        {p["contentKey"]: p["box"] for p in C419_ANALYSIS["copyPlacements"]},
        C419_ANALYSIS,
    )


def test_c419_attempt_1_without_the_business_name_stays_blocking():
    review = decide(1)
    assert review["eligible"] is False
    assert [i["affected"] for i in review["blockingIssues"]] == ["business.name"]
    assert review["blockingIssues"][0]["category"] == "content"


def test_c419_attempt_3_without_the_business_name_stays_blocking():
    review = decide(3)
    assert review["eligible"] is False
    assert [i["affected"] for i in review["blockingIssues"]] == ["business.name"]


def test_c419_attempt_2_is_judged_on_evidence_not_assumed_to_pass():
    """Photo changed and a rule added: the real model found no material harm."""
    review = decide(2)
    assert all(c["status"] == "passed" for c in review["copyChecks"])
    assert review["blockingIssues"] == []
    assert review["eligible"] is True
    codes = {w["code"] for w in review["warnings"]}
    assert codes, "the photo and divider differences are still reported"
    text = json.dumps(review["warnings"]).lower()
    assert "photo" in text or "colo" in text      # the changed photograph
    assert "rule" in text or "divider" in text    # the added line


def test_the_same_attempt_2_verdict_blocks_if_the_business_name_were_missing():
    verdict = copy.deepcopy(C419_VERDICTS[2])
    for entry in verdict["copyChecks"]:
        if entry["contentKey"] == "business.name":
            entry["present"] = False
    review = review_concept.build_review(
        verdict, POLICY, C419_COPY, {}, C419_ANALYSIS)
    assert review["eligible"] is False


def test_a_subtle_extra_divider_alone_is_a_warning():
    divider = layout_issue(severity="warning", code="UNNECESSARY_DIVIDER",
                           impact="A short yellow rule sits above the business name.")
    divider["affected"] = "business.name"
    result = evaluate(concept_verdict(issues=[divider]))
    assert result["eligible"] is True
    assert result["warnings"][0]["code"] == "UNNECESSARY_DIVIDER"


def test_a_major_composition_problem_still_blocks():
    major = layout_issue(code="TEXT_COVERED_BY_IMAGE",
                         impact="The photograph covers the headline, which can no longer be read.")
    assert evaluate(concept_verdict(issues=[major]))["eligible"] is False


# --- 5. candidates and selection ---------------------------------------------


def candidate(attempt, *, blocking=0, structural=0, warnings=0, eligible=None):
    return {
        "attempt": attempt,
        "eligible": (blocking == 0) if eligible is None else eligible,
        "blockingIssues": [{"code": f"B{i}"} for i in range(blocking)],
        "warnings": [{"code": f"W{i}"} for i in range(warnings)],
        "structuralFailures": structural,
    }


def test_selection_prefers_structure_then_fewer_warnings_then_the_earlier_attempt():
    candidates = [candidate(1, structural=1), candidate(2, warnings=3),
                  candidate(3, warnings=1), candidate(4, warnings=1)]
    assert review_policy.select_best(candidates)["attempt"] == 3
    assert review_policy.select_best([candidate(2), candidate(5)])["attempt"] == 2


def test_selection_never_returns_an_ineligible_candidate():
    flawless_but_missing_copy = candidate(3, blocking=1, eligible=False)
    assert review_policy.select_best([flawless_but_missing_copy]) is None
    chosen = review_policy.select_best([flawless_but_missing_copy, candidate(1, warnings=5)])
    assert chosen["attempt"] == 1


def test_the_correction_base_is_the_candidate_closest_to_eligible_not_the_latest():
    candidates = [candidate(1, blocking=1), candidate(2, blocking=3), candidate(3, blocking=2)]
    assert review_policy.best_correction_base(candidates)["attempt"] == 1


def test_each_attempt_is_stored_as_its_own_candidate(distinct_concepts):
    pipeline = distinct_concepts
    seed(pipeline)
    run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=1)
    review(pipeline, 1, concept_verdict(missing_copy=("contact.phone",)))
    correct(pipeline, 1)
    run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=2)
    review(pipeline, 2, concept_verdict(missing_copy=("title.primary",)))

    records = jobs.concept_candidates(job_of(pipeline))
    assert [r["attempt"] for r in records] == [1, 2]
    assert records[0]["conceptSha256"] != records[1]["conceptSha256"]
    assert records[0]["reviewKey"] != records[1]["reviewKey"]
    assert all(r["eligible"] is False for r in records)
    # Both images and both reviews remain readable.
    for r in records:
        assert artifacts.get_bytes(pipeline["jobId"], r["conceptKey"])
        assert artifacts.get_json(pipeline["jobId"], r["reviewKey"])["eligible"] is False


def test_correction_stops_as_soon_as_a_candidate_is_eligible(pipeline):
    seed(pipeline)
    run_stage(pipeline["jobId"], Stage.CONCEPT)
    out = review(pipeline, 1, concept_verdict(
        issues=[layout_issue(severity="warning", code="MINOR", impact="Small shift.")]))
    assert out["approved"] is True
    assert out["warnings"][0]["code"] == "MINOR"
    assert job_of(pipeline)["conceptRounds"] == 0   # no round consumed for a warning
    frozen = job_of(pipeline)["approvedConcept"]
    assert frozen["selectedBy"] == "first-eligible"


def test_the_selection_is_persisted_with_key_hash_attempt_policy_and_reason(pipeline):
    seed(pipeline)
    run_stage(pipeline["jobId"], Stage.CONCEPT)
    review(pipeline, 1, concept_verdict())
    frozen = job_of(pipeline)["approvedConcept"]
    assert frozen["key"] == job_of(pipeline)["artifacts"][Stage.CONCEPT]["concept"]
    assert frozen["sha256"] == artifacts.sha256_hex(concept_bytes(pipeline, 1))
    assert frozen["attempt"] == 1
    assert frozen["policyVersion"] == reference_policy.POLICY_VERSION
    assert frozen["reviewStrictness"] == "balanced"
    assert "eligible" in frozen["selectionReason"]
    record = artifacts.get_json(pipeline["jobId"], frozen["selectionKey"])
    assert record["selectedAttempt"] == 1
    assert record["policyVersion"] == reference_policy.POLICY_VERSION
    assert record["candidates"][0]["eligible"] is True


def exhaust(pipeline, verdicts):
    """Drive the concept loop the way the state machine does, to exhaustion."""
    job_id = pipeline["jobId"]
    attempt = 1
    for verdict in verdicts:
        run_stage(job_id, Stage.CONCEPT, attempt=attempt)
        out = review(pipeline, attempt, verdict)
        if out["approved"] or not out["correctionAvailable"]:
            return out, attempt
        correct(pipeline, attempt)
        attempt += 1
    raise AssertionError("loop did not end")


def test_the_loop_reaches_select_concept_when_no_round_is_left(distinct_concepts):
    pipeline = distinct_concepts
    seed(pipeline)
    out, attempt = exhaust(pipeline, [concept_verdict(missing_copy=("contact.phone",))] * 3)
    assert attempt == 3
    assert out["approved"] is False and out["correctionAvailable"] is False
    assert job_of(pipeline)["conceptRounds"] == conf.MAX_CONCEPT_ROUNDS


def test_no_eligible_candidate_fails_precisely_on_content_and_selects_nothing(
        distinct_concepts):
    pipeline = distinct_concepts
    seed(pipeline)
    exhaust(pipeline, [concept_verdict(missing_copy=("contact.phone",))] * 3)
    with pytest.raises(RequiredCopyMissing) as exc:
        select(pipeline, 3)
    error = exc.value
    assert (error.code, error.category) == ("REQUIRED_COPY_MISSING", "content")
    assert "contact.phone" in error.message
    assert error.retryable is False
    # The latest attempt is never auto-selected.
    assert "approvedConcept" not in job_of(pipeline)
    record = artifacts.get_json(
        pipeline["jobId"],
        artifacts.artifact_key(pipeline["jobId"], Stage.REVIEW_CONCEPT, 3, "concept-selection.json"))
    assert record["selectedAttempt"] is None


def test_a_material_structural_failure_is_a_reference_adherence_failure(distinct_concepts):
    pipeline = distinct_concepts
    seed(pipeline)
    exhaust(pipeline, [concept_verdict(issues=[layout_issue()])] * 3)
    with pytest.raises(ReferenceAdherenceFailed) as exc:
        select(pipeline, 3)
    assert exc.value.code == "REFERENCE_ADHERENCE_FAILED"
    assert exc.value.category == "reference-adherence"
    assert "approvedConcept" not in job_of(pipeline)


def test_an_earlier_eligible_candidate_is_selected_over_a_later_invalid_one(
        distinct_concepts):
    """Candidate 1 was reviewed under another policy version, so it is re-reviewed
    under the current one at exhaustion; it turns out eligible, and the later
    candidates are not."""
    pipeline = distinct_concepts
    seed(pipeline)
    exhaust(pipeline, [concept_verdict(missing_copy=("contact.phone",))] * 3)
    original_key = jobs.concept_candidates(job_of(pipeline))[0]["reviewKey"]

    # Make candidate 1's stored review incompatible with the current policy.
    stale = artifacts.get_json(pipeline["jobId"], original_key)
    stale["policyVersion"] = "1.0"
    artifacts.put_json(original_key, stale, overwrite=True)

    pipeline["concept_verdicts"].append(concept_verdict())  # the re-review of #1
    out = select(pipeline, 3)

    assert out["selectedAttempt"] == 1
    frozen = job_of(pipeline)["approvedConcept"]
    assert frozen["attempt"] == 1
    assert frozen["selectedBy"] == "best-of-candidates"
    assert frozen["sha256"] == artifacts.sha256_hex(concept_bytes(pipeline, 1))
    assert "Attempt 1" in frozen["selectionReason"]

    # The reevaluation sits beside the historical report; neither replaced the other.
    record = jobs.concept_candidates(job_of(pipeline))[0]
    assert record["historicalReviewKeys"] == [original_key]
    assert record["reviewKey"] != original_key
    assert record["reviewKey"].endswith(
        f"concept-review-reevaluation-{reference_policy.POLICY_VERSION}-balanced.json")
    assert artifacts.get_json(pipeline["jobId"], original_key)["policyVersion"] == "1.0"
    assert artifacts.get_json(pipeline["jobId"], record["reviewKey"])["eligible"] is True


def test_stored_verdicts_are_redecided_under_the_current_rules_without_a_model_call(
        distinct_concepts):
    pipeline = distinct_concepts
    seed(pipeline)
    exhaust(pipeline, [concept_verdict(missing_copy=("contact.phone",))] * 3)
    pipeline["calls"]["vision"].clear()
    with pytest.raises(RequiredCopyMissing):
        select(pipeline, 3)
    assert [c for c in pipeline["calls"]["vision"] if c["schema"] == "concept_review_v2"] == []


def test_a_candidate_the_final_review_rejected_is_not_selected_again(distinct_concepts):
    pipeline = distinct_concepts
    seed(pipeline)
    run_stage(pipeline["jobId"], Stage.CONCEPT)
    review(pipeline, 1, concept_verdict())          # approved at attempt 1
    record = dict(jobs.concept_candidates(job_of(pipeline))[0])
    record["rejectedByFinalReview"] = True
    jobs.record_concept_candidate(pipeline["jobId"], 1, record)
    job = job_of(pipeline)
    policy, analysis, items, placements = policy_context.load(pipeline["jobId"], job)
    evaluations = review_concept._evaluations(
        pipeline["jobId"], job, policy, analysis, items, placements)
    assert evaluations[0]["eligible"] is False


def test_downstream_stages_use_exactly_the_selected_candidate(distinct_concepts):
    """Candidate 1 is selected although attempts 2 and 3 exist and are later."""
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    exhaust(pipeline, [concept_verdict(missing_copy=("contact.phone",))] * 3)
    original_key = jobs.concept_candidates(job_of(pipeline))[0]["reviewKey"]
    stale = artifacts.get_json(job_id, original_key)
    stale["policyVersion"] = "1.0"
    artifacts.put_json(original_key, stale, overwrite=True)
    pipeline["concept_verdicts"].append(concept_verdict())
    select(pipeline, 3)

    assert job_of(pipeline)["artifacts"][Stage.CONCEPT]["concept"].endswith("/3/concept.png")
    selected = concept_bytes(pipeline, 1)
    pipeline["calls"]["image"].clear()
    attempt = 4
    for stage in (Stage.EXTRACT_SCENE, Stage.DECORATION_OVERLAY, Stage.CLEAN_PHOTO,
                  Stage.VALIDATE_ASSETS, Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW):
        run_stage(job_id, stage, attempt=attempt)
    sources = [c["source"] for c in pipeline["calls"]["image"]]
    assert sources and all(s == selected for s in sources)

    produced = job_of(pipeline)["artifacts"]
    sha = artifacts.sha256_hex(selected)
    for stage in (Stage.EXTRACT_SCENE, Stage.DECORATION_OVERLAY, Stage.CLEAN_PHOTO,
                  Stage.VALIDATE_ASSETS):
        assert produced[stage]["conceptSha256"] == sha, stage
    review_doc = artifacts.get_json(job_id, produced[Stage.REVIEW]["review"])
    assert review_doc["conceptSha256"] == sha
    # And the whole pipeline can still finish on it.
    run_stage(job_id, Stage.FINALIZE, attempt=attempt)
    assert job_of(pipeline)["status"] == "SUCCEEDED"


# --- 4. corrections, as the provider receives them ---------------------------


def test_a_missing_business_name_is_added_by_editing_the_chosen_candidate(
        distinct_concepts, provider):
    """Text-only correction: the base is the candidate's pixels, the mask is the
    planned business-name slot, and nothing else may change."""
    pipeline = distinct_concepts
    c419(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    review(pipeline, 1, copy.deepcopy(C419_VERDICTS[1]))
    plan = artifacts.get_json(job_id, correct(pipeline, 1)["plan"])
    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=2)

    assert len(provider.calls) == 1
    request = provider.calls[0]
    base = concept_bytes(pipeline, 1)

    # The edit's image is the stored candidate, not the reference image.
    name, sent, mime = request["image"]
    assert sent == base
    assert "mask" in request

    # The mask opens only the planned business-name slot.
    slot = {p["contentKey"]: p["box"] for p in C419_ANALYSIS["copyPlacements"]}["business.name"]
    mask = request["mask"][1]
    with Image.open(io.BytesIO(mask)) as m:
        alpha = m.convert("RGBA").getchannel("A")
    cx = int((slot["x"] + slot["width"] / 2) * 1024)
    cy = int((slot["y"] + slot["height"] / 2) * 1024)
    assert alpha.getpixel((cx, cy)) == 0            # repaint inside the slot
    assert alpha.getpixel((900, 900)) == 255        # locked elsewhere
    assert alpha.getpixel((cx, cy + 300)) == 255
    assert plan["wholeImage"] is False
    assert plan["base"]["attempt"] == 1

    # The prompt carries the complete mandatory copy list ...
    prompt = request["prompt"]
    for item in C419_COPY:
        assert item["text"] in prompt, item["contentKey"]
    # ... only the blocking problem, its planned slot, and the preserve rules.
    assert "MISSING_BUSINESS_NAME" in prompt
    assert "PHOTO_COLOUR_VARIATION" not in prompt       # a warning is not a task
    assert f'business.name: x={slot["x"]:.3f}' in prompt
    assert "PRESERVE EVERYTHING ELSE" in prompt
    assert "Warnings are not problems to fix" in prompt

    # The "provider" returned solid red everywhere. Outside the mask the result is
    # still the base, byte for byte; inside it takes the provider's pixels.
    result = concept_bytes(pipeline, 2)
    for xy in ((900, 900), (50, 500), (cx, cy + 300), (700, 100)):
        assert pixel(result, xy) == pixel(base, xy), xy
    assert pixel(result, (cx, cy)) == (255, 0, 0)
    correction = artifacts.get_json(
        job_id, job_of(pipeline)["artifacts"][Stage.CONCEPT]["correction"])
    assert correction["outsideMaskMeanAbsDiff"] == 0.0
    assert correction["providerOutsideMaskMeanAbsDiff"] > 0       # the provider did drift
    assert correction["baseAttempt"] == 1
    assert correction["issueCodes"] == ["MISSING_BUSINESS_NAME"]


def test_a_correction_never_starts_from_the_reference_image(distinct_concepts, provider):
    pipeline = distinct_concepts
    c419(pipeline)
    run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=1)
    review(pipeline, 1, copy.deepcopy(C419_VERDICTS[1]))
    correct(pipeline, 1)
    provider.install()
    run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=2)
    from tests.test_pipeline import fake_photo
    assert provider.calls[0]["image"][1] != fake_photo()   # the fixture's reference bytes


def test_a_later_attempt_without_a_plan_is_refused_rather_than_regenerated(
        distinct_concepts, provider):
    pipeline = distinct_concepts
    seed(pipeline)
    provider.install()
    with pytest.raises(AssetInvalid) as exc:
        run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=2)
    assert "no correction plan" in exc.value.message
    assert provider.calls == []


def test_a_visual_correction_repaints_only_its_region_and_keeps_every_text(
        distinct_concepts, provider):
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    region = {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.5}
    blocker = layout_issue(
        code="IMAGE_COVERS_HEADLINE", region=region,
        impact="The photograph spills over the headline and hides it.",
        message="Pull the photograph back to the right column.")
    review(pipeline, 1, concept_verdict(issues=[blocker]))
    plan = artifacts.get_json(job_id, correct(pipeline, 1)["plan"])
    assert plan["regions"] == [region] and plan["wholeImage"] is False
    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=2)

    request = provider.calls[0]
    prompt = request["prompt"]
    for item in COPY_ITEMS:                                   # complete mandatory list
        assert item["text"] in prompt and item["contentKey"] in prompt
    assert "IMAGE_COVERS_HEADLINE" in prompt
    with Image.open(io.BytesIO(request["mask"][1])) as m:
        alpha = m.convert("RGBA").getchannel("A")
    assert alpha.getpixel((int(0.75 * 1024), int(0.3 * 1024))) == 0     # inside region
    assert alpha.getpixel((int(0.2 * 1024), int(0.2 * 1024))) == 255    # headline column locked

    base, result = concept_bytes(pipeline, 1), concept_bytes(pipeline, 2)
    assert pixel(result, (int(0.2 * 1024), int(0.2 * 1024))) == pixel(base, (int(0.2 * 1024), int(0.2 * 1024)))


def test_a_correction_that_drops_text_is_caught_by_the_recheck(distinct_concepts, provider):
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    region = {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.5}
    review(pipeline, 1, concept_verdict(issues=[layout_issue(region=region)]))
    correct(pipeline, 1)
    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=2)
    # The corrected image lost the phone number that attempt 1 had.
    out = review(pipeline, 2, concept_verdict(missing_copy=("contact.phone",)))
    assert out["approved"] is False
    stored = artifacts.get_json(job_id, out["review"])
    assert {"kind": "copy", "ref": "contact.phone"} in stored["regressions"]
    assert stored["baseAttempt"] == 1
    assert [i["affected"] for i in stored["blockingIssues"]] == ["contact.phone"]


def test_a_previously_passing_area_that_fails_after_correction_is_recorded(
        distinct_concepts, provider):
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    region = {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.5}
    review(pipeline, 1, concept_verdict(issues=[layout_issue(region=region)]))
    correct(pipeline, 1)
    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=2)
    worse = concept_verdict()
    worse["checks"][2]["passed"] = False          # "panel" passed on the base
    out = review(pipeline, 2, worse)
    stored = artifacts.get_json(job_id, out["review"])
    assert {"kind": "structure", "ref": "panel"} in stored["regressions"]
    assert "REGRESSION_AFTER_CORRECTION" in [w["code"] for w in stored["warnings"]]


def test_an_issue_without_a_region_falls_back_to_a_whole_image_edit_with_no_mask(
        distinct_concepts, provider):
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    review(pipeline, 1, concept_verdict(issues=[layout_issue(region=None)]))
    plan = artifacts.get_json(job_id, correct(pipeline, 1)["plan"])
    assert plan["wholeImage"] is True and plan["regions"] == []
    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=2)
    request = provider.calls[0]
    assert "mask" not in request
    assert request["image"][1] == concept_bytes(pipeline, 1)    # still the candidate
    assert "no mask is applied" in request["prompt"]


def test_warnings_never_enter_the_correction_plan(distinct_concepts, provider):
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    region = {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.5}
    verdict = concept_verdict(issues=[
        layout_issue(region=region),
        layout_issue(severity="warning", code="PHOTO_TINT", impact="Warmer photograph."),
    ])
    review(pipeline, 1, verdict)
    plan = artifacts.get_json(job_id, correct(pipeline, 1)["plan"])
    assert [i["code"] for i in plan["issues"]] == ["ANCHOR_DISPLACED"]


def test_the_correction_base_is_the_best_stored_candidate_not_the_latest(
        distinct_concepts, provider):
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    region = {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.5}
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    review(pipeline, 1, concept_verdict(issues=[layout_issue(region=region)]))   # 1 blocking
    correct(pipeline, 1)
    run_stage(job_id, Stage.CONCEPT, attempt=2)
    review(pipeline, 2, concept_verdict(issues=[
        layout_issue(region=region), layout_issue(region=region, code="SECOND")]))  # 2 blocking
    plan = artifacts.get_json(job_id, correct(pipeline, 2)["plan"])
    assert plan["base"]["attempt"] == 1
    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=3)
    assert provider.calls[-1]["image"][1] == concept_bytes(pipeline, 1)


def test_the_retry_limits_were_not_raised():
    assert conf.MAX_CONCEPT_ROUNDS == 2
    assert conf.MAX_CORRECTION_ROUNDS == 2
    assert conf.MAX_OVERLAY_REPAIRS == 2


def test_the_c419_replay_ends_on_the_corrected_candidate(distinct_concepts, provider):
    """Attempt 1 lacks FIT HOME -> targeted correction -> attempt 2 (all copy, only
    warnings) is eligible -> correction stops. Attempt 3 is never generated."""
    pipeline = distinct_concepts
    c419(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    first = review(pipeline, 1, copy.deepcopy(C419_VERDICTS[1]))
    assert first["approved"] is False and first["correctionAvailable"] is True
    correct(pipeline, 1)
    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=2)
    second = review(pipeline, 2, copy.deepcopy(C419_VERDICTS[2]))
    assert second["approved"] is True
    assert [w["code"] for w in second["warnings"]]
    frozen = job_of(pipeline)["approvedConcept"]
    assert frozen["attempt"] == 2 and frozen["selectedBy"] == "first-eligible"
    assert job_of(pipeline)["conceptRounds"] == 1


# --- 6. downstream quality is preserved --------------------------------------


def test_concept_acceptance_is_not_job_success(pipeline):
    run_stage(pipeline["jobId"], Stage.PREFLIGHT)
    for stage in (Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE, Stage.CONCEPT,
                  Stage.REVIEW_CONCEPT):
        run_stage(pipeline["jobId"], stage)
    assert "approvedConcept" in job_of(pipeline)
    assert job_of(pipeline)["status"] != "SUCCEEDED"


def test_warnings_ride_along_with_a_successful_result(pipeline, monkeypatch):
    pipeline["concept_verdicts"].append(concept_verdict(issues=[
        layout_issue(severity="warning", code="PHOTO_TINT", impact="Warmer photograph.")]))
    real = vision_provider.analyze

    def reviewer(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "review_v2":
            return {"passed": True, "conceptDefect": False, "suggestions": [],
                    "blockingIssues": [],
                    "warnings": [{
                        "code": "ICON_MARGIN", "layerId": None, "assetId": None,
                        "message": "Slightly tight margin.", "category": "visual",
                        "severity": "warning", "affected": "t_phone",
                        "impact": "The phone row sits a little close to the edge.",
                        "suggestion": None, "region": None}]}
        return real(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", reviewer)
    job = run_pipeline(pipeline)
    assert job["status"] == "SUCCEEDED"
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    codes = {(w["stage"], w["code"]) for w in result["warnings"]}
    assert codes == {("REVIEW_CONCEPT", "PHOTO_TINT"), ("REVIEW", "ICON_MARGIN")}
    assert result["qa"]["warnings"] == result["warnings"]
    # Backward compatible: nothing that was there is gone.
    assert result["qa"]["blockingIssues"] == []
    assert result["reviewStrictness"] == "balanced"
    assert result["conceptSelection"]["attempt"] == 1
    assert {"html", "htmlHash", "assets", "qa", "designFreedom"} <= set(result)


def test_a_result_without_warnings_still_has_the_field_empty(pipeline):
    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    assert result["warnings"] == []


def _final_review(monkeypatch, issues, *, passed=False):
    real = vision_provider.analyze

    def reviewer(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "review_v2":
            return {"passed": passed, "conceptDefect": False, "suggestions": [],
                    "blockingIssues": issues, "warnings": []}
        return real(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", reviewer)


def _issue(**over):
    base = {"code": "X", "layerId": None, "assetId": None, "message": "m",
            "category": "visual", "severity": "blocking", "affected": "t_title",
            "impact": "The title is clipped.", "suggestion": None, "region": None}
    return {**base, **over}


def test_the_final_review_does_not_fail_a_job_on_a_minor_visual_difference(
        pipeline, monkeypatch):
    _final_review(monkeypatch, [_issue(severity="warning", code="MINOR_DRIFT")])
    job = run_pipeline(pipeline)
    assert job["status"] == "SUCCEEDED"
    review_doc = artifacts.get_json(pipeline["jobId"], job["artifacts"][Stage.REVIEW]["review"])
    assert review_doc["approved"] is True
    assert review_doc["modelPassed"] is False
    assert [w["code"] for w in review_doc["warnings"]] == ["MINOR_DRIFT"]


def test_the_final_review_still_blocks_on_a_material_problem(pipeline, monkeypatch):
    _final_review(monkeypatch, [_issue(code="TITLE_CLIPPED")])
    with pytest.raises(ReviewBlocked):
        run_pipeline(pipeline)


def test_the_final_review_never_downgrades_content_or_technical_problems(
        pipeline, monkeypatch):
    _final_review(monkeypatch, [_issue(category="content", severity="warning",
                                       code="WRONG_PHONE", affected="t_phone")])
    with pytest.raises(ReviewBlocked):
        run_pipeline(pipeline)


def test_a_mechanical_render_problem_is_a_blocking_technical_issue(pipeline, monkeypatch):
    from lib import render as renderer
    _problem = {"code": "TEXT_OVERFLOW", "layerId": "t_title", "message": "Overflows by 4px."}
    original = renderer.render

    def failing(html, canvas, scene):
        out = original(html, canvas, scene)
        out["checks"]["problems"] = [_problem]
        return out

    monkeypatch.setattr(renderer, "render", failing)
    _through_review_except_last(pipeline)
    out = run_stage(pipeline["jobId"], Stage.REVIEW)
    assert out["approved"] is False
    issue = out["blockingIssues"][0]
    assert (issue["category"], issue["severity"], issue["code"]) == (
        "technical", "blocking", "TEXT_OVERFLOW")


def test_a_skipped_render_check_blocks_the_review_and_cannot_finalize(pipeline, monkeypatch):
    from lib import render as renderer
    original = renderer.render

    def partial(html, canvas, scene):
        out = original(html, canvas, scene)
        out["checks"]["performed"] = [
            c for c in out["checks"]["performed"] if c != "feedCapture"]
        return out

    monkeypatch.setattr(renderer, "render", partial)
    _through_review_except_last(pipeline)
    out = run_stage(pipeline["jobId"], Stage.REVIEW)
    assert out["approved"] is False
    assert "RENDER_CHECK_MISSING" in [i["code"] for i in out["blockingIssues"]]
    with pytest.raises(ReviewBlocked):
        run_stage(pipeline["jobId"], Stage.FINALIZE)
    assert job_of(pipeline)["status"] != "SUCCEEDED"


def _through_review_except_last(pipeline):
    job_id = pipeline["jobId"]
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.CONCEPT, Stage.REVIEW_CONCEPT, Stage.EXTRACT_SCENE,
                  Stage.DECORATION_OVERLAY, Stage.CLEAN_PHOTO, Stage.VALIDATE_ASSETS,
                  Stage.ASSEMBLE, Stage.RENDER):
        run_stage(job_id, stage)


def _through_review(pipeline):
    job_id = pipeline["jobId"]
    for stage in (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                  Stage.CONCEPT, Stage.REVIEW_CONCEPT, Stage.EXTRACT_SCENE,
                  Stage.DECORATION_OVERLAY, Stage.CLEAN_PHOTO, Stage.VALIDATE_ASSETS,
                  Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW):
        run_stage(job_id, stage)


@pytest.mark.parametrize("stage,pointer", [
    (Stage.VALIDATE_ASSETS, "validation"),
    (Stage.DECORATION_OVERLAY, "decoration"),
    (Stage.CLEAN_PHOTO, "background"),
    (Stage.EXTRACT_SCENE, "scene"),
])
def test_finalize_refuses_when_a_mandatory_stage_output_is_missing(
        pipeline, stage, pointer):
    _through_review(pipeline)
    del job_of(pipeline)["artifacts"][stage][pointer]
    with pytest.raises(ReviewBlocked) as exc:
        run_stage(pipeline["jobId"], Stage.FINALIZE)
    assert f"{stage}.{pointer}" in exc.value.message
    assert job_of(pipeline)["status"] != "SUCCEEDED"


def test_finalize_refuses_layers_built_from_a_different_concept(pipeline):
    _through_review(pipeline)
    job_of(pipeline)["artifacts"][Stage.CLEAN_PHOTO]["conceptSha256"] = "0" * 64
    with pytest.raises(FinalHashMismatch) as exc:
        run_stage(pipeline["jobId"], Stage.FINALIZE)
    assert Stage.CLEAN_PHOTO in exc.value.message


def test_finalize_refuses_a_review_of_a_different_concept(pipeline):
    _through_review(pipeline)
    key = job_of(pipeline)["artifacts"][Stage.REVIEW]["review"]
    doc = artifacts.get_json(pipeline["jobId"], key)
    doc["conceptSha256"] = "f" * 64
    artifacts.put_json(key, doc, overwrite=True)
    with pytest.raises(FinalHashMismatch):
        run_stage(pipeline["jobId"], Stage.FINALIZE)


def test_failure_codes_name_the_kind_of_failure():
    assert RequiredCopyMissing("m").to_dict()["category"] == "content"
    assert ReferenceAdherenceFailed("m").to_dict()["category"] == "reference-adherence"
    assert InvalidTransparency("m").to_dict()["category"] == "asset-validation"
    assert InvalidTransparency("m").to_dict()["code"] == "INVALID_TRANSPARENCY"  # unchanged
    assert AssetInvalid("m").to_dict()["category"] == "asset-validation"


def test_the_recorded_failure_keeps_its_category(pipeline):
    job_id = pipeline["jobId"]
    cause = json.dumps({"errorMessage": "No candidate. [REVIEW_CONCEPT]"})
    handler_stage.handler(
        {"jobId": job_id, "recordFailure": True,
         "failure": {"Error": "RequiredCopyMissing", "Cause": cause}}, None)
    error = job_of(pipeline)["error"]
    assert error["code"] == "REQUIRED_COPY_MISSING"
    assert error["category"] == "content"
    assert error["stage"] == Stage.REVIEW_CONCEPT


# --- the workflow ------------------------------------------------------------


def test_the_workflow_routes_exhaustion_to_select_concept():
    from tests.test_workflow import STATES
    choice = STATES["ConceptApproved"]
    rules = {c["Variable"]: (c["BooleanEquals"], c["Next"]) for c in choice["Choices"]}
    assert rules["$.conceptReview.artifacts.approved"] == (True, "ExtractScene")
    assert rules["$.conceptReview.artifacts.correctionAvailable"] == (False, "SelectConcept")
    assert choice["Default"] == "CorrectConcept"
    select_state = STATES["SelectConcept"]
    assert select_state["Parameters"]["operation"] == "SELECT_CONCEPT"
    assert select_state["Next"] == "ExtractScene"
    assert select_state["Catch"][0]["Next"] == "RecordFailure"


# --- edit_mask helpers -------------------------------------------------------


def test_region_mask_opens_only_the_padded_boxes():
    mask, fraction = edit_mask.build_region_mask(
        (200, 100), [{"x": 0.25, "y": 0.5, "width": 0.5, "height": 0.2}])
    with Image.open(io.BytesIO(mask)) as m:
        alpha = m.convert("RGBA").getchannel("A")
    assert alpha.getpixel((100, 60)) == 0
    assert alpha.getpixel((5, 5)) == 255
    assert alpha.getpixel((100, 90)) == 255
    assert 0.1 < fraction < 0.3


def test_preserve_outside_restores_the_base_exactly_outside_the_mask():
    base = png_bytes(Image.new("RGB", (100, 100), (10, 20, 30)))
    painted = png_bytes(Image.new("RGB", (100, 100), (200, 0, 0)))
    mask, _ = edit_mask.build_region_mask(
        (100, 100), [{"x": 0.3, "y": 0.3, "width": 0.4, "height": 0.4}])
    out = edit_mask.preserve_outside(base, painted, mask)
    assert edit_mask.outside_difference(base, out, mask) == 0.0
    assert edit_mask.outside_difference(base, painted, mask) > 50
    assert pixel(out, (50, 50)) == (200, 0, 0)
    assert pixel(out, (2, 2)) == (10, 20, 30)


# --- generation prompt and the final-review concept defect -------------------


def test_generation_states_every_approved_text_and_its_planned_position(
        distinct_concepts):
    pipeline = distinct_concepts
    c419(pipeline)
    run_stage(pipeline["jobId"], Stage.CONCEPT, attempt=1)
    prompt = pipeline["calls"]["image"][-1]["prompt"]
    for item in C419_COPY:
        assert f'{item["contentKey"]}: "{item["text"]}"' in prompt
    assert "business.name: x=" in prompt
    assert "must be present" in prompt          # the business name is not optional
    assert "Take the photograph's subject matter from the brief" in prompt
    assert "BLOCKING CONTENT" in prompt          # the same severity policy


def _defect_review(monkeypatch, issues):
    real = vision_provider.analyze

    def reviewer(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "review_v2":
            # The reviewer must also CLASSIFY the finding: a concept defect that
            # arrives without a valid repair classification is never acted on.
            routed = [{**i, "repairTarget": "concept", "requiresImageGeneration": True,
                       "affectedElementIds": [], "constraints": [],
                       "impact": i.get("impact") or "the approved concept cannot be built"}
                      for i in issues]
            return {"passed": False, "conceptDefect": True, "suggestions": [],
                    "blockingIssues": routed, "warnings": []}
        return real(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", reviewer)


def test_a_concept_defect_corrects_the_approved_candidate_and_never_reselects_it(
        distinct_concepts, provider, monkeypatch):
    pipeline = distinct_concepts
    seed(pipeline)
    job_id = pipeline["jobId"]
    run_stage(job_id, Stage.CONCEPT, attempt=1)
    review(pipeline, 1, concept_verdict())
    assert job_of(pipeline)["approvedConcept"]["attempt"] == 1

    region = {"x": 0.55, "y": 0.1, "width": 0.4, "height": 0.5}
    _defect_review(monkeypatch, [_issue(code="CONCEPT_FLAW", region=region,
                                        affected="hero")])
    for stage in (Stage.EXTRACT_SCENE, Stage.DECORATION_OVERLAY, Stage.CLEAN_PHOTO,
                  Stage.VALIDATE_ASSETS, Stage.ASSEMBLE, Stage.RENDER):
        run_stage(job_id, stage)
    out = run_stage(job_id, Stage.REVIEW)
    assert out["conceptDefect"] is True

    plan = artifacts.get_json(job_id, correct(pipeline, 1)["plan"])
    assert plan["source"] == "final-review"
    assert plan["base"]["attempt"] == 1                   # the approved candidate
    assert [i["code"] for i in plan["issues"]] == ["CONCEPT_FLAW"]
    assert plan["regions"] == [region]
    assert jobs.concept_candidates(job_of(pipeline))[0]["rejectedByFinalReview"] is True

    provider.install()
    run_stage(job_id, Stage.CONCEPT, attempt=2)
    assert provider.calls[0]["image"][1] == concept_bytes(pipeline, 1)
    out = review(pipeline, 2, concept_verdict())
    assert out["approved"] is True
    assert job_of(pipeline)["approvedConcept"]["attempt"] == 2
