"""REVIEW_CONCEPT: judge the concept against the reference policy before any
paid separation work.

SPEC section REVIEW_CONCEPT. Two relationships are reviewed separately in this
service: reference -> concept (here, governed by designFreedom) and approved
concept -> final HTML (the REVIEW stage, faithful at every setting).

Approval is computed by the backend. A model asserting passed:true next to
blocking issues does not override them, and a model cannot approve a concept
that invents content or has unreadable copy at any setting.
"""

from __future__ import annotations

import json

import conf
from lib import artifacts, jobs, prompts, reference, reference_policy, schemas
from lib.errors import (
    ReferenceAdherenceFailed,
    ReferenceConstraintConflict,
    Stage,
)
from lib.providers import vision

CONCEPT_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "passed", "mode", "checks", "documentedExceptions",
        "blockingIssues", "suggestions", "constraintConflict",
    ],
    "properties": {
        "passed": {"type": "boolean"},
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
        "documentedExceptions": {"type": "array", "items": {"type": "string"}},
        "blockingIssues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "category", "message"],
                "properties": {
                    "code": {"type": "string"},
                    "category": {"type": "string", "enum": list(schemas.ISSUE_CATEGORIES)},
                    "message": {"type": "string"},
                },
            },
        },
        "suggestions": {"type": "array", "items": {"type": "string"}},
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


def decide(verdict, policy):
    """Apply the backend rules to a parsed model verdict.

    Returns `(approved, blocking_issues)` as plain dicts. Raises
    ReferenceConstraintConflict when the model reports an unresolvable conflict:
    repair rounds cannot fix one, and the service never silently redesigns, drops
    mandatory content or raises designFreedom to get past it (SPEC section 2.7).
    """
    conflict = verdict.constraintConflict
    if conflict.conflicted:
        reason = (conflict.reason or "").strip() or "Mandatory content cannot fit the reference layout."
        raise ReferenceConstraintConflict(reason)

    issues = [i.model_dump(mode="json") for i in verdict.blockingIssues]
    if policy["mode"] == reference_policy.FREE:
        # At the top band source-layout differences are not blockers. Invented
        # content, unreadable copy and poor composition still are.
        kept = [i for i in issues if i["category"] != "layoutAdherence"]
        if len(kept) != len(issues):
            # The model's own passed:false may have rested on the dropped issues,
            # so approval follows what is left rather than its flag.
            return not kept, kept
    return verdict.passed and not issues, issues


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.REVIEW_CONCEPT)
    produced = job["artifacts"]
    request = artifacts.get_json(job_id, job["requestKey"])

    policy = artifacts.get_json(job_id, produced[Stage.ANALYZE_REFERENCE]["policy"])
    analysis = artifacts.get_json(job_id, produced[Stage.ANALYZE_REFERENCE]["analysis"])
    copy_items = artifacts.get_json(job_id, produced[Stage.PREPARE_COPY]["copy"])["items"]
    concept_key = produced[Stage.CONCEPT]["concept"]
    concept_bytes = artifacts.get_bytes(job_id, concept_key)
    inspiration = reference.fetch(request["referenceImageUrl"])

    prompt = prompts.render(
        "review-concept",
        mode=policy["mode"],
        design_freedom=policy["designFreedom"],
        policy_instruction=policy["instruction"],
        must_preserve=prompts.bullet_list(policy["mustPreserve"]),
        may_change=prompts.bullet_list(policy["mayChange"]),
        documented_exceptions=prompts.bullet_list(policy["documentedExceptions"]),
        reference_anchors=json.dumps(analysis["anchors"], ensure_ascii=False),
        copy_block=prompts.copy_block(copy_items),
    )
    raw = vision.analyze(
        prompt,
        (inspiration["bytes"], concept_bytes),
        schema=CONCEPT_REVIEW_SCHEMA,
        schema_name="concept_review_v1",
        model=conf.REVIEW_MODEL,
        operation_id=f"{job_id}:review-concept:{attempt}",
        detail="original",
    )
    verdict = schemas.parse_concept_review(raw)
    approved, issues = decide(verdict, policy)

    result = {
        "approved": approved,
        "passed": approved,
        "mode": policy["mode"],
        "designFreedom": policy["designFreedom"],
        "policyVersion": policy["policyVersion"],
        "checks": [c.model_dump(mode="json") for c in verdict.checks],
        "documentedExceptions": list(verdict.documentedExceptions),
        "blockingIssues": issues,
        "suggestions": list(verdict.suggestions),
        "conceptKey": concept_key,
        "conceptSha256": artifacts.sha256_hex(concept_bytes),
        "conceptAttempt": int(job["attempts"].get(Stage.CONCEPT, 1)),
    }
    stored = artifacts.put_json(
        artifacts.artifact_key(job_id, Stage.REVIEW_CONCEPT, attempt, "concept-review.json"),
        result,
    )
    jobs.record_artifacts(job_id, Stage.REVIEW_CONCEPT, attempt, {"review": stored["key"]})
    if approved:
        jobs.freeze_concept(
            job_id,
            {
                "key": concept_key,
                "sha256": result["conceptSha256"],
                "attempt": result["conceptAttempt"],
                "reviewKey": stored["key"],
                "designFreedom": policy["designFreedom"],
                "policyVersion": policy["policyVersion"],
            },
        )
    return {
        "approved": approved,
        "review": stored["key"],
        "mode": policy["mode"],
        "blockingIssues": issues,
    }


def correct_concept(job_id, attempt, asset_id=None):
    """Consume one bounded concept-repair round.

    Same shape as review.correct(): the bound is a DynamoDB conditional update so
    it holds per job rather than per task retry.
    """
    jobs.set_stage(job_id, Stage.REVIEW_CONCEPT)
    job = jobs.get_job_unscoped(job_id)
    review = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW_CONCEPT]["review"])

    used = jobs.bump_counter(job_id, "conceptRounds", conf.MAX_CONCEPT_ROUNDS)
    if used is None:
        problems = "; ".join(i["message"] for i in review["blockingIssues"][:5])
        raise ReferenceAdherenceFailed(
            f"The concept still violates the reference policy after "
            f"{conf.MAX_CONCEPT_ROUNDS} concept correction rounds: {problems}"
        )
    return {"round": used, "nextAttempt": int(attempt) + 1}
