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

FIDELITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["displaced", "description"],
    "properties": {
        "displaced": {"type": "boolean"},
        "description": {"type": "string"},
    },
}

_FIDELITY_PROMPT = """Image 1 is the approved concept. Image 2 is the clean photograph with the
separated decoration overlay stacked on top of it. Text is intentionally absent
from image 2.

Judge ONLY whether image 2 restores the concept's artwork in the same place.

- displaced: true if a panel, ribbon, badge, silhouette or the main photographic
  subject sits materially elsewhere, at a materially different size, or is missing,
  compared with image 1. Small edge differences and the absent text are NOT
  displacement.
- description: when displaced, name each element and say where it is in image 1
  versus image 2, so the overlay can be repaired. Empty string otherwise."""

_COMPOSITE_PROMPT = """Image 1 is the APPROVED CONCEPT: the finished design this overlay was
separated from. Images 2, 3 and 4 show the SAME separated graphic overlay
composited onto three different solid backgrounds: magenta, white and black.

Judge the OVERLAY artwork only (images 2-4), not the backgrounds. Use image 1 to
learn what is intended artwork and what is not.

INTENDED ARTWORK - never report any of this, even when it looks dark, grainy or
rough. If a panel, background surface, frame, ribbon, stripe, ornament or pattern
is part of the graphic design in image 1, it belongs in the overlay:
- A flat panel or full-canvas background with a grain, noise, scratch, grunge,
  paper, concrete or stone surface texture is GRAPHIC ARTWORK, not photography.
  A texture applied to a graphic surface is not a photographic scene.

- photographicLeftovers: true ONLY if the overlay still contains part of the
  concept's PHOTOGRAPH: real-world scene content such as sky, landscape,
  buildings, streets, foliage, people, products, skin, or the lighting and depth
  of a real photographed place. A flat graphic panel is not photographic, and
  neither is the surface texture of one.
- textLeftovers: true if any readable word, letter, number or logo remains.
  Pictogram icons - a bed, car, plane, phone or envelope symbol - are intended
  decoration and are not text.
- artworkPresent: true if real decorative artwork is present - panels, ribbons,
  badges, frames or ornaments.
- leftoverDescription: when photographicLeftovers or textLeftovers is true, name
  each offending element and where it sits, for example "a logo mark of cypress
  trees and a house in the top-right corner". Never list a graphic panel,
  background surface or ornament that is present in image 1 as an offending
  element. Empty string when there is nothing to remove.

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

    concept = artifacts.get_bytes(job_id, jobs.approved_concept(job)["key"])
    try:
        stats = validate_assets.validate_overlay(overlay, scene, canvas)
        inspect_composites(overlay, concept, canvas, job_id, attempt)
        # Regeneration can change alignment. Re-stacking the separated layers and
        # comparing with the approved concept catches material displacement, which
        # is repaired at the asset rather than excused by editing the scene.
        inspect_fidelity(background, overlay, concept, job_id, attempt)
    except InvalidTransparency as exc:
        # Every rejection records what is wrong, not only the vision ones: a
        # repair that was told the previous round's finding would be repairing
        # the wrong thing.
        jobs.record_overlay_defect(job_id, getattr(exc, "defect", None) or exc.message)
        # Raises OverlayRepairNeeded when a repair is still available, which the
        # workflow routes back to a regenerated overlay. Otherwise the original
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
        job_id,
        Stage.VALIDATE_ASSETS,
        attempt,
        {"validation": stored["key"], "conceptSha256": artifacts.sha256_hex(concept)},
    )
    return result


def inspect_composites(overlay_bytes, concept_bytes, canvas, job_id, attempt):
    """Ask vision to inspect the overlay over three contrasting backgrounds.

    Three colours rather than one: artwork that looks correct on white may be
    invisible on white because it IS white, and a baked-in rectangle only becomes
    obvious when the background behind it changes.

    The approved concept is sent first. Without it the model cannot tell a
    textured graphic panel that the design intends (a grainy black background,
    say) from a photograph, and it rejected exactly such a panel - after which the
    repair removed the panel. The concept is what defines "intended artwork".
    """
    composites = [
        _thumbnail_png(concept_bytes),
        *(
            composite_on(overlay_bytes, colour)
            for colour in ((255, 0, 255), (255, 255, 255), (0, 0, 0))
        ),
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
        # Carried on the error so `run` persists what the inspection actually saw
        # for the repair, rather than the repair re-deriving it from a message.
        error = InvalidTransparency(
            "The decoration layer was not cleanly separated: " + "; ".join(problems)
        )
        error.defect = verdict["leftoverDescription"] or "; ".join(problems)
        raise error
    return verdict


def inspect_fidelity(background_bytes, overlay_bytes, concept_bytes, job_id, attempt):
    """Check the stacked layers against the approved concept.

    Material displacement is not excused by adjusting the scene: it is recorded as
    the overlay defect so the bounded targeted repair acts on it.
    """
    from PIL import Image

    photo, _ = validate_assets.load_rgba(background_bytes)
    overlay, _ = validate_assets.load_rgba(overlay_bytes)
    overlay = overlay.resize(photo.size, Image.LANCZOS)
    stacked = Image.alpha_composite(photo, overlay).convert("RGB")
    stacked.thumbnail((768, 768), Image.LANCZOS)
    buf = io.BytesIO()
    stacked.save(buf, format="PNG")

    verdict = vision.analyze(
        _FIDELITY_PROMPT,
        (concept_bytes, buf.getvalue()),
        schema=FIDELITY_SCHEMA,
        schema_name="composite_fidelity_v1",
        model=conf.REVIEW_MODEL,
        operation_id=f"{job_id}:validate_assets_fidelity:{attempt}",
    )
    if verdict["displaced"]:
        error = InvalidTransparency(
            "The separated layers do not reconstruct the approved concept: "
            + (verdict["description"] or "material displacement")
        )
        error.defect = verdict["description"] or "material displacement"
        raise error
    return verdict


def _thumbnail_png(image_bytes):
    """Downscale an RGB image to the same bounds used for the composites."""
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB")
    rgb.thumbnail((768, 768), Image.LANCZOS)
    buf = io.BytesIO()
    rgb.save(buf, format="PNG")
    return buf.getvalue()


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
