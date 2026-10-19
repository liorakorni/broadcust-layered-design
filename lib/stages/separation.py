"""CLEAN_PHOTO and DECORATION_OVERLAY: split the concept into two aligned layers.

Both stages edit the SAME immutable concept bytes (invariant 3, SPEC section 4).
Chaining them - cleaning the photo and then extracting decoration from the
cleaned result - produces an empty overlay, because the artwork was already
removed. The workflow runs them as sibling Parallel branches so chaining is
structurally impossible, and `_concept_bytes` below is the only way either one
obtains its input.

DECORATION_OVERLAY requires genuine RGBA. If the provider says it cannot produce
a transparent background, that is terminal: keying out white would destroy every
light-coloured graphic and every soft shadow edge.
"""

from __future__ import annotations

from lib import artifacts, jobs, prompts
from lib.errors import Stage
from lib.providers import image


def clean_photo(job_id, attempt, asset_id=None):
    return _separate(
        job_id,
        attempt,
        stage=Stage.CLEAN_PHOTO,
        prompt_name="clean-photo",
        artifact_name="photo.png",
        label="background",
        transparent=False,
    )


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
    """Edit the current overlay to remove what inspection rejected.

    SPEC section 4 bounds these repairs to two per job. This edits the overlay
    itself rather than re-running the separation from the concept: regenerating
    would discard the parts that were already correct and is as likely to
    reintroduce the defect as to fix it.

    The result is written under its own artifact name so the rejected overlay
    stays readable - artifacts are append-only, and a repair that made things
    worse needs to be diagnosable afterwards.
    """
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.DECORATION_OVERLAY)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    current_key = job["artifacts"][Stage.DECORATION_OVERLAY]["decoration"]
    overlay = artifacts.get_bytes(job_id, current_key)

    defect = (job.get("overlayDefect") or "").strip()
    prompt = prompts.render(
        "repair-overlay",
        width=canvas["width"],
        height=canvas["height"],
        defects=prompts.bullet_list(
            [defect] if defect else ["Any leftover word, letter, number or logo."]
        ),
    )

    round_number = int(job.get("overlayRepairs", 1) or 1)
    result = image.edit(
        prompt,
        overlay,
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
        {"decoration": stored["key"], "sha256": stored["sha256"]},
    )
    return {
        "decoration": stored["key"],
        "sha256": stored["sha256"],
        "repairRound": round_number,
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
        job_id, stage, attempt, {label: stored["key"], "sha256": stored["sha256"]}
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
