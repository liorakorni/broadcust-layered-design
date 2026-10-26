"""ANALYZE_REFERENCE: record what the reference actually contains.

SPEC section ANALYZE_REFERENCE. Vision looks at the real reference pixels and the
derived policy, and records anchors, hierarchy and visual qualities. Those
observations guide concept creation and concept review; they do not replace the
later analysis of the approved concept itself.

Persists two artifacts: policy.json (the derived policy, verbatim) and
reference-analysis.json.

The analysis is content-neutral: anchors describe regions by position, size and
visual role, never by what they depict, because the brief (not the reference)
decides subject matter. It also plans WHERE every approved copy item sits. That
plan is made once and reused by generation, review and every correction, so a
text item with no counterpart in the reference (a business name, typically) is
not given a new position on each attempt.
"""

from __future__ import annotations

import conf
from lib import (
    artifacts, direction_policy, jobs, prompts, reference, reference_policy,
    review_policy, schemas,
)
from lib.errors import InvalidModelJson, Stage
from lib.providers import vision

_STRING_LIST = {"type": "array", "items": {"type": "string"}}

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

REFERENCE_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "anchors", "copyPlacements", "hierarchy", "overlaps", "alignment", "readingFlow",
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
        "copyPlacements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["contentKey", "box", "basis"],
                "properties": {
                    "contentKey": {"type": "string"},
                    "box": _BOX,
                    "basis": {"type": "string"},
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


def check_placements(analysis, copy_items):
    """Every approved copy item has exactly one planned position, and no other.

    A missing placement would leave the item to be positioned ad hoc on each
    attempt - the instability this plan exists to remove - so an incomplete plan
    is a bad model response (retried), not something to fill in silently.
    """
    wanted = [item["contentKey"] for item in copy_items]
    planned = [p["contentKey"] for p in analysis["copyPlacements"]]
    problems = []
    missing = [k for k in wanted if k not in planned]
    unknown = sorted({k for k in planned if k not in wanted})
    duplicate = sorted({k for k in planned if planned.count(k) > 1})
    if missing:
        problems.append("missing " + ", ".join(missing))
    if unknown:
        problems.append("unknown " + ", ".join(unknown))
    if duplicate:
        problems.append("duplicated " + ", ".join(duplicate))
    for placement in analysis["copyPlacements"]:
        box = placement["box"]
        if box["width"] <= 0 or box["height"] <= 0:
            problems.append(f"empty box for {placement['contentKey']}")
        elif box["x"] + box["width"] > 1.001 or box["y"] + box["height"] > 1.001:
            problems.append(f"{placement['contentKey']} extends past the canvas")
    if problems:
        raise InvalidModelJson("copyPlacements invalid: " + "; ".join(problems))


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.ANALYZE_REFERENCE)
    request = artifacts.get_json(job_id, job["requestKey"])

    copy_key = job["artifacts"][Stage.PREPARE_COPY]["copy"]
    copy_items = artifacts.get_json(job_id, copy_key)["items"]
    canvas = request["canvas"]

    # Resolved before the reference is even analysed, so the analysis, the
    # concept and its review all work to the same direction.
    # The acceptance-time resolution when the job has one, so this stage cannot
    # reach a different answer from the approved copy than the request already
    # settled. A job accepted before that existed resolves as it always did.
    direction = job.get("layoutPolicy") or direction_policy.resolve(request, copy_items)
    policy = reference_policy.derive_reference_policy(
        request.get("designFreedom", 0),
        request.get("reviewStrictness", review_policy.DEFAULT_STRICTNESS),
        direction,
    )

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
        content_boundary=prompts.bullet_list(policy["contentBoundary"]),
        copy_block=prompts.copy_block(copy_items),
        direction_block=direction_policy.prompt_block(direction),
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
    check_placements(analysis, copy_items)
    # Stamped so a later stage can refuse to reuse it under a different policy.
    analysis["policyVersion"] = policy["policyVersion"]

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
    return {
        **pointers,
        "mode": policy["mode"],
        "designFreedom": policy["designFreedom"],
        "direction": direction,
    }
