"""CLEAN_PHOTO and DECORATION_OVERLAY: split the concept into two aligned layers.

Both stages edit the SAME immutable concept bytes (invariant 3, SPEC section 4).
Feeding one stage's image output into the other - extracting decoration from the
already cleaned photograph, say - produces an empty overlay, because the artwork
was already removed. `_concept_bytes` below is the only way either one obtains
its source image.

The overlay runs first because CLEAN_PHOTO needs to know where the artwork is: it
repaints only that area and keeps the visible photograph pixel for pixel. Left to
re-compose the whole frame the model returned the photograph zoomed and shifted,
and the stacked layers no longer matched the concept. The overlay contributes
only the SHAPE of its alpha channel to the mask (lib/edit_mask.py); its pixels
are never an input image to CLEAN_PHOTO.

DECORATION_OVERLAY requires genuine RGBA. If the provider says it cannot produce
a transparent background, that is terminal: keying out white would destroy every
light-coloured graphic and every soft shadow edge.
"""

from __future__ import annotations

import io
import logging

import conf
from lib import (
    artifacts, design_base, edit_mask, jobs, prompts, provider_trace,
    repair_budget, stage_cache, telemetry, template_check, template_policy, validate_assets,
)
from lib.errors import AssetInvalid, Stage
from lib.providers import image

log = logging.getLogger("layered-design.separation")


def decoration_overlay(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    if jobs.is_v2(job):
        return _design_base(job_id, attempt, job)
    return _separate(
        job_id,
        attempt,
        stage=Stage.DECORATION_OVERLAY,
        prompt_name="decoration-overlay",
        artifact_name="overlay.png",
        label="decoration",
        transparent=True,
    )


def _scene_of(job_id, job):
    return artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])


def _design_base(job_id, attempt, job):
    """The Design Base extraction of a layered-v2 job: ownership-plan prompt, or no call.

    A design whose base is CSS (a gradient, or a fallback already chosen) has nothing for
    the provider to extract, and the absence is recorded as such: the later stages read
    `omitted` rather than guessing from a missing pointer.
    """
    scene = _scene_of(job_id, job)
    if not any(l["type"] == "decoration" for l in scene.get("imageLayers") or []):
        plan = scene.get("layerPlan") or {}
        reason = (plan.get("template") or {}).get("reason") or "The design base is drawn in CSS."
        jobs.record_artifacts(job_id, Stage.DECORATION_OVERLAY, attempt, {
            "omitted": True, "reason": reason[:400],
            "representation": (plan.get("template") or {}).get("representation", "css-only"),
        })
        return {"omitted": True, "reason": reason[:400], "imageGenerationCalls": 0}

    concept = _concept_bytes(job_id, job)
    prompt, info = design_base.build_prompt(scene, concept, template_policy.for_job(job))
    return _separate(
        job_id, attempt, stage=Stage.DECORATION_OVERLAY, prompt_name="design-base",
        artifact_name="overlay.png", label="decoration", transparent=True,
        prompt=prompt, notes=info,
    )


def repair_overlay(job_id, attempt, asset_id=None):
    """Extract the overlay again from the concept, told exactly what was wrong.

    SPEC section 4 bounds these repairs to two per job. The repair starts from
    the approved concept, not from the rejected overlay. Editing the rejected
    overlay could only remove things from it, so it could not restore a missing
    badge, and it never saw the concept, so it could not tell a panel that belongs
    from a leftover: on the job that exposed this, both edits made the overlay
    worse (the first inverted its alpha, the second erased the panel).

    The finding that triggered the repair is appended to the full extraction
    prompt, so every rule about what to keep and what to remove still applies.

    The result is written under its own artifact name so the rejected overlay
    stays readable - artifacts are append-only, and a repair that made things
    worse needs to be diagnosable afterwards.
    """
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.DECORATION_OVERLAY)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    concept = _concept_bytes(job_id, job)

    defect = (job.get("overlayDefect") or "").strip()
    v2 = jobs.is_v2(job)
    notes = {"defect": defect[:400]}
    if v2:
        # Same ownership-plan request as the first extraction, plus the one finding.
        prompt, info = design_base.build_prompt(
            _scene_of(job_id, job), concept, template_policy.for_job(job),
            defect=defect or "The previous design base differed from the concept.")
        notes.update(info)
        prompt_name = "design-base+design-base-repair"
    else:
        prompt = (
            prompts.render(
                "decoration-overlay", width=canvas["width"], height=canvas["height"],
                components=_removal_block(job_id, job),
            )
            + "\n\n"
            + prompts.render(
                "repair-overlay",
                defects=prompts.bullet_list(
                    [defect] if defect else ["The previous overlay failed validation."]
                ),
            )
        )
        prompt_name = "decoration-overlay+repair-overlay"

    if v2:
        round_number = max(1, repair_budget.used(job, "templateImage"))
    else:
        round_number = int(job.get("overlayRepairs", 1) or 1)
    result = image.edit(
        prompt,
        concept,
        canvas["width"],
        canvas["height"],
        transparent=True,
        operation_id=f"{job_id}:overlay_repair:{round_number}",
    )
    telemetry.record_image_call(job_id, Stage.DECORATION_OVERLAY)

    key = artifacts.artifact_key(
        job_id, Stage.DECORATION_OVERLAY, attempt, f"overlay-repair-{round_number}.png"
    )
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    _trace(job_id, Stage.DECORATION_OVERLAY, attempt, f"provider-trace-overlay-repair-{round_number}.json",
           prompt_name=prompt_name, prompt=prompt, concept=concept,
           mask=None, result=result, stored=stored,
           operation_id=f"{job_id}:overlay_repair:{round_number}",
           notes=notes)
    # Repoints DECORATION_OVERLAY at the repaired bytes, so the re-validation
    # that follows reads the repair rather than the overlay it just rejected.
    jobs.record_artifacts(
        job_id,
        Stage.DECORATION_OVERLAY,
        attempt,
        {
            "decoration": stored["key"],
            "sha256": stored["sha256"],
            "conceptSha256": artifacts.sha256_hex(concept),
        },
    )
    return {
        "decoration": stored["key"],
        "sha256": stored["sha256"],
        "repairRound": round_number,
        "requestId": result["requestId"],
        # The workflow's next step reads this. A layered-v2 template repair never rebuilds
        # the photograph (Background Image is photographic content only, and it was not
        # what was wrong); the legacy path always did, which is where the fixture job's
        # two extra photo calls came from.
        "rebuildPhoto": not v2,
    }


def clean_photo(job_id, attempt, asset_id=None, after_repair=False, instruction=None):
    """Repaint everything that is not the photograph, keeping the photograph.

    Edits the immutable concept under a mask built from the overlay's shape and
    the scene's text boxes (lib/edit_mask.py), so the visible photograph keeps its
    exact framing. `after_repair` rebuilds the photo against a repaired overlay;
    it is written under its own name because artifacts are append-only.
    """
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.CLEAN_PHOTO)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    concept = _concept_bytes(job_id, job)

    overlay_ref = (job.get("artifacts") or {}).get(Stage.DECORATION_OVERLAY) or {}
    # A layered-v2 design whose base is CSS has no template raster; the photograph is then
    # cleaned only of what other owners draw (text, icons, panels).
    no_template = bool(overlay_ref.get("omitted")) and jobs.is_v2(job)
    if not overlay_ref.get("decoration") and not no_template:
        raise AssetInvalid(
            "CLEAN_PHOTO needs the decoration overlay to know what to repaint, but "
            "none has been produced."
        )
    overlay = None if no_template else artifacts.get_bytes(job_id, overlay_ref["decoration"])
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])

    from PIL import Image

    with Image.open(io.BytesIO(concept)) as img:
        size = img.size
    disbelieved, mask_notes = None, {}
    if jobs.is_v2(job) and overlay is not None:
        # Do not repaint the photograph where the template invented artwork: the real
        # photograph is under it, and repainting it is how a defective template used to
        # damage the second layer as well.
        tolerance = template_policy.for_job(job)
        checked = template_check.compare(concept, overlay, scene, tolerance)
        mask_notes["templateMajor"] = checked["major"]
        if checked["major"]:
            disbelieved = template_check.added_artwork_mask(
                concept, overlay, scene, tolerance, size=size)
            mask_notes["disbelievedRegions"] = len(checked["regions"])
    mask, edit_fraction = edit_mask.build_clean_photo_mask(
        size, overlay, scene, disbelieved=disbelieved)
    log.info("clean photo mask editFraction=%.3f", edit_fraction)

    prompt = prompts.render("clean-photo", width=canvas["width"], height=canvas["height"])
    if instruction:
        # A targeted correction of this one asset: the finding, appended to the
        # full prompt so every rule about what to keep still applies.
        prompt += (
            "\n\nCORRECTION REQUIRED for this photograph (change only this; keep "
            f"everything else as it is):\n{instruction}"
        )
    suffix = f"-after-repair-{int(job.get('overlayRepairs', 0) or 0)}" if after_repair else ""
    if instruction:
        suffix = f"-repair-{int(job.get('repairs_asset', 0) or 0)}"

    # Reuse the stored photograph when every input is identical. A repair (with
    # an instruction, or against a repaired overlay) is a deliberate new
    # generation and is never answered from the cache.
    cache_id = None
    if not after_repair and not instruction:
        cache_id = stage_cache.key(
            Stage.CLEAN_PHOTO, concept=artifacts.sha256_hex(concept),
            overlay=artifacts.sha256_hex(overlay) if overlay is not None else "none",
            textBoxes=stage_cache.digest(
                [(l["x"], l["y"], l["width"], l["height"]) for l in scene["textLayers"]]
            ),
            prompt=stage_cache.prompt_digest(prompt), model=conf.IMAGE_MODEL,
            canvas=canvas,
        )
        cached = stage_cache.lookup(job, Stage.CLEAN_PHOTO, cache_id)
        if cached:
            stage_cache.hit(job_id, Stage.CLEAN_PHOTO)
            jobs.record_artifacts(job_id, Stage.CLEAN_PHOTO, attempt, cached["pointers"])
            return {**cached["result"], "cacheHit": True}

    result = image.edit(
        prompt,
        concept,
        canvas["width"],
        canvas["height"],
        transparent=False,
        mask=mask,
        operation_id=f"{job_id}:{Stage.CLEAN_PHOTO}:{attempt}{suffix}",
    )
    telemetry.record_image_call(job_id, Stage.CLEAN_PHOTO)

    key = artifacts.artifact_key(
        job_id, Stage.CLEAN_PHOTO, attempt, f"photo{suffix}.png"
    )
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    _trace(job_id, Stage.CLEAN_PHOTO, attempt, f"provider-trace-photo{suffix or ''}.json",
           prompt_name="clean-photo", prompt=prompt, concept=concept, mask=mask,
           result=result, stored=stored,
           operation_id=f"{job_id}:{Stage.CLEAN_PHOTO}:{attempt}{suffix}",
           notes={"editFraction": round(float(edit_fraction), 4),
                  "afterOverlayRepair": bool(after_repair),
                  "overlaySha256": artifacts.sha256_hex(overlay) if overlay is not None else None,
                  **mask_notes})
    pointers = {
        "background": stored["key"],
        "sha256": stored["sha256"],
        "conceptSha256": artifacts.sha256_hex(concept),
    }
    jobs.record_artifacts(job_id, Stage.CLEAN_PHOTO, attempt, pointers)
    out = {
        "background": stored["key"],
        "sha256": stored["sha256"],
        "requestId": result["requestId"],
    }
    if cache_id:
        stage_cache.remember(job_id, cache_id, {"pointers": pointers, "result": out})
    return out


def _separate(job_id, attempt, *, stage, prompt_name, artifact_name, label,
              transparent, prompt=None, notes=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, stage)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    concept = _concept_bytes(job_id, job)

    if prompt is None:
        prompt = prompts.render(
            prompt_name, width=canvas["width"], height=canvas["height"],
            components=_removal_block(job_id, job),
        )
    cache_id = stage_cache.key(
        stage, concept=artifacts.sha256_hex(concept),
        prompt=stage_cache.prompt_digest(prompt), model=conf.IMAGE_MODEL,
        canvas=canvas, transparent=transparent,
    )
    cached = stage_cache.lookup(job, stage, cache_id)
    if cached:
        stage_cache.hit(job_id, stage)
        jobs.record_artifacts(job_id, stage, attempt, cached["pointers"])
        return {**cached["result"], "cacheHit": True}

    result = image.edit(
        prompt,
        concept,
        canvas["width"],
        canvas["height"],
        transparent=transparent,
        operation_id=f"{job_id}:{stage}:{attempt}",
    )
    telemetry.record_image_call(job_id, stage)

    key = artifacts.artifact_key(job_id, stage, attempt, artifact_name)
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    _trace(job_id, stage, attempt, f"provider-trace-{label}.json",
           prompt_name=prompt_name, prompt=prompt, concept=concept, mask=None,
           result=result, stored=stored, operation_id=f"{job_id}:{stage}:{attempt}",
           notes=notes)
    pointers = {
        label: stored["key"],
        "sha256": stored["sha256"],
        "conceptSha256": artifacts.sha256_hex(concept),
    }
    jobs.record_artifacts(job_id, stage, attempt, pointers)
    out = {
        label: stored["key"],
        "sha256": stored["sha256"],
        "requestId": result["requestId"],
    }
    stage_cache.remember(job_id, cache_id, {"pointers": pointers, "result": out})
    return out


def _trace(job_id, stage, attempt, name, *, prompt_name, prompt, concept, mask, result,
           stored, operation_id, notes=None):
    """Write the restricted diagnostic record for this provider call (never raises).

    The provider's bytes are stored exactly as received, so `processing` is empty:
    nothing between the response and the artifact can have changed them. The first
    code that does is validation's resample, which works on a copy.
    """
    trace = provider_trace.build(
        stage=stage, operation_id=operation_id, prompt_name=prompt_name, prompt=prompt,
        inputs=[("approvedConcept", concept)], mask=mask, result=result,
        stored_sha256=stored["sha256"], stored_bytes=stored["bytes"],
        processing=[], notes=notes,
    )
    provider_trace.record(job_id, stage, attempt, name, trace)


def _removal_block(job_id, job):
    """The components the layout draws, as lines for the extraction prompt.

    Read from the decomposition plan EXTRACT_SCENE produced, so the overlay is
    told to remove exactly what is about to be rebuilt in code - no more. Empty
    (and the instruction harmless) for a scene with no plan.
    """
    scene_ref = (job.get("artifacts") or {}).get(Stage.EXTRACT_SCENE) or {}
    if not scene_ref.get("scene"):
        return ""
    scene = artifacts.get_json(job_id, scene_ref["scene"])
    components = validate_assets.editable_component_boxes(scene)
    lines = [
        f"the {_describe(component)} at x={component['box']['x']}, "
        f"y={component['box']['y']}, {component['box']['width']}x"
        f"{component['box']['height']} px"
        for component in components
    ]
    # Shading the concept painted over the photograph is drawn by the layout from
    # the first reconstruction (lib/readability.py). Left in the overlay it would
    # be a second copy of it - and, being dark, it reads as a solid panel.
    for effect in scene.get("effects") or []:
        if effect.get("kind") == "readabilityGradient" and effect.get("owner") == "photo":
            said = (effect.get("description") or "").strip().rstrip(".")
            lines.append(
                "the soft dark shading over the photograph behind the copy"
                + (f" ({said})" if said else "")
                + " - shading that fades into the photograph, not a panel"
            )
    return prompts.bullet_list(lines) if lines else ""


def _describe(component):
    if component["kind"] == "rasterIcon":
        return "pictogram (the symbol itself, not the circle behind it)"
    colour = f" {component['color']}" if component.get("color") else ""
    return f"{component.get('shape') or 'shape'}{colour}"


def _concept_bytes(job_id, job):
    """Read the one immutable concept both branches must edit.

    Deliberately reads the frozen approved concept from the job rather than
    accepting a key from workflow state, so neither branch can be pointed at the other's output.
    """
    key = jobs.approved_concept(job)["key"]
    return artifacts.get_bytes(job_id, key)


def repair_photo(job_id, attempt, asset_id=None):
    """Regenerate ONLY the photograph, told exactly what was wrong with it.

    The scene, the overlay and every other asset are untouched. The instruction is
    the routed finding recorded by REPAIR_ASSET.
    """
    job = jobs.get_job_unscoped(job_id)
    if job.get("flow") == "layout-first":
        from lib.stages import plan_layout

        return plan_layout.repair_photo(job_id, attempt)
    pending = job.get("photoRepair") or {}
    if int(pending.get("forAttempt", 0)) != int(attempt):
        raise AssetInvalid("No photo repair was planned for this attempt.")
    return clean_photo(job_id, attempt, instruction=pending.get("instruction") or "")
