"""REVIEW_CONCEPT: judge each concept, keep every candidate, select the best valid one.

SPEC section REVIEW_CONCEPT. Two relationships are reviewed separately in this
service: reference -> concept (here, governed by designFreedom and judged under
reviewStrictness) and approved concept -> final HTML (the REVIEW stage).

What this stage decides, and how:

* Each attempt is a CANDIDATE. Its image and its review are stored separately and
  kept for the life of the job, so a later attempt can never erase an earlier one.
* Eligibility is computed by the backend from the evidence the model reports
  (lib/review_policy.py). A candidate is eligible only if every approved copy
  item is present, correct and readable, every required check actually ran, and
  no blocking issue remains. A model asserting that a design passes is not
  consulted: there is no such field.
* Correction stops as soon as a candidate is eligible. When the bounded rounds
  run out without one, every stored candidate is evaluated under the CURRENT
  policy and the best eligible one is selected. The latest attempt is never
  selected merely for being the latest, and a candidate with missing mandatory
  content is never selected for looking better.
* The selection (key, hash, attempt, policy version, reason) is frozen on the job.
  Every downstream stage reads that, so all of them use the same exact image.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import conf
from lib import (
    artifacts,
    direction_policy,
    jobs,
    policy_context,
    prompts,
    reference,
    review_policy,
    schemas,
)
from lib.errors import (
    AssetInvalid,
    InvalidModelJson,
    ReferenceAdherenceFailed,
    ReferenceConstraintConflict,
    RequiredCopyMissing,
    Stage,
)
from lib.providers import vision
from lib.stages import concept as concept_stage

#: Bumped when the shape of a stored review changes. A stored review with a
#: different value is re-evaluated by the model, not trusted.
REVIEW_SCHEMA_VERSION = "2.0"

_BOX = {
    "type": "object",
    "additionalProperties": False,
    "required": ["x", "y", "width", "height"],
    "properties": {
        "x": {"type": "number"},
        "y": {"type": "number"},
        "width": {"type": "number"},
        "height": {"type": "number"},
    },
}

CONCEPT_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "mode", "checks", "copyChecks", "documentedExceptions", "issues",
        "suggestions", "constraintConflict", "compositionDirectionOk",
    ],
    "properties": {
        "mode": {"type": "string"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["anchorId", "feature", "expectedPolicy", "observed", "passed"],
                "properties": {
                    "anchorId": {"type": ["string", "null"]},
                    "feature": {"type": "string"},
                    "expectedPolicy": {"type": "string"},
                    "observed": {"type": "string"},
                    "passed": {"type": "boolean"},
                },
            },
        },
        "copyChecks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["contentKey", "present", "correct", "readable", "observed"],
                "properties": {
                    "contentKey": {"type": "string"},
                    "present": {"type": "boolean"},
                    "correct": {"type": "boolean"},
                    "readable": {"type": "boolean"},
                    "observed": {"type": "string"},
                },
            },
        },
        "documentedExceptions": {"type": "array", "items": {"type": "string"}},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "code", "category", "severity", "affected", "impact",
                    "suggestion", "region",
                ],
                "properties": {
                    "code": {"type": "string"},
                    "category": {"type": "string", "enum": list(schemas.ISSUE_CATEGORIES)},
                    "severity": {"type": "string", "enum": list(schemas.ISSUE_SEVERITIES)},
                    "affected": {"type": "string"},
                    "impact": {"type": "string"},
                    "suggestion": {"type": ["string", "null"]},
                    "region": {"anyOf": [{"type": "null"}, _BOX]},
                },
            },
        },
        "suggestions": {"type": "array", "items": {"type": "string"}},
        # Judged against the direction block in the prompt, which carries the
        # same resolved policy the concept was generated under.
        "compositionDirectionOk": {"type": "boolean"},
        "constraintConflict": {
            "type": "object",
            "additionalProperties": False,
            "required": ["conflicted", "reason"],
            "properties": {
                "conflicted": {"type": "boolean"},
                "reason": {"type": ["string", "null"]},
            },
        },
    },
}


# --------------------------------------------------------------------------
# Reviewing one candidate
# --------------------------------------------------------------------------


def review_candidate(job_id, *, policy, analysis, copy_items, placements,
                     inspiration, concept_bytes, operation_id):
    """Ask the model about one concept and apply the backend's rules.

    Returns the full review document (what gets stored). It does not store it, so
    the caller decides where it lives and whether an earlier review is kept.
    """
    prompt = prompts.render(
        "review-concept",
        mode=policy["mode"],
        design_freedom=policy["designFreedom"],
        policy_instruction=policy["instruction"],
        must_preserve=prompts.bullet_list(policy["mustPreserve"]),
        may_change=prompts.bullet_list(policy["mayChange"]),
        documented_exceptions=prompts.bullet_list(policy["documentedExceptions"]),
        severity_block=review_policy.severity_block(policy["reviewPolicy"]),
        direction_block=direction_policy.prompt_block(
            policy.get("direction") or direction_policy.resolve({}, copy_items)
        ),
        reference_anchors=json.dumps(analysis["anchors"], ensure_ascii=False),
        copy_block=prompts.copy_block(copy_items),
        placement_block=concept_stage.placement_block(placements, copy_items),
    )
    raw = vision.analyze(
        prompt,
        (inspiration, concept_bytes),
        schema=CONCEPT_REVIEW_SCHEMA,
        schema_name="concept_review_v2",
        model=conf.REVIEW_MODEL,
        operation_id=operation_id,
        detail="original",
    )
    verdict = schemas.parse_concept_review(raw)
    return build_review(
        verdict.model_dump(mode="json"), policy, copy_items, placements, analysis
    )


def build_review(verdict, policy, copy_items, placements, analysis):
    """Decide from a parsed verdict dict. Pure; used again to re-decide stored ones."""
    anchor_ids = [a["id"] for a in analysis.get("anchors", [])]
    evaluation = review_policy.evaluate_concept(
        verdict, policy, copy_items, placements, anchor_ids
    )
    conflict = verdict["constraintConflict"]
    return {
        "schemaVersion": REVIEW_SCHEMA_VERSION,
        "approved": evaluation["eligible"],
        "eligible": evaluation["eligible"],
        "passed": evaluation["eligible"],
        "mode": policy["mode"],
        "designFreedom": policy["designFreedom"],
        "reviewStrictness": policy["reviewStrictness"],
        "policyVersion": policy["policyVersion"],
        "checks": verdict["checks"],
        "copyChecks": evaluation["copyChecks"],
        "requiredChecks": evaluation["requiredChecks"],
        "documentedExceptions": list(verdict["documentedExceptions"]),
        "issues": evaluation["issues"],
        "blockingIssues": evaluation["blockingIssues"],
        "warnings": evaluation["warnings"],
        "structuralFailures": evaluation["structuralFailures"],
        "suggestions": list(verdict["suggestions"]),
        "constraintConflict": {
            "conflicted": bool(conflict["conflicted"]),
            "reason": (conflict.get("reason") or "").strip() or None,
        },
        "regressions": [],
        # The model's own words, kept so the same evidence can be re-decided
        # under a later backend rule without another model call.
        "verdict": verdict,
    }


def _check_complete(review):
    """A review that skipped a required check is not a result."""
    missing = [c["name"] for c in review["requiredChecks"] if c["status"] == review_policy.MISSING]
    if missing:
        raise InvalidModelJson(
            "The concept review did not complete the required checks: "
            + ", ".join(missing)
        )


def _is_current(review, policy):
    return (
        review.get("schemaVersion") == REVIEW_SCHEMA_VERSION
        and review.get("policyVersion") == policy["policyVersion"]
        and review.get("reviewStrictness") == policy["reviewStrictness"]
        and isinstance(review.get("verdict"), dict)
    )


def _regressions(review, base_review):
    """What passed on the base candidate and fails on the corrected one."""
    found = []
    base_copy = {c["contentKey"]: c for c in base_review.get("copyChecks", [])}
    for check in review["copyChecks"]:
        before = base_copy.get(check["contentKey"])
        if before and before["status"] == review_policy.PASSED and check["status"] != review_policy.PASSED:
            found.append({"kind": "copy", "ref": check["contentKey"]})
    base_checks = {
        c["anchorId"]: c for c in base_review.get("checks", []) if c.get("anchorId")
    }
    for check in review["checks"]:
        before = base_checks.get(check.get("anchorId"))
        if before and before["passed"] and not check["passed"]:
            found.append({"kind": "structure", "ref": check["anchorId"]})
    return found


# --------------------------------------------------------------------------
# Stage entry point
# --------------------------------------------------------------------------


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.REVIEW_CONCEPT)
    produced = job["artifacts"]
    request = artifacts.get_json(job_id, job["requestKey"])

    policy, analysis, copy_items, placements = policy_context.load(job_id, job)
    concept_key = produced[Stage.CONCEPT]["concept"]
    concept_bytes = artifacts.get_bytes(job_id, concept_key)
    inspiration = reference.fetch(request["referenceImageUrl"])

    review = review_candidate(
        job_id,
        policy=policy,
        analysis=analysis,
        copy_items=copy_items,
        placements=placements,
        inspiration=inspiration["bytes"],
        concept_bytes=concept_bytes,
        operation_id=f"{job_id}:review-concept:{attempt}",
    )
    if review["constraintConflict"]["conflicted"]:
        # Repair rounds cannot fix one, and the service never silently redesigns,
        # drops mandatory content or raises designFreedom to get past it (2.7).
        raise ReferenceConstraintConflict(
            review["constraintConflict"]["reason"]
            or "Mandatory content cannot fit the reference layout."
        )
    _check_complete(review)

    attempt = int(attempt)
    correction = job.get("conceptCorrection") or {}
    base_attempt = None
    if correction.get("forAttempt") == attempt:
        plan = artifacts.get_json(job_id, correction["planKey"])
        base_attempt = plan["base"]["attempt"]
        base_review = artifacts.get_json(job_id, plan["base"]["reviewKey"])
        review["regressions"] = _regressions(review, base_review)
        review["baseAttempt"] = base_attempt
        for regression in review["regressions"]:
            if regression["kind"] == "structure":
                review["warnings"].append(
                    {
                        "code": "REGRESSION_AFTER_CORRECTION",
                        "category": review_policy.VISUAL,
                        "severity": review_policy.WARNING,
                        "affected": regression["ref"],
                        "impact": "This area passed before the correction and no longer does.",
                        "suggestion": None,
                        "region": None,
                        "message": f"{regression['ref']}: passed before the correction, fails now.",
                    }
                )

    review["conceptKey"] = concept_key
    review["conceptSha256"] = artifacts.sha256_hex(concept_bytes)
    review["conceptAttempt"] = attempt
    stored = artifacts.put_json(
        artifacts.artifact_key(job_id, Stage.REVIEW_CONCEPT, attempt, "concept-review.json"),
        review,
    )
    jobs.record_artifacts(job_id, Stage.REVIEW_CONCEPT, attempt, {"review": stored["key"]})
    jobs.record_concept_candidate(
        job_id,
        attempt,
        {
            "attempt": attempt,
            "conceptKey": concept_key,
            "conceptSha256": review["conceptSha256"],
            "reviewKey": stored["key"],
            "historicalReviewKeys": [],
            "policyVersion": policy["policyVersion"],
            "reviewStrictness": policy["reviewStrictness"],
            "baseAttempt": base_attempt,
            "eligible": review["eligible"],
            "blocking": len(review["blockingIssues"]),
            "warnings": len(review["warnings"]),
            "structuralFailures": review["structuralFailures"],
            "rejectedByFinalReview": False,
        },
    )

    if review["eligible"]:
        # Correction stops here: a valid candidate exists.
        job = jobs.get_job_unscoped(job_id)
        candidates = _evaluations(job_id, job, policy, analysis, copy_items, placements)
        _select_and_freeze(
            job_id, attempt, policy, candidates,
            selected_by="first-eligible",
        )

    return {
        "approved": review["eligible"],
        "review": stored["key"],
        "mode": policy["mode"],
        "reviewStrictness": policy["reviewStrictness"],
        "blockingIssues": review["blockingIssues"],
        "warnings": review["warnings"],
        # The workflow's Choice needs to know whether another round exists before
        # it branches: when none does it goes to SELECT_CONCEPT instead.
        "correctionAvailable": (
            int(job.get("conceptRounds", 0)) < conf.MAX_CONCEPT_ROUNDS
        ),
    }


# --------------------------------------------------------------------------
# Candidates, selection
# --------------------------------------------------------------------------


def _evaluations(job_id, job, policy, analysis, copy_items, placements, reviews=None):
    """Every candidate as a comparable record, decided under the CURRENT backend rules.

    The stored model verdict is re-decided here, so a change to the backend's
    rules applies to old candidates too. A candidate whose stored review is from
    another policy version or schema is not decided from it at all; the caller
    re-reviews those (see `select_concept`).
    """
    evaluations = []
    for record in jobs.concept_candidates(job):
        review = (reviews or {}).get(int(record["attempt"])) or artifacts.get_json(
            job_id, record["reviewKey"]
        )
        if not _is_current(review, policy):
            continue
        decided = build_review(review["verdict"], policy, copy_items, placements, analysis)
        evaluations.append(
            {
                "attempt": int(record["attempt"]),
                "conceptKey": record["conceptKey"],
                "conceptSha256": record["conceptSha256"],
                "reviewKey": record["reviewKey"],
                "baseAttempt": record.get("baseAttempt"),
                "rejectedByFinalReview": bool(record.get("rejectedByFinalReview")),
                "eligible": decided["eligible"] and not record.get("rejectedByFinalReview"),
                "blockingIssues": decided["blockingIssues"],
                "warnings": decided["warnings"],
                "structuralFailures": decided["structuralFailures"],
                "requiredChecks": decided["requiredChecks"],
                "conflicted": decided["constraintConflict"]["conflicted"],
            }
        )
    return evaluations


def _select_and_freeze(job_id, attempt, policy, candidates, *, selected_by):
    """Pick the best eligible candidate, record why, and pin it on the job."""
    selected = review_policy.select_best(candidates)
    if selected is None:
        return None
    reason = review_policy.selection_reason(selected, candidates)
    document = {
        "selectedAttempt": selected["attempt"],
        "selectedBy": selected_by,
        "reason": reason,
        "policyVersion": policy["policyVersion"],
        "reviewStrictness": policy["reviewStrictness"],
        "candidates": _summaries(candidates),
    }
    key = _store_selection(job_id, attempt, document)
    jobs.freeze_concept(
        job_id,
        {
            "key": selected["conceptKey"],
            "sha256": selected["conceptSha256"],
            "attempt": selected["attempt"],
            "reviewKey": selected["reviewKey"],
            "designFreedom": policy["designFreedom"],
            "policyVersion": policy["policyVersion"],
            "reviewStrictness": policy["reviewStrictness"],
            "selectedBy": selected_by,
            "selectionReason": reason,
            "selectionKey": key,
        },
    )
    return selected


def _summaries(candidates):
    return [
        {
            "attempt": c["attempt"],
            "eligible": c["eligible"],
            "blocking": [i["code"] for i in c["blockingIssues"]],
            "warnings": [i["code"] for i in c["warnings"]],
            "structuralFailures": c["structuralFailures"],
            "rejectedByFinalReview": c["rejectedByFinalReview"],
        }
        for c in sorted(candidates, key=lambda c: c["attempt"])
    ]


def _store_selection(job_id, attempt, document):
    """Write the selection record; a repeated call reads back what is there."""
    key = artifacts.artifact_key(
        job_id, Stage.REVIEW_CONCEPT, int(attempt), "concept-selection.json"
    )
    try:
        artifacts.put_json(key, document)
    except AssetInvalid:
        # A retried invocation: artifacts are immutable, and the first record of
        # the decision is the one that counts.
        pass
    return key


def select_concept(job_id, attempt, asset_id=None):
    """At retry exhaustion: choose the best eligible stored candidate, or fail.

    Every candidate is evaluated under the current policy. Those reviewed under it
    are re-decided from their stored verdicts; any reviewed under another policy
    version is reviewed again by the model, and that reevaluation is stored beside
    (never over) the original, which stays readable as history.
    """
    jobs.set_stage(job_id, Stage.REVIEW_CONCEPT)
    job = jobs.get_job_unscoped(job_id)
    policy, analysis, copy_items, placements = policy_context.load(job_id, job)
    request = artifacts.get_json(job_id, job["requestKey"])

    records = jobs.concept_candidates(job)
    if not records:
        raise AssetInvalid("No concept candidate was recorded for this job.")

    reviews = {}
    stale = []
    for record in records:
        review = artifacts.get_json(job_id, record["reviewKey"])
        if _is_current(review, policy):
            reviews[int(record["attempt"])] = review
        else:
            stale.append(record)

    if stale:
        inspiration = reference.fetch(request["referenceImageUrl"])["bytes"]

        def reevaluate(record):
            concept_bytes = artifacts.get_bytes(job_id, record["conceptKey"])
            review = review_candidate(
                job_id,
                policy=policy,
                analysis=analysis,
                copy_items=copy_items,
                placements=placements,
                inspiration=inspiration,
                concept_bytes=concept_bytes,
                operation_id=(
                    f"{job_id}:review-concept:reeval:{record['attempt']}:"
                    f"{policy['policyVersion']}"
                ),
            )
            review["conceptKey"] = record["conceptKey"]
            review["conceptSha256"] = record["conceptSha256"]
            review["conceptAttempt"] = int(record["attempt"])
            review["reevaluationOf"] = record["reviewKey"]
            return record, review

        with ThreadPoolExecutor(max_workers=min(4, len(stale))) as pool:
            done = list(pool.map(reevaluate, stale))
        for record, review in done:
            if review["constraintConflict"]["conflicted"]:
                continue
            name = (
                f"concept-review-reevaluation-{policy['policyVersion']}-"
                f"{policy['reviewStrictness']}.json"
            )
            key = artifacts.artifact_key(
                job_id, Stage.REVIEW_CONCEPT, int(record["attempt"]), name
            )
            try:
                artifacts.put_json(key, review)
            except AssetInvalid:
                review = artifacts.get_json(job_id, key)
            updated = dict(record)
            updated["historicalReviewKeys"] = list(record.get("historicalReviewKeys", [])) + [
                record["reviewKey"]
            ]
            updated["reviewKey"] = key
            updated["policyVersion"] = policy["policyVersion"]
            updated["reviewStrictness"] = policy["reviewStrictness"]
            jobs.record_concept_candidate(job_id, int(record["attempt"]), updated)
            reviews[int(record["attempt"])] = review
        job = jobs.get_job_unscoped(job_id)

    candidates = _evaluations(job_id, job, policy, analysis, copy_items, placements, reviews)
    selected = _select_and_freeze(
        job_id, attempt, policy, candidates, selected_by="best-of-candidates"
    )
    if selected is not None:
        return {
            "approved": True,
            "selectedAttempt": selected["attempt"],
            "selectedBy": "best-of-candidates",
        }

    _raise_no_valid_candidate(job_id, attempt, policy, candidates)


def _raise_no_valid_candidate(job_id, attempt, policy, candidates):
    """Fail with the reason that actually decided it: content or structure."""
    document = {
        "selectedAttempt": None,
        "selectedBy": None,
        "reason": "No candidate is eligible.",
        "policyVersion": policy["policyVersion"],
        "reviewStrictness": policy["reviewStrictness"],
        "candidates": _summaries(candidates),
    }
    _store_selection(job_id, attempt, document)
    if not candidates:
        raise ReferenceAdherenceFailed(
            "No concept candidate could be evaluated under the current policy."
        )
    closest = review_policy.best_correction_base(candidates)
    rounds = conf.MAX_CONCEPT_ROUNDS
    content = [i for i in closest["blockingIssues"] if i["category"] == review_policy.CONTENT]
    detail = "; ".join(i["message"] for i in closest["blockingIssues"][:5])
    summary = "; ".join(
        f"attempt {c['attempt']}: {len(c['blockingIssues'])} blocking"
        for c in sorted(candidates, key=lambda c: c["attempt"])
    )
    if content:
        raise RequiredCopyMissing(
            f"No concept candidate carries all of the mandatory approved text after "
            f"{rounds} concept correction rounds ({summary}). Closest candidate "
            f"(attempt {closest['attempt']}): {detail}"
        )
    raise ReferenceAdherenceFailed(
        f"No concept candidate is free of blocking issues after {rounds} concept "
        f"correction rounds ({summary}). Closest candidate "
        f"(attempt {closest['attempt']}): {detail}"
    )


# --------------------------------------------------------------------------
# Correction plan
# --------------------------------------------------------------------------


def correct_concept(job_id, attempt, asset_id=None):
    """Consume one bounded concept-correction round and plan the edit.

    Same shape as review.correct(): the bound is a DynamoDB conditional update so
    it holds per job rather than per task retry. The round's output is a PLAN:
    which stored candidate to edit, which blocking issues to fix and where. The
    next CONCEPT attempt executes it. It never regenerates from the reference.
    """
    jobs.set_stage(job_id, Stage.REVIEW_CONCEPT)
    job = jobs.get_job_unscoped(job_id)
    policy, analysis, copy_items, placements = policy_context.load(job_id, job)

    used = jobs.bump_counter(job_id, "conceptRounds", conf.MAX_CONCEPT_ROUNDS)
    if used is None:
        review = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW_CONCEPT]["review"])
        problems = "; ".join(i["message"] for i in review["blockingIssues"][:5])
        raise ReferenceAdherenceFailed(
            f"The concept still has blocking issues after "
            f"{conf.MAX_CONCEPT_ROUNDS} concept correction rounds: {problems}"
        )

    latest = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW_CONCEPT]["review"])
    if latest["eligible"]:
        # Routed here by the final review: the approved concept itself is at fault.
        source = "final-review"
        approved = jobs.approved_concept(job)
        final = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW]["review"])
        issues = [
            review_policy.normalize_issue(
                {**i, "affected": i.get("affected") or i.get("layerId") or i.get("assetId")}
            )
            for i in final["blockingIssues"]
        ]
        base = {
            "attempt": int(approved["attempt"]),
            "key": approved["key"],
            "sha256": approved["sha256"],
            "reviewKey": approved["reviewKey"],
        }
        _reject_after_final_review(job_id, job, approved["attempt"])
    else:
        source = "concept-review"
        candidates = _evaluations(job_id, job, policy, analysis, copy_items, placements)
        chosen = review_policy.best_correction_base(candidates)
        if chosen is None:
            raise AssetInvalid("No concept candidate is available to correct.")
        issues = chosen["blockingIssues"]
        base = {
            "attempt": chosen["attempt"],
            "key": chosen["conceptKey"],
            "sha256": chosen["conceptSha256"],
            "reviewKey": chosen["reviewKey"],
        }

    issues = [i for i in issues if i["severity"] == review_policy.BLOCKING]
    if not issues:
        raise ReferenceAdherenceFailed(
            "The concept was rejected but no blocking issue was recorded, so there "
            "is nothing specific to correct."
        )

    regions, whole_image = plan_regions(issues, placements)
    plan = {
        "forAttempt": int(attempt) + 1,
        "source": source,
        "base": base,
        "issues": issues,
        "regions": regions,
        "wholeImage": whole_image,
        "policyVersion": policy["policyVersion"],
        "reviewStrictness": policy["reviewStrictness"],
    }
    key = artifacts.artifact_key(
        job_id, Stage.REVIEW_CONCEPT, int(attempt), "correction-plan.json"
    )
    try:
        artifacts.put_json(key, plan)
    except AssetInvalid:
        plan = artifacts.get_json(job_id, key)
    jobs.set_concept_correction(job_id, {"forAttempt": int(attempt) + 1, "planKey": key})
    return {
        "round": used,
        "nextAttempt": int(attempt) + 1,
        "plan": key,
        "baseAttempt": base["attempt"],
        "issues": [i["code"] for i in issues],
    }


def plan_regions(issues, placements):
    """Where to edit: the planned slot for copy, the reported region otherwise.

    Returns `(regions, whole_image)`. If any blocking issue has no locatable
    region the edit cannot be confined, so it is whole-image: still based on the
    chosen candidate and told to preserve everything else, but without a mask.
    """
    regions = []

    def add(box):
        if box not in regions:
            regions.append(box)

    for issue in issues:
        box = None
        if issue["category"] == review_policy.CONTENT and issue["affected"] in placements:
            box = placements[issue["affected"]]
            # A reported region for the same copy item is where the wrong text
            # actually is, which may differ from where it was planned.
            if issue.get("region"):
                add(issue["region"])
        elif issue.get("region"):
            box = issue["region"]
        if box is None:
            return [], True
        add(box)
    return regions, False


def _reject_after_final_review(job_id, job, attempt):
    """The final review found this approved concept at fault: never select it again."""
    record = (job.get("conceptCandidates") or {}).get(str(int(attempt)))
    if record:
        updated = dict(record)
        updated["rejectedByFinalReview"] = True
        jobs.record_concept_candidate(job_id, int(attempt), updated)
