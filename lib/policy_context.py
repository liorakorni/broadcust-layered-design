"""Load the policy, reference analysis and copy a concept stage works from.

One loader for generation, review, correction and selection is what makes them
use the same policy: there is no second place that could read a different
version or fall back to a default.

A stored policy or analysis is only reused when it was produced under the
current `POLICY_VERSION`. The reference analysis now carries planned copy
placements and content-neutral anchors; one written earlier has neither, and
reading it as if it did would apply the wrong rules without any error. It fails
loudly instead, naming both versions.
"""

from __future__ import annotations

from lib import artifacts, reference_policy
from lib.errors import PolicyVersionMismatch, Stage


def load(job_id, job):
    """Return `(policy, analysis, copy_items, placements)` for a job.

    `placements` maps contentKey to its planned normalized box.
    """
    produced = job["artifacts"]
    analyzed = produced.get(Stage.ANALYZE_REFERENCE) or {}
    if not analyzed.get("policy") or not analyzed.get("analysis"):
        raise PolicyVersionMismatch(
            "No reference policy has been recorded for this job."
        )
    policy = artifacts.get_json(job_id, analyzed["policy"])
    analysis = artifacts.get_json(job_id, analyzed["analysis"])
    current = reference_policy.POLICY_VERSION

    for label, document in (("policy", policy), ("reference analysis", analysis)):
        found = document.get("policyVersion")
        if found != current:
            raise PolicyVersionMismatch(
                f"The stored {label} was produced under policy version "
                f"{found!r}, not the current {current!r}. It is not reused; "
                f"submit the request again to analyse the reference afresh."
            )
    if policy.get("reviewStrictness") != job.get("reviewStrictness", policy.get("reviewStrictness")):
        raise PolicyVersionMismatch(
            "The stored policy's reviewStrictness differs from the job's."
        )

    copy_items = artifacts.get_json(job_id, produced[Stage.PREPARE_COPY]["copy"])["items"]
    placements = {p["contentKey"]: p["box"] for p in analysis.get("copyPlacements", [])}
    return policy, analysis, copy_items, placements
