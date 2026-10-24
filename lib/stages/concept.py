"""CONCEPT: create the internal composition that separation works from.

The concept is NOT the deliverable (SPEC section 4). It may contain the target
text, because its job is to show the layout engine what the design should look
like. The customer never sees it.

The exact bytes are persisted immutably before returning, because CLEAN_PHOTO
and DECORATION_OVERLAY must both edit the same image (invariant 3). If this
stage returned a pointer that could be regenerated, the two branches could end up
editing different concepts and the layers would not align.

The concept can run again at a later attempt (a concept correction round). What
keeps the branches aligned is `job["approvedConcept"]`, written by REVIEW_CONCEPT
when a candidate is selected: downstream stages read that, never the latest
CONCEPT artifact.

Attempt 1 generates from the reference. Every later attempt is a CORRECTION of a
stored candidate, never a fresh generation from the reference. Regenerating with a
list of fixes is what let a correction drop text that an earlier attempt had
right and change imagery nobody asked it to touch: the model re-composes
everything it is not forced to keep. A correction instead edits the chosen
candidate's own pixels, repaints only the regions the blocking issues name where
they can be located (a mask), and then puts the base's pixels back everywhere
else, so what was right stays exactly as it was.
"""

from __future__ import annotations

import io
import json
import logging

from lib import (
    artifacts, edit_mask, jobs, policy_context, prompts, reference, review_policy, telemetry,
)
from lib.errors import AssetInvalid, Stage
from lib.providers import image

log = logging.getLogger("layered-design.concept")


def policy_block(policy, analysis):
    """Render the derived policy as readable English for every concept prompt.

    SPEC section 2.7: passing only a number or a guessed provider flag does not
    implement designFreedom. The instruction, what must be preserved, what may
    change, the always-permitted exceptions, the reference/content boundary and
    the severity policy all go to the model. Generation, review and correction
    render the same block, which is what keeps their idea of "acceptable" aligned.
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
        "",
        review_policy.severity_block(policy["reviewPolicy"]),
    ]
    if policy["mustPreserve"]:
        # Concrete, canvas-independent anchors observed in the reference pixels.
        lines += [
            "",
            "Reference anchors (normalized boxes, 0..1 from the top-left):",
            json.dumps(analysis["anchors"], ensure_ascii=False),
        ]
    return "\n".join(lines)


def placement_block(placements, copy_items):
    """The planned position of every approved copy item, one line each."""
    lines = []
    for item in copy_items:
        box = placements.get(item["contentKey"])
        if box:
            lines.append(
                f'- {item["contentKey"]}: x={box["x"]:.3f} y={box["y"]:.3f} '
                f'w={box["width"]:.3f} h={box["height"]:.3f}'
            )
    return "\n".join(lines) if lines else "(none planned)"


def issues_block(issues):
    return prompts.bullet_list(
        [
            f"[{i['code']}] {i['message']}" for i in issues
        ]
    )


def edit_scope_block(plan):
    if plan["wholeImage"] or not plan["regions"]:
        return (
            "EDIT SCOPE: no mask is applied. Change only the elements named in the "
            "problems above; every other element must be left as it is."
        )
    boxes = "\n".join(
        f'- x={r["x"]:.3f} y={r["y"]:.3f} w={r["width"]:.3f} h={r["height"]:.3f}'
        for r in plan["regions"]
    )
    return (
        "EDIT SCOPE: a mask limits editing to these regions (normalized, from the "
        "top-left). Everything outside them is locked and is kept exactly. Make the "
        "correction inside them:\n" + boxes
    )


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.CONCEPT)
    request = artifacts.get_json(job_id, job["requestKey"])

    policy, analysis, copy_items, placements = policy_context.load(job_id, job)

    if int(attempt) <= 1:
        return _generate(job_id, attempt, request, policy, analysis, copy_items, placements)

    correction = job.get("conceptCorrection") or {}
    if correction.get("forAttempt") != int(attempt):
        # Never fall back to regenerating from the reference: that is the
        # regression this stage exists to prevent.
        raise AssetInvalid(
            f"Concept attempt {attempt} has no correction plan; a later attempt "
            f"corrects a stored candidate and is never generated afresh."
        )
    plan = artifacts.get_json(job_id, correction["planKey"])
    return _correct(job_id, attempt, request, policy, analysis, copy_items, placements, plan)


def _brand_block(brief, brand):
    return prompts.field_block(
        [
            ("Colours", ", ".join(brand.get("colors") or [])),
            ("Fonts", ", ".join(brand.get("fontFamilies") or [])),
            ("Business", (brief.get("business") or {}).get("name")),
        ]
    )


def _generate(job_id, attempt, request, policy, analysis, copy_items, placements):
    canvas = request["canvas"]
    brief = request["brief"]
    brand = request.get("brand") or {}

    prompt = prompts.render(
        "concept",
        policy_block=policy_block(policy, analysis),
        width=canvas["width"],
        height=canvas["height"],
        post=brief["post"],
        copy_block=prompts.copy_block(copy_items),
        placement_block=placement_block(placements, copy_items),
        brand_block=_brand_block(brief, brand),
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
    telemetry.record_image_call(job_id, Stage.CONCEPT)
    return _store(job_id, attempt, result, {})


def _correct(job_id, attempt, request, policy, analysis, copy_items, placements, plan):
    """Edit the chosen candidate; never start from the reference again."""
    canvas = request["canvas"]
    base_bytes = artifacts.get_bytes(job_id, plan["base"]["key"])
    if artifacts.sha256_hex(base_bytes) != plan["base"]["sha256"]:
        raise AssetInvalid("The correction base does not match the candidate it was planned from.")

    from PIL import Image

    with Image.open(io.BytesIO(base_bytes)) as img:
        size = img.size

    mask = None
    edit_fraction = 1.0
    if not plan["wholeImage"] and plan["regions"]:
        mask, edit_fraction = edit_mask.build_region_mask(size, plan["regions"])

    prompt = prompts.render(
        "concept-correction",
        policy_block=policy_block(policy, analysis),
        width=canvas["width"],
        height=canvas["height"],
        copy_block=prompts.copy_block(copy_items),
        placement_block=placement_block(placements, copy_items),
        issues_block=issues_block(plan["issues"]),
        edit_scope_block=edit_scope_block(plan),
    )

    result = image.edit(
        prompt,
        base_bytes,
        canvas["width"],
        canvas["height"],
        transparent=False,
        mask=mask,
        operation_id=f"{job_id}:concept:{attempt}",
        mime="image/png",
    )
    telemetry.record_image_call(job_id, Stage.CONCEPT)

    record = {
        "baseAttempt": plan["base"]["attempt"],
        "baseSha256": plan["base"]["sha256"],
        "source": plan["source"],
        "issueCodes": [i["code"] for i in plan["issues"]],
        "wholeImage": mask is None,
        "regions": plan["regions"],
        "editFraction": round(edit_fraction, 4),
        "promptSha256": artifacts.sha256_hex(prompt.encode("utf-8")),
        "maskSha256": artifacts.sha256_hex(mask) if mask is not None else None,
    }

    # The provider's own output is kept untouched, so what the model really did
    # to the locked area stays measurable after the fact.
    raw_key = artifacts.artifact_key(job_id, Stage.CONCEPT, attempt, "concept-provider-output.png")
    artifacts.put_bytes(raw_key, result["bytes"], "image/png")
    final_bytes = result["bytes"]
    if mask is not None:
        record["providerOutsideMaskMeanAbsDiff"] = round(
            edit_mask.outside_difference(base_bytes, result["bytes"], mask), 3
        )
        # Enforced, not requested: pixels outside the repaint region are the
        # base's, whatever the provider returned there.
        final_bytes = edit_mask.preserve_outside(base_bytes, result["bytes"], mask)
        record["outsideMaskMeanAbsDiff"] = round(
            edit_mask.outside_difference(base_bytes, final_bytes, mask), 3
        )
        mask_key = artifacts.artifact_key(job_id, Stage.CONCEPT, attempt, "correction-mask.png")
        artifacts.put_bytes(mask_key, mask, "image/png")
        record["maskKey"] = mask_key
    log.info(
        "concept correction attempt=%s base=%s whole=%s editFraction=%.3f",
        attempt, plan["base"]["attempt"], mask is None, edit_fraction,
    )

    correction_key = artifacts.artifact_key(job_id, Stage.CONCEPT, attempt, "correction.json")
    artifacts.put_json(correction_key, record)
    return _store(
        job_id, attempt, {**result, "bytes": final_bytes},
        {"correction": correction_key, "baseAttempt": plan["base"]["attempt"]},
    )


def _store(job_id, attempt, result, extra):
    key = artifacts.artifact_key(job_id, Stage.CONCEPT, attempt, "concept.png")
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    jobs.record_artifacts(
        job_id,
        Stage.CONCEPT,
        attempt,
        {"concept": stored["key"], "sha256": stored["sha256"], **extra},
    )
    return {
        "concept": stored["key"],
        "sha256": stored["sha256"],
        "bytes": stored["bytes"],
        "requestId": result["requestId"],
    }
