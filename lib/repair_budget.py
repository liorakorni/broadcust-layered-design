"""One persisted, shared repair budget for the layered-v2 path.

Before this, the bounds were spread over three places: `repairs_<category>` counters
(conf.MAX_REPAIRS), an `overlayRepairs` counter that validation and the repair stage both
spent, and the workflow's own loop (RepairOverlay -> RebuildPhoto -> ValidateAssets) that
could go round once per overlay repair, regenerating the photograph every time. The
fixture job 9281413450b748119609d627b61670f0 spent seven image calls that way.

For a layered-v2 job the budget is a document on the job, written once at acceptance:

    templateImage   template (Design Base) image repairs. Default 1, spent only AFTER
                    the first HTML exists, on a finding from the composite review.
    domCss          DOM/CSS correction rounds (the light-repair path). Default 2.
    photoImage      photograph regenerations. Default 0: a template repair never rebuilds
                    the photograph. A confirmed, separate photograph defect can be GRANTED
                    one (and only one) by `grant_photo_repair`, which records why.

The limits are stored with the job, so a later change to a default does not move the bar
for a job already accepted. Every spend is an atomic conditional increment of a flat
counter, so the bound holds across task retries and concurrent stages; nothing resets a
counter, and nothing here retries. Transient technical retries (rate limits, timeouts) are
the workflow's Retry blocks and are a separate, bounded matter that never touches this.
"""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError

from lib import jobs

log = logging.getLogger("layered-design.repair-budget")

VERSION = "repair-budget-1"

KINDS = ("templateImage", "domCss", "photoImage")

DEFAULT_LIMITS = {"templateImage": 1, "domCss": 2, "photoImage": 0}

#: A photograph regeneration can be granted once per job, never more.
PHOTO_REPAIR_CAP = 1


def seed():
    """The budget document stored on a new job."""
    return {"version": VERSION, "limits": dict(DEFAULT_LIMITS), "photoGrant": None}


def counter(kind):
    _check(kind)
    return f"budgetUsed_{kind}"


def limits(job):
    stored = ((job or {}).get("repairBudget") or {}).get("limits")
    return {**DEFAULT_LIMITS, **{k: int(v) for k, v in (stored or {}).items() if k in KINDS}}


def used(job, kind):
    return int((job or {}).get(counter(kind), 0) or 0)


def remaining(job, kind):
    return max(0, limits(job)[kind] - used(job, kind))


def snapshot(job):
    """What a result or a diagnostic reports: limits, spend and what is left."""
    return {
        "version": ((job or {}).get("repairBudget") or {}).get("version", VERSION),
        "limits": limits(job),
        "used": {k: used(job, k) for k in KINDS},
        "remaining": {k: remaining(job, k) for k in KINDS},
        "photoGrant": ((job or {}).get("repairBudget") or {}).get("photoGrant"),
    }


def routing_view(job):
    """`(counts, limits)` per routing category, for a layered-v2 job.

    The router reasons in categories (content, typography, layout, asset, concept); the
    budget is in kinds. The three DOM/CSS categories share ONE allowance, so two content
    rounds and two layout rounds cannot add up to four; the asset category is the template
    allowance plus any granted photograph repair; concept keeps its own bound.
    """
    import conf

    dom_used, dom_limit = used(job, "domCss"), limits(job)["domCss"]
    counts = {category: dom_used for category in ("content", "typography", "layout")}
    caps = {category: dom_limit for category in ("content", "typography", "layout")}
    # The template allowance plus the one photograph repair that could still be granted;
    # which of the two a given repair spends is decided by `asset_kind` / `can_spend`.
    counts["asset"] = used(job, "templateImage") + used(job, "photoImage")
    caps["asset"] = limits(job)["templateImage"] + max(limits(job)["photoImage"], PHOTO_REPAIR_CAP)
    counts["concept"] = int((job or {}).get("conceptRounds", 0) or 0)
    caps["concept"] = conf.MAX_CONCEPT_ROUNDS
    return counts, caps


def asset_kind(asset_target):
    """The budget kind a repair of this asset spends."""
    return "photoImage" if asset_target == "background" else "templateImage"


def can_spend(job, kind):
    """Could `kind` still be spent: its allowance is not used up (a photograph repair may
    also be granted, once, if none has been)."""
    _check(kind)
    if kind == "photoImage":
        granted = ((job or {}).get("repairBudget") or {}).get("photoGrant")
        allowance = limits(job)["photoImage"] if granted else PHOTO_REPAIR_CAP
        return used(job, kind) < allowance
    return remaining(job, kind) > 0


def consume(job_id, kind):
    """Spend one unit of `kind`. Returns the new total, or None when none is left.

    The limit is read from the job's own persisted budget; the increment is conditional
    on staying below it, so two concurrent spends cannot both succeed on the last unit.
    """
    _check(kind)
    job = jobs.get_job_unscoped(job_id)
    limit = limits(job)[kind]
    if limit <= 0:
        return None
    field = counter(kind)
    try:
        result = jobs._update(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression=f"SET {field} = {field} + :one, updatedAt = :now",
            ConditionExpression=f"{field} < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":now": jobs._now()},
            ReturnValues="UPDATED_NEW",
        )
        return int(result["Attributes"][field])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


def initial_attributes():
    """The attributes a new layered-v2 job is created with: the document and its counters.

    The counters must exist at zero: `field < :limit` is false for an absent field, which
    would read as an exhausted budget.
    """
    return {"repairBudget": seed(), **{counter(k): 0 for k in KINDS}}


def grant_photo_repair(job_id, reason, evidence=None):
    """Allow ONE photograph regeneration, for a confirmed defect in the photograph itself.

    Returns True when granted, False when the cap is already reached. `reason` is required
    and is stored with the grant: the exception is documented, bounded and visible in the
    result, not an open door. A template repair never calls this.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A photograph repair needs a documented reason.")
    job = jobs.get_job_unscoped(job_id)
    if limits(job)["photoImage"] >= PHOTO_REPAIR_CAP:
        return False
    grant = {"reason": reason.strip()[:400], "evidence": evidence or {}, "grantedAt": jobs._now()}
    budget = dict((job.get("repairBudget") or seed()))
    budget["limits"] = {**limits(job), "photoImage": PHOTO_REPAIR_CAP}
    budget["photoGrant"] = grant
    jobs._update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET repairBudget = :budget, updatedAt = :now",
        ExpressionAttributeValues={":budget": budget, ":now": jobs._now()},
    )
    return True


def _check(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown repair budget {kind!r}")
