"""REVIEW, CORRECT and FINALIZE.

REVIEW judges the real rendered pixels against the APPROVED concept, at both
native size and feed size, whatever the designFreedom. The reference-policy review
of that concept is loaded as settled evidence rather than re-litigated here.

Mechanical render problems from RENDER are merged into the verdict rather than
replacing it, because the two find different things. The renderer knows a text
box overflowed by 3 pixels; only vision knows white text landed on a white panel.

FINALIZE marks the job SUCCEEDED only after every artifact is durable and the
delivered HTML hash still matches what ASSEMBLE produced (SPEC section 4).
"""

from __future__ import annotations

import conf
from lib import (
    artifacts, content_check, design_state, telemetry, jobs, media, prompts, repair_routing, review_policy,
    scene_patch, schemas,
)
from lib import candidates as candidates_module
from lib.errors import (
    CorrectionExhausted, FinalHashMismatch, MandatoryContentMissing, RepairNoProgress,
    RepairRouteUnresolved, ReviewBlocked, Stage,
)
from lib.providers import vision

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

_ISSUE = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "code", "layerId", "assetId", "message", "category", "severity",
        "affected", "impact", "suggestion", "region",
        "repairTarget", "affectedElementIds", "requiresImageGeneration", "constraints",
    ],
    "properties": {
        "code": {"type": "string"},
        # Nullable rather than omitted: strict mode requires every
        # property to appear in `required`.
        "layerId": {"type": ["string", "null"]},
        "assetId": {"type": ["string", "null"]},
        "message": {"type": "string"},
        "category": {"type": "string", "enum": list(schemas.ISSUE_CATEGORIES)},
        "severity": {"type": "string", "enum": list(schemas.ISSUE_SEVERITIES)},
        "affected": {"type": "string"},
        "impact": {"type": "string"},
        "suggestion": {"type": ["string", "null"]},
        "region": {"anyOf": [{"type": "null"}, _BOX]},
        # Structured repair routing (lib/repair_routing.py).
        "repairTarget": {"type": "string", "enum": list(repair_routing.TARGETS)},
        "affectedElementIds": {"type": "array", "items": {"type": "string"}},
        "requiresImageGeneration": {"type": "boolean"},
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["elementId", "property", "value"],
                "properties": {
                    "elementId": {"type": "string"},
                    "property": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["passed", "blockingIssues", "warnings", "suggestions", "conceptDefect"],
    "properties": {
        "passed": {"type": "boolean"},
        "blockingIssues": {"type": "array", "items": _ISSUE},
        "warnings": {"type": "array", "items": _ISSUE},
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "conceptDefect": {"type": "boolean"},
    },
}


#: Every check REVIEW requires before it may approve. A name missing from
#: RENDER's `checksPerformed` is reported as "missing", never as passing.
REQUIRED_RENDER_CHECKS = (
    "imagesDecoded",
    "fontsResolved",
    "textFit",
    "emptyText",
    "bounds",
    "uniqueIds",
    "feedCapture",
)

_CHECK_FOR_PROBLEM = {
    "TEXT_OVERFLOW": "textFit",
    "EMPTY_TEXT": "emptyText",
    "OUT_OF_BOUNDS": "bounds",
    "DUPLICATE_ID": "uniqueIds",
}


def _render_checks(rendered):
    """Per-check status: passed, failed or missing."""
    performed = set(rendered.get("checksPerformed") or [])
    failed = {
        _CHECK_FOR_PROBLEM.get(problem["code"])
        for problem in rendered.get("problems", [])
    }
    report = []
    for name in REQUIRED_RENDER_CHECKS:
        if name not in performed:
            status = "missing"
        elif name in failed:
            status = "failed"
        else:
            status = "passed"
        report.append({"name": name, "status": status})
    return report


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.REVIEW)

    produced = job["artifacts"]
    copy_items = artifacts.get_json(job_id, produced[Stage.PREPARE_COPY]["copy"])["items"]
    basis = _basis(job_id, job)
    native = artifacts.get_bytes(job_id, produced[Stage.RENDER]["native"])
    feed = artifacts.get_bytes(job_id, produced[Stage.RENDER]["feed"])
    # The scene and document THIS attempt assembled (text fitting may have changed
    # the scene), which is what REVIEW and every repair must reason about.
    scene = artifacts.get_json(job_id, produced[Stage.ASSEMBLE]["scene"])
    html = artifacts.get_bytes(job_id, produced[Stage.ASSEMBLE]["html"]).decode("utf-8")

    prompt = prompts.render(
        basis["promptName"],
        severity_block=review_policy.severity_block(
            reference_policy_review(job, basis["conceptReview"])
        ),
        copy_block=prompts.copy_block(copy_items),
        concept_review_block=basis["block"],
        design_block=repair_routing.describe_design(scene, produced[Stage.ASSEMBLE]["assets"]),
    )
    raw = vision.analyze(
        prompt,
        (basis["image"], native, feed),
        schema=REVIEW_SCHEMA,
        schema_name="review_v2",
        model=conf.REVIEW_MODEL,
        operation_id=f"{job_id}:review:{attempt}",
        detail="original",
    )

    verdict = schemas.parse_review(raw)
    # Severity is applied by the backend (lib/review_policy.py), not trusted from
    # the model: content and technical findings are always blocking, and a visual
    # one blocks only when it names what it harms. A finding listed under
    # `warnings`, or marked as one, never blocks.
    reported = [
        _final_issue(i) for i in (*verdict.blockingIssues, *verdict.warnings)
    ]
    issues, _ = review_policy.split_issues(reported)
    warnings = [i for i in reported if i["severity"] != review_policy.BLOCKING]
    issues.extend(_render_problems(produced, scene, job))

    # Mandatory text is verified in code on every candidate, not left to the
    # model: a repair that touched typography or layout is exactly the change that
    # can quietly drop a line.
    content_issues = content_check.check(scene, copy_items, html)
    known = {(i["code"], i["affected"]) for i in issues}
    issues.extend(i for i in content_issues if (i["code"], i["affected"]) not in known)
    mandatory_ok = not content_issues

    render_checks = _render_checks(produced.get(Stage.RENDER, {}))
    for check in render_checks:
        if check["status"] == "missing":
            issues.append(
                _technical_issue(
                    "RENDER_CHECK_MISSING",
                    check["name"],
                    f"Render check {check['name']!r} did not run, so it cannot count as passing.",
                    target="layout",
                    rerender=True,
                )
            )

    # Approval is the backend's: no blocking issue and every required render
    # check present. The model's own `passed` is recorded but never decisive in
    # either direction - a "failed" with nothing blocking behind it is an
    # unexplained veto, and minor visual differences alone must not fail a job.
    approved = not issues
    routing = _route(job, scene, copy_items, produced, issues, verdict, approved, attempt)
    result = {
        "approved": approved,
        "modelPassed": verdict.passed,
        "blockingIssues": issues,
        "warnings": warnings,
        "suggestions": list(verdict.suggestions),
        "renderChecks": render_checks,
        "reviewStrictness": job.get("reviewStrictness", review_policy.DEFAULT_STRICTNESS),
        # Identity of what was judged, so FINALIZE can prove it ships the same
        # document and render rather than a later one.
        "htmlHash": produced[Stage.ASSEMBLE]["htmlHash"],
        "renderKey": produced[Stage.RENDER]["native"],
        "renderSha256": artifacts.sha256_hex(native),
        # And of what it was judged against: FINALIZE proves the selected basis
        # is the one every stage used.
        "conceptSha256": basis["sha256"],
        "mandatoryContentOk": mandatory_ok,
        # How the findings were routed, and so what happens next.
        "routing": routing,
        "route": routing["route"],
        # Kept for the workflow and existing callers: true exactly when the review
        # justified regenerating the concept and a concept round remains.
        "conceptDefect": routing["route"] == repair_routing.ROUTE_CONCEPT,
        # Retained for compatibility: a Choice used to read it before routing.
        "correctionAvailable": routing["route"] in (
            repair_routing.ROUTE_LIGHT, repair_routing.ROUTE_ASSET,
        ),
        "conceptReviewKey": basis["reviewKey"],
    }
    key = artifacts.artifact_key(job_id, Stage.REVIEW, attempt, "review.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(job_id, Stage.REVIEW, attempt, {"review": stored["key"]})
    _record_candidate(job_id, job, attempt, result, stored["key"], produced, issues, warnings)
    jobs.record_route(job_id, attempt, {
        "route": routing["route"],
        "reason": routing["reason"],
        "categories": routing.get("categories", []),
        "blocking": [
            {"code": i["code"], "target": i.get("repairTarget"), "affected": i["affected"]}
            for i in issues
        ],
        "imageGenerationRequired": routing["imageGenerationRequired"],
    })
    return {**result, "review": stored["key"]}


def _basis(job_id, job):
    """What the render is judged against: the approved concept, or the layout plan.

    The legacy flow judges against the concept image it separated into layers. The
    layout-first flow has no concept: the render is judged against the reference's
    structure and the layout that was planned, with the reference's subject matter
    explicitly out of scope.
    """
    if job.get("flow") == "layout-first":
        return layout_basis(job_id, job)
    approved = jobs.approved_concept(job)
    concept_review = artifacts.get_json(job_id, approved["reviewKey"])
    return {
        "image": artifacts.get_bytes(job_id, approved["key"]),
        "sha256": approved["sha256"],
        "conceptReview": concept_review,
        "block": _concept_review_block(concept_review),
        "reviewKey": approved["reviewKey"],
        "promptName": "review",
    }


def layout_basis(job_id, job):
    """The review basis for a layout-first job (see lib/stages/plan_layout.py)."""
    from lib.stages import plan_layout

    return plan_layout.review_basis(job_id, job)


def _route(job, scene, copy_items, produced, issues, verdict, approved, attempt):
    """Validate each blocking issue's classification and decide the route."""
    if approved:
        return {
            "route": repair_routing.ROUTE_APPROVE, "reason": "approved", "categories": [],
            "issues": [], "unresolved": [], "imageGenerationRequired": False,
        }
    ctx = {
        "scene": scene,
        "copyKeys": {item["contentKey"] for item in copy_items},
        "assetForLayer": repair_routing.asset_for_layers(scene),
        "assetIds": {e["assetId"] for e in produced[Stage.ASSEMBLE]["assets"]},
        "fonts": conf.APPROVED_FONT_FAMILIES,
        # A layout-first design has no concept to regenerate, so the review cannot
        # justify that route however it answers.
        "conceptJustified": bool(verdict.conceptDefect) and job.get("flow") != "layout-first",
    }
    counts = jobs.repair_counts(job)
    counts["concept"] = int(job.get("conceptRounds", 0) or 0)
    candidates = jobs.design_candidates(job)
    applied = [c["patchDigest"] for c in candidates if c.get("patchDigest") and c.get("route") == repair_routing.ROUTE_LIGHT]
    routing = repair_routing.plan_route(
        issues, ctx, counts=counts, limits=conf.MAX_REPAIRS, applied_patches=applied
    )
    # No meaningful improvement: the previous candidate was repaired and the
    # findings are about the same things. Repeating the repair would not help.
    now = repair_routing.fingerprint(issues)
    previous = candidates[-1] if candidates else None
    repaired = (
        previous is not None
        and previous.get("route") in (repair_routing.ROUTE_LIGHT, repair_routing.ROUTE_ASSET)
    )
    stalled = repaired and previous.get("fingerprint") == now
    # A repair that made the design worse is not built on: it has more blocking
    # findings than the candidate it started from, so more repairing from here
    # would be compounding a regression. The better candidate stays available.
    regressed = repaired and len(issues) > int(previous.get("blockingCount", 0))
    if routing["route"] in (repair_routing.ROUTE_LIGHT, repair_routing.ROUTE_ASSET) and (
        stalled or regressed
    ):
        routing = {
            **routing,
            "route": repair_routing.ROUTE_NO_PROGRESS,
            "reason": (
                "the previous repair left the same findings on the same elements"
                if stalled else
                "the previous repair introduced more blocking findings than it fixed"
            ),
            "imageGenerationRequired": False,
        }
    routing["fingerprint"] = now
    return routing


def _record_candidate(job_id, job, attempt, result, review_key, produced, issues, warnings):
    """Keep this attempt as a candidate: its snapshot, render and review."""
    assembled = produced[Stage.ASSEMBLE]
    routing = result["routing"]
    jobs.record_design_candidate(
        job_id,
        attempt,
        {
            "attempt": int(attempt),
            "approved": result["approved"],
            "mandatoryContentOk": result["mandatoryContentOk"],
            "blockingCount": len(issues),
            "warningCount": len(warnings),
            "fingerprint": routing.get("fingerprint"),
            "route": routing["route"],
            "patchDigest": routing.get("patchDigest"),
            "snapshotVersion": (assembled.get("snapshot") or {}).get("version"),
            "snapshotKey": (assembled.get("snapshot") or {}).get("key"),
            "reviewKey": review_key,
            "htmlHash": assembled["htmlHash"],
            "pointers": {
                Stage.ASSEMBLE: assembled,
                Stage.RENDER: produced[Stage.RENDER],
                Stage.REVIEW: {"review": review_key},
            },
        },
    )


def _concept_review_block(concept_review):
    """Summarise the settled reference-policy review for the final reviewer."""
    lines = [
        f"- mode: {concept_review['mode']} (designFreedom {concept_review['designFreedom']})",
        f"- passed: {concept_review['approved']}",
        "- documented exceptions:",
        prompts.bullet_list(concept_review.get("documentedExceptions", [])),
    ]
    return "\n".join(lines)


def _final_issue(issue):
    """Normalize one final-review finding, keeping the layer and asset it names."""
    raw = issue.model_dump(mode="json")
    raw["affected"] = raw.get("affected") or raw.get("layerId") or raw.get("assetId")
    raw["impact"] = raw.get("impact") or raw.get("message")
    if raw.get("severity") is None:
        # An older-shaped finding carries none of the structured fields and was
        # always blocking; it must not be downgraded for lacking them.
        raw["affected"] = raw["affected"] or raw["code"]
    normalized = review_policy.normalize_issue(raw)
    normalized["layerId"] = raw.get("layerId")
    normalized["assetId"] = raw.get("assetId")
    # Routing fields are carried verbatim: validating them is routing's job, and
    # repairing them here would turn an untrustworthy classification into a
    # trustworthy-looking one.
    normalized["repairTarget"] = raw.get("repairTarget")
    normalized["affectedElementIds"] = list(raw.get("affectedElementIds") or [])
    normalized["requiresImageGeneration"] = raw.get("requiresImageGeneration")
    normalized["constraints"] = list(raw.get("constraints") or [])
    return normalized


def _technical_issue(code, affected, message, layer_id=None, *, target=None,
                     element_ids=(), constraints=(), rerender=False):
    """A mechanical finding, with the routing the backend itself knows.

    The renderer and the content check measured these, so the repair target and
    the constraints are decided here rather than asked of a model. `target` is
    None for a finding that has no bounded repair, which routes as unresolved.
    """
    return {
        "code": code,
        "category": review_policy.TECHNICAL,
        "severity": review_policy.BLOCKING,
        "affected": affected,
        "impact": message,
        "suggestion": None,
        "region": None,
        "message": message,
        "layerId": layer_id,
        "assetId": None,
        "repairTarget": target,
        "affectedElementIds": list(element_ids),
        "requiresImageGeneration": False if target else None,
        "constraints": list(constraints),
        "rerender": rerender,
    }


def reference_policy_review(job, concept_review):
    """The review policy the job was started under, for the final reviewer.

    Read from the job's selected concept review, which carries the policy it was
    judged under; falls back to deriving it from the job's recorded strictness.
    """
    return review_policy.derive_review_policy(
        concept_review.get("reviewStrictness")
        or job.get("reviewStrictness")
        or review_policy.DEFAULT_STRICTNESS
    )


def _render_problems(produced, scene, job):
    """Fold the renderer's mechanical findings into the verdict, routed.

    A model looking at a screenshot cannot reliably tell that text overflowed its
    box by a few pixels; the DOM measurement can, and it is never wrong about it.
    These are technical findings, always blocking - and each carries the repair
    the measurement implies, so none of them needs a model to be routed.
    """
    from lib import assemble_html

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    element_to_layer = {v: k for k, v in assemble_html.element_ids(scene).items()}
    element_to_layer.update({v: k for k, v in assemble_html.shape_element_ids(scene).items()})
    layers = {l["id"]: l for l in scene["textLayers"]}
    issues = []
    for problem in produced.get(Stage.RENDER, {}).get("problems", []):
        element = problem.get("layerId")
        layer_id = element_to_layer.get(element)
        code = problem["code"]
        target, constraints, ids = None, [], ([layer_id] if layer_id else [])
        if code == "TEXT_OVERFLOW" and layer_id in layers:
            fixed = repair_routing.overflow_constraints(
                layers[layer_id], problem.get("measured"), canvas
            )
            if fixed:
                target, constraints = fixed
        elif code == "EMPTY_TEXT" and layer_id in layers:
            target, ids = "content", [layers[layer_id]["contentKey"]]
        elif code == "OUT_OF_BOUNDS" and layer_id:
            kind, layer = scene_patch.layer_kind(scene, layer_id)
            clamp = repair_routing.clamp_constraints(layer, canvas) if layer else []
            if clamp and kind in ("text", "shape"):
                target, constraints = "layout", clamp
        issues.append(
            _technical_issue(
                code,
                layer_id or element or "render",
                problem["message"],
                layer_id=problem.get("layerId"),
                target=target,
                element_ids=ids,
                constraints=constraints,
            )
        )
    return issues


def correct(job_id, attempt, asset_id=None):
    """End the repair loop: nothing routable remains, so fail precisely.

    The workflow reaches this state only when the review neither approved the
    design nor routed it to a repair that can still run. It no longer regenerates
    anything: regeneration is the expensive path, and it had been the answer to
    every finding whether or not an image was what was wrong. What it does is name
    why the loop ended and preserve the best candidate so far.
    """
    jobs.set_stage(job_id, Stage.REVIEW)
    job = jobs.get_job_unscoped(job_id)
    review = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW]["review"])
    routing = review.get("routing") or {}
    route = routing.get("route") or repair_routing.ROUTE_UNRESOLVED
    candidates = jobs.design_candidates(job)
    best = candidates_module.repair_base(candidates)

    summary = "; ".join(i["message"] for i in review["blockingIssues"][:5])
    preserved = candidates_module.describe_preserved(candidates)
    diagnostics = {
        "route": route,
        "reason": routing.get("reason"),
        "unresolved": routing.get("unresolved", []),
        "preservedCandidate": (
            {"attempt": best["attempt"], "snapshotKey": best.get("snapshotKey"),
             "blockingCount": best.get("blockingCount"),
             "mandatoryContentOk": best.get("mandatoryContentOk")}
            if best else None
        ),
        "repairCounts": jobs.repair_counts(job),
    }
    jobs.record_diagnostics(job_id, diagnostics)

    # Missing mandatory text outranks every other reason: the closest candidate
    # cannot be delivered however the loop ended.
    if best is not None and not best.get("mandatoryContentOk"):
        error = MandatoryContentMissing(
            "Mandatory approved text is missing from every candidate: "
            + summary + f". {preserved}."
        )
    elif route == repair_routing.ROUTE_NO_PROGRESS:
        error = RepairNoProgress(
            f"The repair did not improve the design ({routing.get('reason')}): "
            f"{summary}. {preserved}."
        )
    elif route == repair_routing.ROUTE_EXHAUSTED:
        error = CorrectionExhausted(
            f"The design still has blocking issues after the bounded repairs "
            f"({routing.get('reason')}): {summary}. {preserved}."
        )
    else:
        reasons = "; ".join(
            f"{u['code']}: {', '.join(u['problems'])}" for u in routing.get("unresolved", [])[:3]
        )
        error = RepairRouteUnresolved(
            "The review's findings could not be routed to a bounded repair"
            f" ({reasons or routing.get('reason')}), so no image was generated: "
            f"{summary}. {preserved}."
        )
    error.details = diagnostics
    raise error


def finalize(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.FINALIZE)

    # Deliver the best ELIGIBLE candidate, not whichever was assembled last. A
    # candidate qualifies only if it was approved and carries every mandatory
    # text; the artifacts that describe it (document, render, review) are read
    # from its own record, so nothing from a later, rejected attempt can leak in.
    candidates = jobs.design_candidates(job)
    selected = None
    if candidates:
        selected = candidates_module.select_deliverable(candidates)
        if selected is None:
            best = candidates_module.repair_base(candidates)
            if best is not None and not best.get("mandatoryContentOk"):
                raise MandatoryContentMissing(
                    "No candidate carries all of the approved text, so none can be "
                    f"delivered: {candidates_module.describe_preserved(candidates)}."
                )
            raise ReviewBlocked(
                "No candidate was approved, so none can be delivered: "
                + candidates_module.describe_preserved(candidates)
            )
        produced = {**job["artifacts"], **selected["pointers"]}
        # Edits are made against what was delivered, so that becomes the current
        # design (a new version when it is not the last one assembled).
        design_state.deliver(job_id, job, selected)
        jobs.select_design(
            job_id,
            {
                "attempt": selected["attempt"],
                "snapshotKey": selected.get("snapshotKey"),
                "htmlHash": selected["htmlHash"],
                "reason": candidates_module.selection_reason(selected, candidates),
            },
        )
    else:
        produced = job["artifacts"]
    review = artifacts.get_json(job_id, produced[Stage.REVIEW]["review"])
    if not review["approved"]:
        raise ReviewBlocked(
            "The design was finalized while review was still blocking: "
            + "; ".join(i["message"] for i in review["blockingIssues"][:5])
        )

    # Reaching FINALIZE is not enough: success needs every mandatory downstream
    # check to have actually completed, and every stage to have worked from the
    # one selected concept.
    approved_concept = _finalize_basis(job_id, job)
    _require_downstream_complete(job, produced, review, approved_concept)

    html_key = produced[Stage.ASSEMBLE]["html"]
    html = artifacts.get_bytes(job_id, html_key)
    html_hash = artifacts.sha256_hex(html)
    if html_hash != produced[Stage.ASSEMBLE]["htmlHash"]:
        raise FinalHashMismatch(
            "The stored HTML does not match what ASSEMBLE produced; the "
            "deliverable may have been modified."
        )

    # ASSEMBLE's scene describes the document being delivered; EXTRACT_SCENE's
    # describes what was asked for before text fitting adjusted it.
    scene = artifacts.get_json(job_id, produced[Stage.ASSEMBLE]["scene"])

    native = artifacts.get_bytes(job_id, produced[Stage.RENDER]["native"])

    # Review judged one specific render of one specific document. If either
    # differs from what is about to ship, the approval does not apply to it.
    # `.get` is deliberate: a review written without these fields fails closed.
    if (
        review.get("htmlHash") != html_hash
        or review.get("renderSha256") != artifacts.sha256_hex(native)
    ):
        raise FinalHashMismatch(
            "The delivered HTML or preview is not the version that was reviewed."
        )

    # The preview is the native render published to a durable URL. It joins the
    # manifest so its hash is covered too.
    preview = media.publish(native, job_id=job_id, asset_id="preview")
    manifest = list(produced[Stage.ASSEMBLE]["assets"]) + [
        {
            "assetId": "preview",
            "key": produced[Stage.RENDER]["native"],
            "sha256": artifacts.sha256_hex(native),
            "url": preview["url"],
        }
    ]
    manifest_hash = artifacts.canonical_manifest_hash(manifest)

    result_key = artifacts.artifact_key(job_id, Stage.FINALIZE, attempt, "result.json")

    # The QA record is built here rather than in REVIEW because assetManifestHash
    # is final only now: it includes the published preview.
    concept_review = approved_concept["review"]
    # Non-blocking findings travel with the successful result. They never
    # blocked anything; they are reported so a caller can see what differs.
    warnings = _warnings(concept_review, review)
    selection = {
        "attempt": approved_concept["attempt"],
        "key": approved_concept["key"],
        "sha256": approved_concept["sha256"],
        "policyVersion": approved_concept.get("policyVersion", job["policyVersion"]),
        "selectedBy": approved_concept.get("selectedBy"),
        "reason": approved_concept.get("selectionReason"),
        "selectionKey": approved_concept.get("selectionKey"),
    }
    design_selection = (
        {
            "attempt": selected["attempt"],
            "snapshotKey": selected.get("snapshotKey"),
            "reason": candidates_module.selection_reason(selected, candidates),
            "candidates": [
                {"attempt": c["attempt"], "approved": c["approved"],
                 "blockingCount": c["blockingCount"], "warningCount": c["warningCount"],
                 "mandatoryContentOk": c["mandatoryContentOk"], "route": c.get("route")}
                for c in candidates
            ],
        }
        if selected else None
    )
    qa = {
        "passed": review["approved"],
        "designFreedom": int(job["designFreedom"]),
        "reviewStrictness": job.get("reviewStrictness", review_policy.DEFAULT_STRICTNESS),
        "policyVersion": job["policyVersion"],
        "conceptRounds": int(job.get("conceptRounds", 0)),
        "conceptSelection": selection,
        "conceptReview": {
            "key": approved_concept["reviewKey"],
            "passed": concept_review["approved"],
            "mode": concept_review["mode"],
            "documentedExceptions": concept_review["documentedExceptions"],
            "requiredChecks": concept_review.get("requiredChecks", []),
        },
        "blockingIssues": review["blockingIssues"],
        "warnings": warnings,
        "suggestions": review["suggestions"],
        "renderChecks": review["renderChecks"],
        "renderKey": review["renderKey"],
        "renderSha256": review["renderSha256"],
        "htmlHash": html_hash,
        "assetManifestHash": manifest_hash,
        "designSelection": design_selection,
        "repairRoutes": telemetry.routes(job),
    }

    result = {
        "jobId": job_id,
        "status": "SUCCEEDED",
        "html": html.decode("utf-8"),
        "htmlHash": html_hash,
        "assets": [
            {"assetId": e["assetId"], "url": e["url"], "sha256": e["sha256"]}
            for e in manifest
        ],
        "assetManifestHash": manifest_hash,
        "layerMap": _layer_map(scene),
        # `preview` predates `previewUrl` and is kept for existing callers.
        "preview": preview["url"],
        "canvas": scene["canvas"],
        "designFreedom": int(job["designFreedom"]),
        "reviewStrictness": job.get("reviewStrictness", review_policy.DEFAULT_STRICTNESS),
        "policyVersion": job["policyVersion"],
        # Additive and backward compatible: absent keys mean nothing to report.
        "warnings": warnings,
        "conceptSelection": selection,
        "designSelection": design_selection,
        # What it cost to produce: time per stage, image-generation calls, cache
        # hits and the route of every repair. Additive; nothing else changed.
        "metrics": telemetry.summary(jobs.get_job_unscoped(job_id)),
        # References to objects the pipeline already stores. Compact transports
        # return these instead of the HTML itself.
        "resultKey": result_key,
        "htmlKey": html_key,
        "previewKey": produced[Stage.RENDER]["native"],
        "previewUrl": preview["url"],
        "qaKey": produced[Stage.REVIEW]["review"],
        "qa": qa,
    }

    stored = artifacts.put_json(result_key, result)

    # A saved design must outlive the temporary-artifact expiry. Retag before
    # marking success so a job is never SUCCEEDED with expiring dependencies.
    artifacts.retain(
        job_id,
        {
            html_key,
            produced[Stage.ASSEMBLE]["scene"],
            produced[Stage.RENDER]["native"],
            produced[Stage.REVIEW]["review"],
            approved_concept["reviewKey"],
            approved_concept["key"],
            *([approved_concept["selectionKey"]] if approved_concept.get("selectionKey") else []),
            *([selected["snapshotKey"]] if selected and selected.get("snapshotKey") else []),
            produced[Stage.ANALYZE_REFERENCE]["policy"],
            job["requestKey"],
            stored["key"],
            *[entry["key"] for entry in manifest],
        },
    )

    # Only now is the job complete: every artifact above is durable.
    jobs.mark_succeeded(job_id, stored["key"], html_hash, manifest_hash)
    return {"result": stored["key"], "htmlHash": html_hash}


#: Pointers a successful job must hold, by stage. A missing one means a mandatory
#: stage never completed; it is never read as "nothing to check".
_REQUIRED_POINTERS = (
    (Stage.EXTRACT_SCENE, "scene"),
    (Stage.DECORATION_OVERLAY, "decoration"),
    (Stage.CLEAN_PHOTO, "background"),
    (Stage.VALIDATE_ASSETS, "validation"),
    (Stage.ASSEMBLE, "html"),
    (Stage.RENDER, "native"),
    (Stage.RENDER, "feed"),
    (Stage.REVIEW, "review"),
)

#: The layout-first flow has no concept and no overlay: the layout plan and the
#: generated photograph are what the later stages are required to have used.
_REQUIRED_POINTERS_LAYOUT_FIRST = (
    (Stage.PLAN_LAYOUT, "scene"),
    (Stage.GENERATE_PHOTO, "background"),
    (Stage.VALIDATE_ASSETS, "validation"),
    (Stage.ASSEMBLE, "html"),
    (Stage.RENDER, "native"),
    (Stage.RENDER, "feed"),
    (Stage.REVIEW, "review"),
)

#: Stages that record which concept they worked from.
_CONCEPT_PROVENANCE = (
    Stage.EXTRACT_SCENE,
    Stage.DECORATION_OVERLAY,
    Stage.CLEAN_PHOTO,
    Stage.VALIDATE_ASSETS,
)

_LAYOUT_PROVENANCE = (Stage.GENERATE_PHOTO, Stage.VALIDATE_ASSETS)


def _finalize_basis(job_id, job):
    """The frozen thing every stage worked from, with its settled review.

    For the legacy flow that is the approved concept. For the layout-first flow it
    is the layout plan, whose "review" is the plan's own validation record.
    """
    if job.get("flow") == "layout-first":
        from lib.stages import plan_layout

        return plan_layout.finalize_basis(job_id, job)
    approved = dict(jobs.approved_concept(job))
    approved["review"] = artifacts.get_json(job_id, approved["reviewKey"])
    return approved


def _require_downstream_complete(job, produced, review, approved_concept):
    """Refuse to succeed unless every mandatory check ran and used the selected basis.

    Concept acceptance is not job success. Layer separation, asset validation,
    editable HTML, rendering and final review all have to have completed, the
    render checks must each have passed (a check that did not run is not a pass),
    and every stage must have worked from the concept (or layout plan) that was
    selected.
    """
    layout_first = job.get("flow") == "layout-first"
    for stage, pointer in (_REQUIRED_POINTERS_LAYOUT_FIRST if layout_first else _REQUIRED_POINTERS):
        if not (produced.get(stage) or {}).get(pointer):
            raise ReviewBlocked(
                f"Mandatory stage output {stage}.{pointer} is missing, so the "
                f"design cannot be reported as succeeded."
            )
    not_passed = [c["name"] for c in review.get("renderChecks", []) if c["status"] != "passed"]
    required = {name for name in REQUIRED_RENDER_CHECKS}
    reported = {c["name"] for c in review.get("renderChecks", [])}
    if not_passed or required - reported:
        raise ReviewBlocked(
            "Required render checks did not pass: "
            + ", ".join(sorted(set(not_passed) | (required - reported)))
        )
    selected = approved_concept["sha256"]
    for stage in (_LAYOUT_PROVENANCE if layout_first else _CONCEPT_PROVENANCE):
        used = (produced.get(stage) or {}).get("conceptSha256")
        if used != selected:
            raise FinalHashMismatch(
                f"{stage} did not work from the selected concept; the layers may "
                f"not describe the concept that was approved."
            )
    if review.get("conceptSha256") != selected:
        raise FinalHashMismatch(
            "The final review judged a different concept from the selected one."
        )
    # Mandatory text is verified one last time on the document being delivered.
    if not review.get("mandatoryContentOk", False):
        raise MandatoryContentMissing(
            "The delivered design is missing approved text, so it cannot be "
            "reported as succeeded."
        )


def _warnings(concept_review, review):
    """Every non-blocking finding, tagged with the review that raised it."""
    found = []
    for source, issues in (
        (Stage.REVIEW_CONCEPT, concept_review.get("warnings", [])),
        (Stage.REVIEW, review.get("warnings", [])),
    ):
        for issue in issues:
            found.append(
                {
                    "stage": source,
                    "code": issue["code"],
                    "category": issue.get("category"),
                    "affected": issue.get("affected"),
                    "impact": issue.get("impact"),
                    "suggestion": issue.get("suggestion"),
                }
            )
    return found


def _layer_map(scene):
    """The caller-facing description of what is in which layer (SPEC 2.5)."""
    layers = [
        {
            "id": layer["id"],
            "kind": layer["type"],
            "assetId": layer.get("assetId"),
            "zIndex": layer["zIndex"],
            "box": {
                "x": layer["x"],
                "y": layer["y"],
                "width": layer["width"],
                "height": layer["height"],
            },
        }
        for layer in scene["imageLayers"]
    ]
    layers.extend(
        {
            "id": layer["id"],
            "kind": "text",
            "role": layer["role"],
            "contentKey": layer["contentKey"],
            "zIndex": layer["zIndex"],
            "box": {
                "x": layer["x"],
                "y": layer["y"],
                "width": layer["width"],
                "height": layer["height"],
            },
        }
        for layer in scene["textLayers"]
    )
    return sorted(layers, key=lambda item: (item["zIndex"], item["id"]))
