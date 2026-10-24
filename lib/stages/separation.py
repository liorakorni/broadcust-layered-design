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

from lib import artifacts, edit_mask, jobs, prompts
from lib.errors import AssetInvalid, Stage
from lib.providers import image

log = logging.getLogger("layered-design.separation")


def decoration_overlay(job_id, attempt, asset_id=None):
    return _separate(
        job_id,
        attempt,
        stage=Stage.DECORATION_OVERLAY,
        prompt_name="decoration-overlay",
        artifact_name="overlay.png",
        label="decoration",
        transparent=True,
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
    prompt = (
        prompts.render(
            "decoration-overlay", width=canvas["width"], height=canvas["height"]
        )
        + "\n\n"
        + prompts.render(
            "repair-overlay",
            defects=prompts.bullet_list(
                [defect] if defect else ["The previous overlay failed validation."]
            ),
        )
    )

    round_number = int(job.get("overlayRepairs", 1) or 1)
    result = image.edit(
        prompt,
        concept,
        canvas["width"],
        canvas["height"],
        transparent=True,
        operation_id=f"{job_id}:overlay_repair:{round_number}",
    )

    key = artifacts.artifact_key(
        job_id, Stage.DECORATION_OVERLAY, attempt, f"overlay-repair-{round_number}.png"
    )
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
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
    }


def clean_photo(job_id, attempt, asset_id=None, after_repair=False):
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
    if not overlay_ref.get("decoration"):
        raise AssetInvalid(
            "CLEAN_PHOTO needs the decoration overlay to know what to repaint, but "
            "none has been produced."
        )
    overlay = artifacts.get_bytes(job_id, overlay_ref["decoration"])
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])

    from PIL import Image

    with Image.open(io.BytesIO(concept)) as img:
        size = img.size
    mask, edit_fraction = edit_mask.build_clean_photo_mask(size, overlay, scene)
    log.info("clean photo mask editFraction=%.3f", edit_fraction)

    prompt = prompts.render("clean-photo", width=canvas["width"], height=canvas["height"])
    suffix = f"-after-repair-{int(job.get('overlayRepairs', 0) or 0)}" if after_repair else ""
    result = image.edit(
        prompt,
        concept,
        canvas["width"],
        canvas["height"],
        transparent=False,
        mask=mask,
        operation_id=f"{job_id}:{Stage.CLEAN_PHOTO}:{attempt}{suffix}",
    )

    key = artifacts.artifact_key(
        job_id, Stage.CLEAN_PHOTO, attempt, f"photo{suffix}.png"
    )
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    jobs.record_artifacts(
        job_id,
        Stage.CLEAN_PHOTO,
        attempt,
        {
            "background": stored["key"],
            "sha256": stored["sha256"],
            "conceptSha256": artifacts.sha256_hex(concept),
        },
    )
    return {
        "background": stored["key"],
        "sha256": stored["sha256"],
        "requestId": result["requestId"],
    }


def _separate(job_id, attempt, *, stage, prompt_name, artifact_name, label,
              transparent):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, stage)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    concept = _concept_bytes(job_id, job)

    prompt = prompts.render(
        prompt_name, width=canvas["width"], height=canvas["height"]
    )
    result = image.edit(
        prompt,
        concept,
        canvas["width"],
        canvas["height"],
        transparent=transparent,
        operation_id=f"{job_id}:{stage}:{attempt}",
    )

    key = artifacts.artifact_key(job_id, stage, attempt, artifact_name)
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    jobs.record_artifacts(
        job_id,
        stage,
        attempt,
        {
            label: stored["key"],
            "sha256": stored["sha256"],
            "conceptSha256": artifacts.sha256_hex(concept),
        },
    )
    return {
        label: stored["key"],
        "sha256": stored["sha256"],
        "requestId": result["requestId"],
    }


def _concept_bytes(job_id, job):
    """Read the one immutable concept both branches must edit.

    Deliberately reads the frozen approved concept from the job rather than
    accepting a key from workflow state, so neither branch can be pointed at the other's output.
    """
    key = jobs.approved_concept(job)["key"]
    return artifacts.get_bytes(job_id, key)
