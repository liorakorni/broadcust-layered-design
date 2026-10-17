"""CONCEPT: create the internal composition that separation works from.

The concept is NOT the deliverable (SPEC section 4). It may contain the target
text, because its job is to show the layout engine what the design should look
like. The customer never sees it.

The exact bytes are persisted immutably before returning, because CLEAN_PHOTO
and DECORATION_OVERLAY must both edit the same image (invariant 3). If this
stage returned a pointer that could be regenerated, the two branches could end up
editing different concepts and the layers would not align.
"""

from __future__ import annotations

from lib import artifacts, jobs, prompts, reference
from lib.errors import Stage
from lib.providers import image


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.CONCEPT)
    request = artifacts.get_json(job_id, job["requestKey"])

    copy_key = job["artifacts"][Stage.PREPARE_COPY]["copy"]
    copy_items = artifacts.get_json(job_id, copy_key)["items"]

    canvas = request["canvas"]
    brief = request["brief"]
    brand = request.get("brand") or {}

    prompt = prompts.render(
        "concept",
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
