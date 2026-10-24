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
from lib import artifacts, jobs, media, prompts, review_policy, schemas
from lib.errors import CorrectionExhausted, FinalHashMismatch, ReviewBlocked, Stage
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
    concept = artifacts.get_bytes(job_id, jobs.approved_concept(job)["key"])
    native = artifacts.get_bytes(job_id, produced[Stage.RENDER]["native"])
    feed = artifacts.get_bytes(job_id, produced[Stage.RENDER]["feed"])

    concept_review = artifacts.get_json(job_id, jobs.approved_concept(job)["reviewKey"])

    prompt = prompts.render(
        "review",
        severity_block=review_policy.severity_block(
            reference_policy_review(job, concept_review)
        ),
        copy_block=prompts.copy_block(copy_items),
        concept_review_block=_concept_review_block(concept_review),
    )
    raw = vision.analyze(
        prompt,
        (concept, native, feed),
        schema=REVIEW_SCHEMA,
        schema_name="review_v1",
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
    issues.extend(_render_problems(produced))

    render_checks = _render_checks(produced.get(Stage.RENDER, {}))
    for check in render_checks:
        if check["status"] == "missing":
            issues.append(
                _technical_issue(
                    "RENDER_CHECK_MISSING",
                    check["name"],
                    f"Render check {check['name']!r} did not run, so it cannot count as passing.",
                )
            )

    # Approval is the backend's: no blocking issue and every required render
    # check present. The model's own `passed` is recorded but never decisive in
    # either direction - a "failed" with nothing blocking behind it is an
    # unexplained veto, and minor visual differences alone must not fail a job.
    approved = not issues
    approved_concept = jobs.approved_concept(job)
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
        # And of the concept it was judged against: FINALIZE proves the selected
        # candidate is the one every stage used.
        "conceptSha256": artifacts.sha256_hex(concept),
        # The workflow's Choice needs to know whether another round is available
        # before it branches, so the rounds already used are reported here.
        "correctionAvailable": (
            int(job.get("correctionRounds", 0)) < conf.MAX_CORRECTION_ROUNDS
        ),
        # Routes to CorrectConcept. Only reported while a concept round is left:
        # otherwise CorrectConcept would end the job with REFERENCE_ADHERENCE_FAILED
        # when an ordinary HTML correction might still have fixed it.
        "conceptDefect": bool(
            not approved
            and verdict.conceptDefect
            and int(job.get("conceptRounds", 0)) < conf.MAX_CONCEPT_ROUNDS
        ),
        "conceptReviewKey": approved_concept["reviewKey"],
    }
    key = artifacts.artifact_key(job_id, Stage.REVIEW, attempt, "review.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(job_id, Stage.REVIEW, attempt, {"review": stored["key"]})
    return {**result, "review": stored["key"]}


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
    return normalized


def _technical_issue(code, affected, message, layer_id=None):
    issue = {
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
    }
    return issue


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


def _render_problems(produced):
    """Fold the renderer's mechanical findings into the verdict.

    A model looking at a screenshot cannot reliably tell that text overflowed its
    box by a few pixels; the DOM measurement can, and it is never wrong about it.
    These are technical findings, always blocking.
    """
    return [
        _technical_issue(
            problem["code"],
            problem.get("layerId") or "render",
            problem["message"],
            layer_id=problem.get("layerId"),
        )
        for problem in produced.get(Stage.RENDER, {}).get("problems", [])
    ]


def correct(job_id, attempt, asset_id=None):
    """Consume one bounded correction round.

    The bound is a DynamoDB conditional update so it holds per job rather than
    per task retry: without that, a Lambda retry would silently buy extra rounds
    and an unfixable design could loop until the workflow timed out.
    """
    jobs.set_stage(job_id, Stage.REVIEW)
    job = jobs.get_job_unscoped(job_id)
    review = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW]["review"])

    used = jobs.bump_counter(job_id, "correctionRounds", conf.MAX_CORRECTION_ROUNDS)
    if used is None:
        raise CorrectionExhausted(
            f"The design still has blocking issues after "
            f"{conf.MAX_CORRECTION_ROUNDS} correction rounds: "
            + "; ".join(i["message"] for i in review["blockingIssues"][:5])
        )

    # The next attempt re-runs the scene and separation stages against the same
    # concept, so the correction is a fresh layout rather than a patch.
    return {"round": used, "nextAttempt": int(attempt) + 1}


def finalize(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.FINALIZE)

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
    approved_concept = jobs.approved_concept(job)
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
    concept_review = artifacts.get_json(job_id, approved_concept["reviewKey"])
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

#: Stages that record which concept they worked from.
_CONCEPT_PROVENANCE = (
    Stage.EXTRACT_SCENE,
    Stage.DECORATION_OVERLAY,
    Stage.CLEAN_PHOTO,
    Stage.VALIDATE_ASSETS,
)


def _require_downstream_complete(job, produced, review, approved_concept):
    """Refuse to succeed unless every mandatory check ran and used the selected concept.

    Concept acceptance is not job success. Layer separation, asset validation,
    editable HTML, rendering and final review all have to have completed, the
    render checks must each have passed (a check that did not run is not a pass),
    and every stage must have worked from the concept that was selected.
    """
    for stage, pointer in _REQUIRED_POINTERS:
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
    for stage in _CONCEPT_PROVENANCE:
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
