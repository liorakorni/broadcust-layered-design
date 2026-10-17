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

    Deliberately reads CONCEPT's own artifact rather than accepting a key from
    workflow state, so neither branch can be pointed at the other's output.
    """
    key = job["artifacts"][Stage.CONCEPT]["concept"]
    return artifacts.get_bytes(job_id, key)
