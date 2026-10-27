"""VALIDATE_ASSETS for a layered-v2 job: technical checks only, then the first HTML.

The legacy validation asked a vision model, before any HTML existed, whether the overlay
was clean and whether it reconstructed the concept, and sent it back for repair (and a
photograph rebuild) when it was not. On the fixture job that meant two repairs, four
extra image calls and no HTML at all.

Here only questions with machine answers are asked before the first HTML:

* is the photograph usable (decodable, the right size, not flat);
* is the template usable (decodable, real alpha, does not hide the photograph);
* is anything another layer owns still baked into either (a violation, recorded);
* how far is the template from the concept (lib/template_check.py, recorded as findings
  that the composite review will weigh - never a reason to stop here).

A template that cannot be used is dropped and its basics drawn in CSS from the parameters
persisted in the layer plan before any asset was generated (lib/layer_plan.py); a template
with a MAJOR structural finding is dropped the same way once the single template repair
has been spent. The photograph, text and icons are never touched, no model is called, and
the flattened concept is never substituted.
"""

from __future__ import annotations

import logging

from lib import (
    artifacts, dependencies, jobs, layer_plan, repair_budget, template_check, template_policy,
    template_validate, validate_assets,
)
from lib.errors import Stage

log = logging.getLogger("layered-design.validate-v2")

#: Findings of the template comparison that the composite review is told about.
MAX_REPORTED_REGIONS = 5


def run(job_id, attempt, job):
    from lib.stages import validate as legacy
    from lib.stages.assemble import scene_key_for

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    scene = artifacts.get_json(job_id, scene_key_for(job, attempt))
    template_ref = job["artifacts"][Stage.DECORATION_OVERLAY]
    background_key = job["artifacts"][Stage.CLEAN_PHOTO]["background"]
    background = artifacts.get_bytes(job_id, background_key)
    concept = artifacts.get_bytes(job_id, jobs.approved_concept(job)["key"])
    concept_sha = artifacts.sha256_hex(concept)
    tolerance = template_policy.for_job(job)

    overlay_key = template_ref.get("decoration") if not template_ref.get("omitted") else None
    overlay = artifacts.get_bytes(job_id, overlay_key) if overlay_key else None
    plan = scene.get("layerPlan") or {}
    representation = (plan.get("template") or {}).get("representation", "overlay")

    # The photograph. Corruption is terminal: there is nothing to fall back to.
    validate_assets.validate_background(background, canvas)
    photo_problems = template_validate.photo_problems(background, scene, canvas)

    problems, alpha, check, fallback = [], None, {"checked": False}, None
    if overlay is not None:
        technical = template_validate.check_template(overlay, scene, canvas, representation)
        problems, alpha = technical["problems"], technical["alpha"]
        if technical["rgbaOk"]:
            check = template_check.compare(concept, overlay, scene, tolerance)

        reason = _fallback_reason(job, problems, check)
        if reason:
            scene, fallback = _apply_fallback(job_id, job, attempt, scene, reason, template_ref)
            overlay = None

    code_owned = readability = None
    fitted = legacy._fit_readability(job_id, scene, attempt, concept, background, overlay)
    if fitted and fitted["action"] in ("added", "kept", "refitted"):
        code_owned = fitted
    elif fitted:
        readability = fitted

    shas = {"background": artifacts.sha256_hex(background)}
    entries = {"photoValidation": legacy._entry(
        dependencies.cache_key("photoValidation", background=shas["background"]),
        {"assets": {"background": shas["background"]}}, cache_hit=False)}
    if overlay is not None:
        shas["decoration"] = artifacts.sha256_hex(overlay)
        entries["overlayValidation"] = legacy._entry(
            dependencies.cache_key(
                "overlayValidation", decoration=shas["decoration"],
                samples=dependencies.digest(scene.get("overlaySamples", [])), canvas=canvas),
            {"assets": {"decoration": shas["decoration"]}}, cache_hit=False)

    findings = _findings(problems, check)
    violations = [p for p in problems + photo_problems if p["category"] ==
                  template_validate.CATEGORY_OWNERSHIP]
    for entry in violations:
        _warn(job_id, entry)

    result = {
        "pipelineVersion": jobs.pipeline_version(job),
        "background": background_key,
        "decoration": overlay_key if overlay is not None else None,
        "alpha": alpha,
        "entries": entries,
        "policyVersion": dependencies.VALIDATION_POLICY_VERSION,
        "codeOwned": code_owned,
        "readability": readability,
        "template": {
            "representation": "css-only" if overlay is None else representation,
            "present": overlay is not None,
            "problems": problems,
            "findings": findings,
            "major": [f for f in findings if f["severity"] == "major"],
            "check": _summary(check),
            "tolerance": {k: tolerance[k] for k in ("value", "profile", "policyVersion")}
            if tolerance else None,
        },
        "violations": violations,
        "photoProblems": photo_problems,
        "fallback": fallback,
        "imageGenerationCalls": 0,
        "modelCalls": 0,
    }
    key = artifacts.artifact_key(job_id, Stage.VALIDATE_ASSETS, attempt, "validation.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(job_id, Stage.VALIDATE_ASSETS, attempt, {
        "validation": stored["key"], "conceptSha256": concept_sha, "entries": entries,
        "cacheHits": [],
    })
    return result


def _fallback_reason(job, problems, check):
    """`(code, message)` when the raster template must be dropped, else None."""
    for found in problems:
        if found["action"] == template_validate.ACTION_FALLBACK:
            return found["code"], found["message"]
    if check.get("major") and repair_budget.remaining(job, "templateImage") == 0:
        first = next(f for f in check["findings"] if f["severity"] == "major")
        return first["code"], (
            first["message"] + " The single template repair has been spent, so the raster "
            "template is replaced by the CSS fallback.")
    return None


def _apply_fallback(job_id, job, attempt, scene, reason, template_ref):
    code, message = reason
    patched, summary = layer_plan.apply_fallback(scene, message, code)
    key = artifacts.artifact_key(
        job_id, Stage.VALIDATE_ASSETS, attempt, "scene-template-fallback.json")
    stored = artifacts.put_json(key, patched)
    jobs.set_working_scene(
        job_id, {"key": stored["key"], "forAttempt": int(attempt), "baseVersion": 0})
    # The template stays stored (artifacts are append-only); the pointer says it is not used.
    jobs.record_artifacts(job_id, Stage.DECORATION_OVERLAY, attempt, {
        "omitted": True, "reason": message[:400], "code": code,
        "supersededDecoration": template_ref.get("decoration"),
        "supersededSha256": template_ref.get("sha256"),
        "conceptSha256": template_ref.get("conceptSha256"),
    })
    summary = {**summary, "scene": stored["key"], "imageGenerationCalls": 0,
               "affects": "structuralTemplate"}
    jobs.record_fallback(job_id, summary)
    jobs.record_warning(job_id, {
        "stage": Stage.VALIDATE_ASSETS, "code": "TEMPLATE_REPLACED_BY_CSS_FALLBACK",
        "category": "asset-validation", "affected": summary["templateElement"] or "",
        "impact": (
            "The generated design base could not be used (" + code + "), so the page uses "
            "plain fills and gradients drawn in CSS. Structural ornaments the concept had "
            "are not reproduced."),
        "suggestion": "Request a new design if the omitted artwork matters.",
    })
    jobs.add_metric(job_id, "templateFallbacks")
    log.info("template replaced by CSS fallback jobId=%s code=%s", job_id, code)
    return patched, summary


def _findings(problems, check):
    """The template's findings in one shape: what the review is told, and how bad."""
    out = []
    for found in problems:
        if found["action"] == template_validate.ACTION_FINDING:
            out.append({"code": found["code"], "severity": "major" if found["category"] ==
                        template_validate.CATEGORY_OBSTRUCTION else "minor",
                        "category": found["category"], "message": found["message"]})
    for found in check.get("findings") or []:
        out.append({"code": found["code"], "severity": found["severity"],
                    "category": template_validate.CATEGORY_VISUAL,
                    "message": found["message"], "region": found.get("region")})
    return out


def _summary(check):
    if not check.get("checked"):
        return {"checked": False}
    return {"checked": True, "version": check["version"], "tolerance": check["tolerance"],
            "measured": check["measured"], "major": check["major"],
            "regions": check["regions"][:MAX_REPORTED_REGIONS]}


def _warn(job_id, entry):
    jobs.record_warning(job_id, {
        "stage": Stage.VALIDATE_ASSETS, "code": entry["code"], "category": "asset-validation",
        "affected": ",".join(entry["affected"]), "impact": entry["message"],
        "suggestion": "The layer contract is violated for these elements; review before "
                      "publishing.",
    })
