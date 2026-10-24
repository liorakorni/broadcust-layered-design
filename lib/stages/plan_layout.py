"""PLAN_LAYOUT and GENERATE_PHOTO: the layout-first flow (Phase B, behind a flag).

The legacy flow generates a flattened full-concept image and then decomposes it into
scene, overlay and photograph. Every defect it introduces is baked into pixels, and
a text or layout change can only be answered by regenerating them.

This flow starts from the editable document instead:

    reference analysis + approved copy
      -> PLAN_LAYOUT   an editable layout (text layers with typography, code-owned
                       shapes, effect owners) and a TEXT-FREE brief for the photograph
      -> GENERATE_PHOTO the one image that is actually generated
      -> VALIDATE_ASSETS (local photograph checks; no overlay exists)
      -> ASSEMBLE / RENDER / REVIEW, then targeted repair like any other design

There is no flattened concept, no scene extraction and no overlay separation, and it
never routes back through them. A text or layout problem is a patch to the layout; a
wrong photograph regenerates the photograph alone.

What the stages share, in place of a concept, is the BASIS: the reference image plus
the photo brief, hashed. Every later stage records that hash and FINALIZE refuses to
ship unless they all agree, which is the same guarantee the concept hash gives the
legacy flow.
"""

from __future__ import annotations

import copy
import json

import conf
from lib import (
    artifacts, dependencies, jobs, policy_context, prompts, reference, schemas,
    stage_cache, telemetry, validate_assets,
)
from lib.errors import AssetInvalid, InvalidScene, Stage
from lib.providers import image, vision
from lib.stages import extract_scene
from lib.stages.concept import _brand_block, placement_block, policy_block

_STOP = {
    "type": "object",
    "additionalProperties": False,
    "required": ["color", "opacity", "position"],
    "properties": {
        "color": {"type": "string"},
        "opacity": {"type": "number"},
        "position": {"type": "number"},
    },
}

_SHAPE = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id", "kind", "effect", "x", "y", "width", "height", "zIndex", "color",
        "opacity", "radius", "angle", "stops", "borderColor", "borderWidth", "rotation",
    ],
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string", "enum": list(schemas.SHAPE_KINDS)},
        "effect": {"type": "string"},
        **extract_scene._BOX,
        "zIndex": {"type": "integer"},
        "color": {"type": ["string", "null"]},
        "opacity": {"type": "number"},
        "radius": {"type": "integer"},
        "angle": {"type": "integer"},
        "stops": {"type": "array", "items": _STOP},
        "borderColor": {"type": ["string", "null"]},
        "borderWidth": {"type": "number"},
        "rotation": {"type": "integer"},
    },
}

_EFFECT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "kind", "owner", "note"],
    "properties": {
        "id": {"type": "string"},
        "kind": {"type": "string", "enum": list(schemas.EFFECT_KINDS)},
        "owner": {"type": "string", "enum": ["layout"]},
        "note": {"type": ["string", "null"]},
    },
}


def _plan_schema():
    """The scene schema, minus the overlay, plus shapes, effects and the photo brief."""
    schema = copy.deepcopy(extract_scene.SCENE_SCHEMA)
    schema["required"] = [
        "canvas", "textLayers", "imageLayers", "shapeLayers", "effects", "photoBrief",
    ]
    del schema["properties"]["overlaySamples"]
    schema["properties"]["imageLayers"]["items"]["properties"]["type"]["enum"] = ["background"]
    schema["properties"]["shapeLayers"] = {"type": "array", "items": _SHAPE}
    schema["properties"]["effects"] = {"type": "array", "items": _EFFECT}
    schema["properties"]["photoBrief"] = {"type": "string"}
    return schema


PLAN_SCHEMA = _plan_schema()


def basis_sha(reference_sha, photo_brief):
    """The hash every later stage must agree on, in place of a concept's."""
    return dependencies.digest({"reference": reference_sha, "photoBrief": photo_brief})


# --- PLAN_LAYOUT -------------------------------------------------------------


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.PLAN_LAYOUT)
    request = artifacts.get_json(job_id, job["requestKey"])
    policy, analysis, copy_items, placements = policy_context.load(job_id, job)
    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}

    inspiration = reference.fetch(request["referenceImageUrl"])
    brief = request["brief"]
    prompt = prompts.render(
        "plan-layout",
        width=canvas["width"],
        height=canvas["height"],
        fonts=", ".join(conf.APPROVED_FONT_FAMILIES),
        policy_block=policy_block(policy, analysis),
        post=brief["post"],
        brand_block=_brand_block(brief, request.get("brand") or {}),
        instructions_block=prompts.instructions_block(brief.get("instructions")),
        copy_block=prompts.copy_block(copy_items),
        placement_block=placement_block(placements, copy_items),
    )
    approved = {i["contentKey"]: i["text"] for i in copy_items}
    scene = photo_brief = None
    rejection = None
    for planning_round in (1, 2):
        # One bounded retry: a plan that breaks a schema rule (a chevron with no
        # outline colour) is told exactly what was wrong. This is a text/vision call,
        # not an image generation, and a second rejection is final.
        raw = vision.analyze(
            prompt if rejection is None else (
                prompt + "\n\nYOUR PREVIOUS PLAN WAS REJECTED: " + rejection
                + "\nReturn the complete corrected plan; change only what that requires."),
            (inspiration["bytes"],),
            schema=PLAN_SCHEMA,
            schema_name="layout_plan_v1",
            model=conf.SCENE_MODEL,
            operation_id=f"{job_id}:plan_layout:{attempt}:{planning_round}",
            detail="original",
        )
        photo_brief = (raw.pop("photoBrief") or "").strip()
        if not photo_brief:
            raise InvalidScene("The layout plan did not describe the photograph it needs.")
        raw["overlaySamples"] = []
        _limit_stops(raw)
        _snap_shape_numbers(raw)
        _drop_undrawn_effects(raw)
        try:
            scene = schemas.parse_scene(
                raw, canvas["width"], canvas["height"],
                allowed_content_keys=set(approved),
                allowed_fonts=set(conf.APPROVED_FONT_FAMILIES),
            )
            break
        except InvalidScene as exc:
            if planning_round == 2:
                raise
            rejection = exc.message
            jobs.add_metric(job_id, "planRetries")
    extract_scene.check_copy_is_verbatim(scene, approved)
    check_plan(scene, approved)

    document = scene.model_dump(mode="json")
    reference_sha = artifacts.sha256_hex(inspiration["bytes"])
    sha = basis_sha(reference_sha, photo_brief)
    scene_key = artifacts.artifact_key(job_id, Stage.PLAN_LAYOUT, attempt, "scene.json")
    stored = artifacts.put_json(scene_key, document)
    plan_key = artifacts.artifact_key(job_id, Stage.PLAN_LAYOUT, attempt, "plan.json")
    artifacts.put_json(plan_key, {
        "photoBrief": photo_brief, "referenceSha256": reference_sha, "basisSha256": sha,
        "promptSha256": dependencies.digest(prompt), "model": conf.SCENE_MODEL,
    })
    jobs.record_artifacts(
        job_id, Stage.PLAN_LAYOUT, attempt,
        {"scene": stored["key"], "plan": plan_key, "conceptSha256": sha,
         "referenceSha256": reference_sha},
    )
    return {"scene": stored["key"], "plan": plan_key, "textLayers": len(scene.textLayers),
            "shapeLayers": len(scene.shapeLayers)}


MAX_STOPS = 4


def _limit_stops(raw):
    """Thin a gradient the model gave too many stops to the schema's limit.

    A strict model schema cannot express a maximum list length, so a planner that
    writes five stops would fail the whole plan for a limit that has nothing to do
    with the design. The first and last stops are kept (they set the ends) and the
    rest are sampled evenly; the gradient looks the same to within the number of
    stops it was thinned by.
    """
    for shape in raw.get("shapeLayers") or []:
        stops = shape.get("stops") or []
        if len(stops) > MAX_STOPS:
            ordered = sorted(stops, key=lambda stop: stop["position"])
            step = (len(ordered) - 1) / (MAX_STOPS - 1)
            shape["stops"] = [ordered[round(i * step)] for i in range(MAX_STOPS)]


def _snap_shape_numbers(raw):
    """Bring shape numbers a strict model schema cannot bound into their ranges.

    A chevron rotated 100 degrees or an opacity of 1.2 is a rounding of what the
    model meant, not a design decision, and rejecting the whole plan for it throws
    away a paid planning call. Rotation snaps to the nearest 45 degrees; the rest
    clamp to the range the schema allows. Nothing here changes which shapes exist.
    """
    def clamp(value, low, high):
        return max(low, min(high, value))

    for shape in raw.get("shapeLayers") or []:
        if isinstance(shape.get("rotation"), (int, float)):
            shape["rotation"] = int(round(shape["rotation"] / 45.0) * 45) % 360
        for name, low, high in (("opacity", 0.0, 1.0), ("angle", 0, 360), ("zIndex", 0, 100),
                                ("radius", 0, 2048), ("borderWidth", 0.0, 48.0)):
            if isinstance(shape.get(name), (int, float)):
                shape[name] = clamp(shape[name], low, high)
        for stop in shape.get("stops") or []:
            for name in ("opacity", "position"):
                if isinstance(stop.get(name), (int, float)):
                    stop[name] = clamp(stop[name], 0.0, 1.0)


def _drop_undrawn_effects(raw):
    """Withdraw a layout-owned effect that no shape draws.

    The claim "the layout draws this" with nothing drawing it describes an empty
    layout, not a design: nothing is painted either way, so dropping the claim loses
    nothing - and it leaves the effect un-owned, which lets the final review ask for
    it properly (a shape added through the same ownership checks) if it is needed.
    """
    drawn = {shape.get("effect") for shape in raw.get("shapeLayers") or []}
    raw["effects"] = [
        e for e in raw.get("effects") or []
        if e.get("owner") != "layout" or e.get("id") in drawn
    ]


def check_plan(scene, approved):
    """A plan must cover every approved item and have exactly one photograph."""
    missing = sorted(set(approved) - {l.contentKey for l in scene.textLayers})
    if missing:
        raise InvalidScene("The layout plan omitted approved copy: " + ", ".join(missing))
    backgrounds = [l for l in scene.imageLayers if l.type == "background"]
    if len(backgrounds) != 1 or len(scene.imageLayers) != 1:
        raise InvalidScene(
            "A layout-first plan has exactly one image layer, the background photograph."
        )


# --- GENERATE_PHOTO ----------------------------------------------------------


def generate_photo(job_id, attempt, asset_id=None):
    """Generate the single text-free photograph the plan asked for."""
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.GENERATE_PHOTO)
    planned = job["artifacts"][Stage.PLAN_LAYOUT]
    plan = artifacts.get_json(job_id, planned["plan"])
    if plan["basisSha256"] != planned["conceptSha256"]:
        raise AssetInvalid("The stored layout plan does not match its recorded basis.")
    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}

    instruction = None
    pending = job.get("photoRepair") or {}
    if int(pending.get("forAttempt", 0)) == int(attempt):
        instruction = pending.get("instruction")
    prompt = prompts.render(
        "generate-photo", width=canvas["width"], height=canvas["height"],
        photo_brief=plan["photoBrief"],
    )
    if instruction:
        prompt += (
            "\n\nCORRECTION REQUIRED (change only this; keep the rest of the "
            f"photograph as briefed):\n{instruction}"
        )

    cache_id = stage_cache.key(
        Stage.GENERATE_PHOTO, prompt=stage_cache.prompt_digest(prompt),
        model=conf.IMAGE_MODEL, canvas=canvas,
    )
    cached = None if instruction else stage_cache.lookup(job, Stage.GENERATE_PHOTO, cache_id)
    if cached:
        stage_cache.hit(job_id, Stage.GENERATE_PHOTO)
        jobs.record_artifacts(job_id, Stage.GENERATE_PHOTO, attempt, cached["pointers"])
        return {**cached["result"], "cacheHit": True}

    result = image.generate(
        prompt, canvas["width"], canvas["height"], transparent=False,
        operation_id=f"{job_id}:generate_photo:{attempt}",
    )
    telemetry.record_image_call(job_id, Stage.GENERATE_PHOTO)
    key = artifacts.artifact_key(job_id, Stage.GENERATE_PHOTO, attempt, "photo.png")
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    pointers = {
        "background": stored["key"], "sha256": stored["sha256"],
        "conceptSha256": planned["conceptSha256"],
    }
    jobs.record_artifacts(job_id, Stage.GENERATE_PHOTO, attempt, pointers)
    out = {"background": stored["key"], "sha256": stored["sha256"],
           "requestId": result["requestId"]}
    if not instruction:
        stage_cache.remember(job_id, cache_id, {"pointers": pointers, "result": out})
    return out


def repair_photo(job_id, attempt, asset_id=None):
    """Regenerate only the photograph, told what was wrong (layout-first)."""
    job = jobs.get_job_unscoped(job_id)
    pending = job.get("photoRepair") or {}
    if int(pending.get("forAttempt", 0)) != int(attempt):
        raise AssetInvalid("No photo repair was planned for this attempt.")
    return generate_photo(job_id, attempt)


# --- VALIDATE_ASSETS ---------------------------------------------------------


def validate_photo(job_id, job, attempt):
    """The only generated asset is the photograph, so that is all there is to check.

    The checks are local and deterministic (decodes, canvas-sized after resampling,
    opaque, not blank). The result is bound to the photograph's hash and the basis.
    """
    from lib.stages.validate import _entry

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    produced = job["artifacts"][Stage.GENERATE_PHOTO]
    data = artifacts.get_bytes(job_id, produced["background"])
    sha = artifacts.sha256_hex(data)
    validate_assets.validate_background(data, canvas)
    entries = {
        "photoValidation": _entry(
            dependencies.cache_key("photoValidation", background=sha),
            {"assets": {"background": sha}}, cache_hit=False,
        )
    }
    result = {
        "background": produced["background"], "entries": entries,
        "policyVersion": dependencies.VALIDATION_POLICY_VERSION,
    }
    key = artifacts.artifact_key(job_id, Stage.VALIDATE_ASSETS, attempt, "validation.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(
        job_id, Stage.VALIDATE_ASSETS, attempt,
        {"validation": stored["key"], "conceptSha256": produced["conceptSha256"],
         "entries": entries, "cacheHits": []},
    )
    return result


# --- REVIEW / FINALIZE basis -------------------------------------------------


def _plan_review(job_id, job):
    """The plan's own settled record, in the shape a concept review has."""
    plan = artifacts.get_json(job_id, job["artifacts"][Stage.PLAN_LAYOUT]["plan"])
    return {
        "approved": True,
        "mode": "layout-first",
        "designFreedom": int(job.get("designFreedom", 0)),
        "documentedExceptions": [],
        "requiredChecks": [],
        "warnings": [],
        "reviewStrictness": job.get("reviewStrictness"),
        "plan": {"photoBrief": plan["photoBrief"], "basisSha256": plan["basisSha256"]},
    }


def review_basis(job_id, job):
    """What a layout-first render is judged against: the reference, for structure."""
    planned = job["artifacts"][Stage.PLAN_LAYOUT]
    request = artifacts.get_json(job_id, job["requestKey"])
    inspiration = reference.fetch(request["referenceImageUrl"])
    if artifacts.sha256_hex(inspiration["bytes"]) != planned["referenceSha256"]:
        raise AssetInvalid("The reference image changed since the layout was planned.")
    review = _plan_review(job_id, job)
    plan = artifacts.get_json(job_id, planned["plan"])
    return {
        "image": inspiration["bytes"],
        "sha256": planned["conceptSha256"],
        "conceptReview": review,
        "block": "\n".join([
            "- flow: layout-first (editable layout; one generated text-free photograph)",
            f"- photograph brief: {plan['photoBrief']}",
        ]),
        "reviewKey": planned["plan"],
        "promptName": "review-layout",
    }


def finalize_basis(job_id, job):
    planned = job["artifacts"][Stage.PLAN_LAYOUT]
    return {
        "attempt": 1,
        "key": planned["scene"],
        "sha256": planned["conceptSha256"],
        "reviewKey": planned["plan"],
        "selectedBy": "layout-first",
        "selectionReason": "layout-first flow: the plan is the basis",
        "selectionKey": None,
        "policyVersion": job["policyVersion"],
        "review": _plan_review(job_id, job),
    }
