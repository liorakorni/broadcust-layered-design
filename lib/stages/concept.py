"""CONCEPT: create the internal composition that separation works from.

The concept is NOT the deliverable (SPEC section 4). It may contain the target
text, because its job is to show the layout engine what the design should look
like. The customer never sees it.

The exact bytes are persisted immutably before returning, because CLEAN_PHOTO
and DECORATION_OVERLAY must both edit the same image (invariant 3). If this
stage returned a pointer that could be regenerated, the two branches could end up
editing different concepts and the layers would not align.

The concept can run again at a later attempt (a concept repair round). What keeps
the branches aligned is `job["approvedConcept"]`, written by REVIEW_CONCEPT when a
concept is approved: downstream stages read that, never the latest CONCEPT
artifact.
"""

from __future__ import annotations

import json

from lib import artifacts, jobs, prompts, reference
from lib.errors import Stage
from lib.providers import image


def policy_block(policy, analysis):
    """Render the derived policy as readable English for the concept prompt.

    SPEC section 2.7: passing only a number or a guessed provider flag does not
    implement designFreedom. The instruction, what must be preserved, what may
    change and the always-permitted exceptions all go to the model.
    """
    lines = [
        f"REFERENCE POLICY: {policy['mode']} (designFreedom {policy['designFreedom']})",
        policy["instruction"],
        "",
        "Must preserve:",
        prompts.bullet_list(policy["mustPreserve"]),
        "",
        "May change:",
        prompts.bullet_list(policy["mayChange"]),
        "",
        "Always-permitted adaptations:",
        prompts.bullet_list(policy["documentedExceptions"]),
    ]
    if policy["mustPreserve"]:
        # Concrete, canvas-independent anchors observed in the reference pixels.
        lines += [
            "",
            "Reference anchors (normalized boxes, 0..1 from the top-left):",
            json.dumps(analysis["anchors"], ensure_ascii=False),
        ]
    return "\n".join(lines)


def repair_block(job_id, job, attempt):
    """Targeted repair instructions for a regenerated concept.

    On the first attempt there is nothing to repair. Later attempts follow either
    a failed concept review or a final review that found a concept defect.
    """
    if int(attempt) <= 1:
        return ""
    produced = job["artifacts"]
    messages = []
    key = produced.get(Stage.REVIEW_CONCEPT, {}).get("review")
    if key:
        review = artifacts.get_json(job_id, key)
        if not review.get("approved"):
            messages = [i["message"] for i in review.get("blockingIssues", [])]
    if not messages:
        key = produced.get(Stage.REVIEW, {}).get("review")
        if key:
            review = artifacts.get_json(job_id, key)
            messages = [i["message"] for i in review.get("blockingIssues", [])]
    if not messages:
        return ""
    return "FIX THESE PROBLEMS FROM THE PREVIOUS CONCEPT:\n" + prompts.bullet_list(messages[:10])


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.CONCEPT)
    request = artifacts.get_json(job_id, job["requestKey"])

    copy_key = job["artifacts"][Stage.PREPARE_COPY]["copy"]
    copy_items = artifacts.get_json(job_id, copy_key)["items"]

    canvas = request["canvas"]
    brief = request["brief"]
    brand = request.get("brand") or {}

    produced = job["artifacts"]
    policy = artifacts.get_json(job_id, produced[Stage.ANALYZE_REFERENCE]["policy"])
    analysis = artifacts.get_json(job_id, produced[Stage.ANALYZE_REFERENCE]["analysis"])

    prompt = prompts.render(
        "concept",
        policy_block=policy_block(policy, analysis),
        repair_block=repair_block(job_id, job, attempt),
        width=canvas["width"],
        height=canvas["height"],
        post=brief["post"],
        copy_block=prompts.copy_block(copy_items),
        brand_block=prompts.field_block(
            [
                ("Colours", ", ".join(brand.get("colors") or [])),
                ("Fonts", ", ".join(brand.get("fontFamilies") or [])),
                ("Business", (brief.get("business") or {}).get("name")),
            ]
        ),
        instructions_block=prompts.instructions_block(brief.get("instructions")),
    )

    # Real inspiration pixels, not a description of them (SPEC section 4).
    inspiration = reference.fetch(request["referenceImageUrl"])

    result = image.edit(
        prompt,
        inspiration["bytes"],
        canvas["width"],
        canvas["height"],
        transparent=False,
        operation_id=f"{job_id}:concept:{attempt}",
        mime=inspiration["mimeType"],
    )

    key = artifacts.artifact_key(job_id, Stage.CONCEPT, attempt, "concept.png")
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    jobs.record_artifacts(
        job_id,
        Stage.CONCEPT,
        attempt,
        {"concept": stored["key"], "sha256": stored["sha256"]},
    )
    return {
        "concept": stored["key"],
        "sha256": stored["sha256"],
        "bytes": stored["bytes"],
        "requestId": result["requestId"],
    }
