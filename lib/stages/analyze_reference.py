"""ANALYZE_REFERENCE: record what the reference actually contains.

SPEC section ANALYZE_REFERENCE. Vision looks at the real reference pixels and the
derived policy, and records anchors, hierarchy and visual qualities. Those
observations guide concept creation and concept review; they do not replace the
later analysis of the approved concept itself.

Persists two artifacts: policy.json (the derived policy, verbatim) and
reference-analysis.json.
"""

from __future__ import annotations

import conf
from lib import artifacts, jobs, prompts, reference, reference_policy, schemas
from lib.errors import Stage
from lib.providers import vision

_STRING_LIST = {"type": "array", "items": {"type": "string"}}

REFERENCE_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "anchors", "hierarchy", "overlaps", "alignment", "readingFlow",
        "palette", "visualQualities", "notes",
    ],
    "properties": {
        "anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "box", "relativeScale", "confidence"],
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(schemas.ANCHOR_KINDS)},
                    "box": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["x", "y", "width", "height"],
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                        },
                    },
                    "relativeScale": {"type": "number"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "hierarchy": _STRING_LIST,
        "overlaps": _STRING_LIST,
        "alignment": {"type": "string"},
        "readingFlow": {"type": "string"},
        "palette": _STRING_LIST,
        "visualQualities": _STRING_LIST,
        "notes": {"type": "string"},
    },
}


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.ANALYZE_REFERENCE)
    request = artifacts.get_json(job_id, job["requestKey"])

    policy = reference_policy.derive_reference_policy(request.get("designFreedom", 0))
    copy_key = job["artifacts"][Stage.PREPARE_COPY]["copy"]
    copy_items = artifacts.get_json(job_id, copy_key)["items"]
    canvas = request["canvas"]

    # Real reference pixels, not a description of them.
    inspiration = reference.fetch(request["referenceImageUrl"])

    prompt = prompts.render(
        "analyze-reference",
        width=canvas["width"],
        height=canvas["height"],
        mode=policy["mode"],
        design_freedom=policy["designFreedom"],
        policy_instruction=policy["instruction"],
        must_preserve=prompts.bullet_list(policy["mustPreserve"]),
        copy_block=prompts.copy_block(copy_items),
    )
    raw = vision.analyze(
        prompt,
        (inspiration["bytes"],),
        schema=REFERENCE_ANALYSIS_SCHEMA,
        schema_name="reference_analysis_v1",
        model=conf.REVIEW_MODEL,
        operation_id=f"{job_id}:analyze-reference:{attempt}",
        detail="original",
    )
    analysis = schemas.parse_reference_analysis(raw).model_dump(mode="json")

    policy_stored = artifacts.put_json(
        artifacts.artifact_key(job_id, Stage.ANALYZE_REFERENCE, attempt, "policy.json"),
        policy,
    )
    analysis_stored = artifacts.put_json(
        artifacts.artifact_key(
            job_id, Stage.ANALYZE_REFERENCE, attempt, "reference-analysis.json"
        ),
        analysis,
    )
    pointers = {"policy": policy_stored["key"], "analysis": analysis_stored["key"]}
    jobs.record_artifacts(job_id, Stage.ANALYZE_REFERENCE, attempt, pointers)
    return {**pointers, "mode": policy["mode"], "designFreedom": policy["designFreedom"]}
