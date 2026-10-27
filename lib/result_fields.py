"""The result fields a layered-v2 job adds, all read from ONE immutable candidate.

`finalize` already reads the delivered candidate's own pointers (document, scene,
render, review). Everything here is derived from those same objects and from the job's
persisted settings, never from "whatever happened last", so a rejected later attempt can
change neither a score nor a fallback list nor a warning.

A job accepted before this pipeline version (`layered-v1`) is not touched: it gets none
of these fields and no migration is attempted.

The pieces, each computed in isolation so that one failing cannot take the HTML with it:
  * layer-contract compliance      lib/layer_contract.py
  * fallbacksUsed                  the candidate's own layer plan, enriched by the job record
  * quality score                  lib/quality.py (provisional)
  * timing                         lib/milestones.py
"""

from __future__ import annotations

import logging

from lib import artifacts, jobs, layer_contract, milestones, quality, template_policy
from lib.errors import Stage, Status

log = logging.getLogger("layered-design.result")

#: Warnings the validation stage records job-wide. For v2 they are rebuilt from the
#: selected candidate's own records, so a warning raised while a later, rejected attempt
#: was validated is not carried into a result about an earlier one.
_REBUILT_STAGES = (Stage.VALIDATE_ASSETS,)


def applies(job):
    return jobs.is_v2(job)


def fallbacks_for(job, scene):
    """The fallbacks this candidate's own document depends on.

    Read from the candidate's layer plan (what it actually is), enriched with the
    job-level record (why, and what was omitted) when one matches by code.
    """
    plan = (scene or {}).get("layerPlan") or {}
    recorded = {f"{e.get('kind')}:{e.get('code')}": e for e in jobs.fallbacks_used(job)}
    found = []
    for correction in plan.get("corrections") or []:
        if correction.get("action") not in ("template-omitted-css-fallback", "template-css-only"):
            continue
        kind = ("template-css-only" if correction["action"] == "template-css-only"
                else "template-css-fallback")
        entry = recorded.get(f"{kind}:{correction.get('code')}") or {}
        found.append({
            "kind": kind, "code": correction.get("code"),
            "reason": entry.get("reason") or (plan.get("template") or {}).get("reason"),
            "drawn": entry.get("drawn") or [], "omitted": entry.get("omitted") or [],
            "touched": correction.get("touched") or [], "layerPlanRevision": correction.get("revision"),
        })
    return found


def _warning(code, message, affected, action):
    return {"stage": Stage.FINALIZE, "code": code, "severity": "warning", "category": None,
            "affected": ", ".join(affected) or None, "affectedElementIds": list(affected),
            "impact": message, "message": message, "suggestion": action,
            "suggestedAction": action}


def build(job_id, job, produced, scene, review, selected, outcome, render_pointers):
    """`(fields, outcome, extra_warnings)`: additive result fields and the adjusted outcome.

    Never raises: each part that fails is reported as unavailable with its reason.
    """
    fields = {
        "pipelineVersion": jobs.pipeline_version(job),
        "templateFlexibility": job.get("templateFlexibility",
                                       (job.get("templateTolerance") or {}).get("value")),
        "templateTolerance": job.get("templateTolerance"),
    }
    extra, outcome = [], dict(outcome)

    validation = None
    pointer = (produced.get(Stage.VALIDATE_ASSETS) or {}).get("validation")
    try:
        validation = artifacts.get_json(job_id, pointer) if pointer else None
    except Exception:  # noqa: BLE001
        log.exception("could not read the candidate's validation record")
    try:
        contract = layer_contract.assess(scene, validation)
    except Exception:  # noqa: BLE001
        log.exception("layer contract assessment failed")
        contract = {"checked": False, "compliant": None, "violations": [],
                    "needsReview": False, "reason": "assessment failed"}
    fields["layerContract"] = contract
    if contract.get("violations"):
        # An owner conflict is for a person to look at; it is not hidden and it does not
        # by itself fail a design that was otherwise delivered.
        outcome["needsReview"] = True
        outcome["publishReady"] = False
        for violation in contract["violations"]:
            extra.append(_warning(
                violation["code"], violation["message"], violation["affected"],
                "Review these layers in the editor before publishing."))

    try:
        fields["fallbacksUsed"] = fallbacks_for(job, scene)
    except Exception:  # noqa: BLE001
        log.exception("could not list the fallbacks")
        fields["fallbacksUsed"] = []
    for entry in fields["fallbacksUsed"]:
        if entry["kind"] == "template-css-fallback":
            # A design base that could not be used is a delivery with a warning - it is
            # neither a failure nor a clean success - and for a person to look at.
            if outcome.get("status") == Status.SUCCEEDED:
                outcome["status"] = Status.SUCCEEDED_WITH_WARNINGS
            outcome["needsReview"] = True
            outcome["publishReady"] = False
            extra.append(_warning(
                "TEMPLATE_REPLACED_BY_CSS_FALLBACK",
                "The generated design base could not be used (" + str(entry["code"]) + "), "
                "so the page uses plain fills and gradients drawn in CSS. Structural "
                "ornaments the concept had are not reproduced.",
                [e for e in entry["touched"]],
                "Request a new design if the omitted artwork matters."))

    try:
        assessment = quality.assess(
            review=review, candidate=selected, job=job, layer_contract=contract,
            has_concept=job.get("flow") != "layout-first")
    except Exception as exc:  # noqa: BLE001 - a score failure must not discard the HTML
        log.exception("quality assessment failed")
        assessment = {"qualityScore": None, "qualityBreakdown": None, "critical": [],
                      "rubricVersion": quality.RUBRIC_VERSION,
                      "qualityReason": f"The assessment failed ({type(exc).__name__})."}
    critical = assessment.get("critical") or []
    if critical and outcome.get("publishReady"):
        outcome["publishReady"] = False         # a critical finding overrides everything
        outcome["needsReview"] = True
    fields.update({k: assessment.get(k) for k in (
        "qualityScore", "qualityBreakdown", "rubricVersion", "qualityReason",
        "qualityBand", "calibration", "provisional") if k in assessment})
    fields["criticalFindings"] = critical

    fields["timing"] = milestones.final_timing(job, render_pointers)
    fields["milestones"] = {**milestones.first_html_summary(jobs.get_job_unscoped(job_id)),
                            **fields["timing"]}
    return fields, outcome, extra


def stage_warning_filter(job):
    """Which job-wide stage warnings may be copied into a result for this job."""
    if not applies(job):
        return lambda warning: True
    return lambda warning: warning.get("stage") not in _REBUILT_STAGES
