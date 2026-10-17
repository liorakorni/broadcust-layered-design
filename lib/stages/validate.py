"""VALIDATE_ASSETS: prove the separated layers are usable before assembling.

Runs both checks described in lib/validate_assets.py - the measured alpha
channel and a vision inspection of contrasting composites - because during the
probe the model itself said it could not confirm real transparency from a
preview, and a histogram cannot tell a photograph baked into the overlay from
legitimate artwork.

On a transparency failure this stage requests a bounded overlay repair rather
than failing the job outright (SPEC section 4). The bound lives in a DynamoDB
conditional update so it holds per job, not per task retry.
"""

from __future__ import annotations

import io

import conf
from lib import artifacts, jobs, validate_assets
from lib.errors import InvalidTransparency, OverlayRepairNeeded, Stage
from lib.providers import vision

COMPOSITE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "photographicLeftovers",
        "textLeftovers",
        "artworkPresent",
        "leftoverDescription",
        "notes",
    ],
    "properties": {
        # A photographic scene surviving in the overlay is the failure a
        # histogram cannot see.
        "photographicLeftovers": {"type": "boolean"},
        "textLeftovers": {"type": "boolean"},
        "artworkPresent": {"type": "boolean"},
        # What and where, so a repair can target the offending elements instead
        # of regenerating the whole overlay and losing the parts that were right.
        "leftoverDescription": {"type": "string"},
        "notes": {"type": "string"},
    },
}

_COMPOSITE_PROMPT = """These three images show the SAME graphic overlay composited onto
three different solid backgrounds: magenta, white and black.

Judge the OVERLAY artwork only, not the backgrounds.

- photographicLeftovers: true if any part of a photographic scene survives in the
  overlay - sky, landscape, buildings, foliage, skin, or photographic texture.
  A flat graphic panel is not photographic.
- textLeftovers: true if any readable word, letter, number or logo remains.
  Pictogram icons - a bed, car, plane, phone or envelope symbol - are intended
  decoration and are not text.
- artworkPresent: true if real decorative artwork is present - panels, ribbons,
  badges, frames or ornaments.
- leftoverDescription: when anything above is a problem, name each offending
  element and where it sits, for example "a logo mark of cypress trees and a
  house in the top-right corner". Empty string when there is nothing to remove.

If the artwork looks identical against all three backgrounds, the transparency is
genuine. If it sits on a visible rectangle that changes with the background, it is
not."""


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.VALIDATE_ASSETS)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])

    background_key = job["artifacts"][Stage.CLEAN_PHOTO]["background"]
    overlay_key = job["artifacts"][Stage.DECORATION_OVERLAY]["decoration"]
    background = artifacts.get_bytes(job_id, background_key)
    overlay = artifacts.get_bytes(job_id, overlay_key)

    validate_assets.validate_background(background, canvas)

    try:
        stats = validate_assets.validate_overlay(overlay, scene, canvas)
        inspect_composites(overlay, canvas, job_id, attempt)
    except InvalidTransparency as exc:
        # Raises OverlayRepairNeeded when a repair is still available, which the
        # workflow routes back to a targeted overlay edit. Otherwise the original
        # terminal error propagates.
        _request_repair_or_fail(job_id, exc)
        raise

    result = {
        "background": background_key,
        "decoration": overlay_key,
        "alpha": {k: round(v, 4) for k, v in stats.items() if k != "alphaMax"},
    }
    key = artifacts.artifact_key(job_id, Stage.VALIDATE_ASSETS, attempt, "validation.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(
        job_id, Stage.VALIDATE_ASSETS, attempt, {"validation": stored["key"]}
    )
    return result


def inspect_composites(overlay_bytes, canvas, job_id, attempt):
    """Ask vision to inspect the overlay over three contrasting backgrounds.

    Three colours rather than one: artwork that looks correct on white may be
    invisible on white because it IS white, and a baked-in rectangle only becomes
    obvious when the background behind it changes.
    """
    composites = [
        composite_on(overlay_bytes, colour)
        for colour in ((255, 0, 255), (255, 255, 255), (0, 0, 0))
    ]
    verdict = vision.analyze(
        _COMPOSITE_PROMPT,
        composites,
        schema=COMPOSITE_SCHEMA,
        schema_name="overlay_inspection_v1",
        model=conf.REVIEW_MODEL,
        operation_id=f"{job_id}:validate_assets:{attempt}",
    )

    problems = []
    if verdict["photographicLeftovers"]:
        problems.append("photographic scene remains in the decoration layer")
    if verdict["textLeftovers"]:
        problems.append("text or a logo remains in the decoration layer")
    if not verdict["artworkPresent"]:
        problems.append("no decorative artwork was found in the decoration layer")
    if problems:
        # Persisted before raising so the repair can act on what the inspection
        # actually saw, rather than re-deriving it from the error message.
        jobs.record_overlay_defect(job_id, verdict["leftoverDescription"])
        raise InvalidTransparency(
            "The decoration layer was not cleanly separated: " + "; ".join(problems)
        )
    return verdict


def composite_on(overlay_bytes, colour):
    """Flatten the overlay onto a solid colour and return PNG bytes."""
    from PIL import Image

    rgba, _ = validate_assets.load_rgba(overlay_bytes)
    # Downscale before sending: the inspection is about what survives, not fine
    # detail, and a full-size PNG per colour is a large, slow request.
    rgba.thumbnail((768, 768), Image.LANCZOS)
    backdrop = Image.new("RGBA", rgba.size, colour + (255,))
    flattened = Image.alpha_composite(backdrop, rgba).convert("RGB")
    buf = io.BytesIO()
    flattened.save(buf, format="PNG")
    return buf.getvalue()


def _request_repair_or_fail(job_id, exc):
    """Consume one of the bounded overlay repairs, if any remain.

    Raises OverlayRepairNeeded when one was available, because that class is how
    the workflow decides to repair rather than fail - Step Functions can branch
    on an exception class but not on a field inside it.

    Exceeding the bound returns instead, after rewriting the message so the
    caller sees that repair was attempted and exhausted rather than the same
    transparency complaint twice.
    """
    used = jobs.bump_counter(job_id, "overlayRepairs", conf.MAX_OVERLAY_REPAIRS)
    if used is None:
        exc.message = (
            f"{exc.message} Overlay repair was attempted "
            f"{conf.MAX_OVERLAY_REPAIRS} times without success."
        )
        exc.retryable = False
        return False
    raise OverlayRepairNeeded(exc.message) from exc
