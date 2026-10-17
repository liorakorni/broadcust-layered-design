"""REVIEW, CORRECT and FINALIZE.

REVIEW judges the real rendered pixels against the concept, at both native size
and feed size. The concept is a guide, not a target: a different but
well-executed composition passes. Only genuine defects block.

Mechanical render problems from RENDER are merged into the verdict rather than
replacing it, because the two find different things. The renderer knows a text
box overflowed by 3 pixels; only vision knows white text landed on a white panel.

FINALIZE marks the job SUCCEEDED only after every artifact is durable and the
delivered HTML hash still matches what ASSEMBLE produced (SPEC section 4).
"""

from __future__ import annotations

import conf
from lib import artifacts, jobs, media, prompts, schemas
from lib.errors import CorrectionExhausted, FinalHashMismatch, ReviewBlocked, Stage
from lib.providers import vision

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["passed", "blockingIssues", "suggestions"],
    "properties": {
        "passed": {"type": "boolean"},
        "blockingIssues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "layerId", "assetId", "message"],
                "properties": {
                    "code": {"type": "string"},
                    # Nullable rather than omitted: strict mode requires every
                    # property to appear in `required`.
                    "layerId": {"type": ["string", "null"]},
                    "assetId": {"type": ["string", "null"]},
                    "message": {"type": "string"},
                },
            },
        },
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
}


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.REVIEW)

    produced = job["artifacts"]
    copy_items = artifacts.get_json(job_id, produced[Stage.PREPARE_COPY]["copy"])["items"]
    concept = artifacts.get_bytes(job_id, produced[Stage.CONCEPT]["concept"])
    native = artifacts.get_bytes(job_id, produced[Stage.RENDER]["native"])
    feed = artifacts.get_bytes(job_id, produced[Stage.RENDER]["feed"])

    prompt = prompts.render("review", copy_block=prompts.copy_block(copy_items))
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
    issues = [issue.model_dump(mode="json") for issue in verdict.blockingIssues]
    issues.extend(_render_problems(produced))

    result = {
        "approved": verdict.passed and not issues,
        "blockingIssues": issues,
        "suggestions": list(verdict.suggestions),
        # The workflow's Choice needs to know whether another round is available
        # before it branches, so the rounds already used are reported here.
        "correctionAvailable": (
            int(job.get("correctionRounds", 0)) < conf.MAX_CORRECTION_ROUNDS
        ),
    }
    key = artifacts.artifact_key(job_id, Stage.REVIEW, attempt, "review.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(job_id, Stage.REVIEW, attempt, {"review": stored["key"]})
    return {**result, "review": stored["key"]}


def _render_problems(produced):
    """Fold the renderer's mechanical findings into the verdict.

    A model looking at a screenshot cannot reliably tell that text overflowed its
    box by a few pixels; the DOM measurement can, and it is never wrong about it.
    """
    return [
        {
            "code": problem["code"],
            "layerId": problem.get("layerId"),
            "assetId": None,
            "message": problem["message"],
        }
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

    html_key = produced[Stage.ASSEMBLE]["html"]
    html = artifacts.get_bytes(job_id, html_key)
    html_hash = artifacts.sha256_hex(html)
    if html_hash != produced[Stage.ASSEMBLE]["htmlHash"]:
        raise FinalHashMismatch(
            "The stored HTML does not match what ASSEMBLE produced; the "
            "deliverable may have been modified."
        )

    scene = artifacts.get_json(job_id, produced[Stage.EXTRACT_SCENE]["scene"])

    # The preview is the native render published to a durable URL. It joins the
    # manifest so its hash is covered too.
    native = artifacts.get_bytes(job_id, produced[Stage.RENDER]["native"])
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
        "preview": preview["url"],
        "canvas": scene["canvas"],
    }

    key = artifacts.artifact_key(job_id, Stage.FINALIZE, attempt, "result.json")
    stored = artifacts.put_json(key, result)

    # Only now is the job complete: every artifact above is durable.
    jobs.mark_succeeded(job_id, stored["key"], html_hash, manifest_hash)
    return {"result": stored["key"], "htmlHash": html_hash}


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
