"""REUSE_CONCEPT: start a job from an earlier job's approved concept.

Replaces CONCEPT and REVIEW_CONCEPT for a request carrying `reuseConceptFromJobId`.
The use is re-running the reconstruction - extraction, separation, assembly, review -
after a fix to it, on the very concept the customer approved. Nothing is generated and
nothing is re-judged: the concept, its review and the rule it was selected under are
copied into this job and frozen exactly as REVIEW_CONCEPT would freeze them, with a
note of where they came from. A paid concept can then never be replaced by accident
(final review may not route to a new concept either; see review._route).

The source job is read-only here and must belong to the same owner.
"""

from __future__ import annotations

import logging

from lib import artifacts, jobs
from lib.errors import AssetInvalid, JobNotFound, Stage

log = logging.getLogger("layered-design.reuse-concept")


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.REUSE_CONCEPT)

    source_id = (job.get("reuseConceptFrom") or {}).get("jobId")
    if not source_id:
        raise AssetInvalid("This job was not started from an earlier job's concept.")
    try:
        source = jobs.get_job_unscoped(source_id)
    except JobNotFound:
        raise AssetInvalid("The job whose concept was to be reused no longer exists.") from None
    if source.get("ownerScope") != job.get("ownerScope"):
        # Checked again here, not only at acceptance: this is the stage that reads.
        raise AssetInvalid("The concept to reuse belongs to a different owner.")

    approved = jobs.approved_concept(source)
    data = artifacts.get_bytes(source_id, approved["key"])
    if artifacts.sha256_hex(data) != approved["sha256"]:
        raise AssetInvalid("The approved concept no longer matches its recorded hash.")

    concept_key = artifacts.artifact_key(job_id, Stage.CONCEPT, 1, "concept.png")
    concept = artifacts.put_bytes(concept_key, data, "image/png")
    review = artifacts.get_bytes(source_id, approved["reviewKey"])
    review_key = artifacts.artifact_key(
        job_id, Stage.REVIEW_CONCEPT, 1, "concept-review.json")
    stored_review = artifacts.put_bytes(review_key, review, "application/json")

    reason = (
        f"Reused the approved concept of job {source_id} (attempt "
        f"{approved.get('attempt')}); no concept was generated or re-judged."
    )
    selection_key = artifacts.artifact_key(
        job_id, Stage.REVIEW_CONCEPT, 1, "concept-selection.json")
    artifacts.put_json(selection_key, {
        "selectedAttempt": 1, "selectedBy": "reused", "reason": reason,
        "reusedFrom": {"jobId": source_id, "attempt": approved.get("attempt"),
                       "sha256": approved["sha256"],
                       "selectionKey": approved.get("selectionKey")},
        "policyVersion": approved.get("policyVersion"),
        "reviewStrictness": approved.get("reviewStrictness"),
        "candidates": [],
    })

    jobs.record_artifacts(job_id, Stage.CONCEPT, 1, {
        "concept": concept["key"], "sha256": concept["sha256"], "reusedFrom": source_id})
    jobs.record_artifacts(job_id, Stage.REVIEW_CONCEPT, 1, {"review": stored_review["key"]})
    jobs.freeze_concept(job_id, {
        "key": concept["key"],
        "sha256": concept["sha256"],
        "attempt": 1,
        "reviewKey": stored_review["key"],
        "designFreedom": approved.get("designFreedom", job.get("designFreedom", 0)),
        "policyVersion": approved.get("policyVersion", job.get("policyVersion")),
        "reviewStrictness": approved.get("reviewStrictness", job.get("reviewStrictness")),
        "selectedBy": "reused",
        "selectionReason": reason,
        "selectionKey": selection_key,
        "reusedFrom": {"jobId": source_id, "attempt": approved.get("attempt")},
    })
    jobs.record_concept_candidate(job_id, 1, {
        "attempt": 1, "conceptKey": concept["key"], "conceptSha256": concept["sha256"],
        "reviewKey": stored_review["key"], "historicalReviewKeys": [],
        "policyVersion": approved.get("policyVersion"),
        "reviewStrictness": approved.get("reviewStrictness"),
        "baseAttempt": None, "eligible": True, "blocking": 0, "warnings": 0,
        "structuralFailures": [], "rejectedByFinalReview": False,
    })
    log.info("concept reused jobId=%s from=%s", job_id, source_id)
    return {"concept": concept["key"], "sha256": concept["sha256"], "reusedFrom": source_id}
