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
from lib import artifacts, dependencies, design_state, jobs, scene_patch, validate_assets
from lib.errors import InvalidScene, InvalidTransparency, OverlayRepairNeeded, Stage
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
    "required": ["displaced", "description", "missingShading"],
    "properties": {
        "displaced": {"type": "boolean"},
        "description": {"type": "string"},
        "missingShading": {"enum": ["none", "top", "bottom", "left", "right"]},
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
- missingShading: a plain soft dark (black or near-black) shading or fade along ONE
  edge of image 1 that is absent from image 2 - a translucent gradient or darkened
  band whose only job is to make text readable over the photograph. Name the edge
  it hugs ("top", "bottom", "left" or "right"), or "none" when image 2 has it or
  image 1 has none. The layout draws this in code, so it is NOT displacement:
  never set displaced, and never mention it in description, because of it. Any
  OTHER missing element (a coloured panel, ribbon, badge, graphic) is displacement.
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

    if job.get("flow") == "layout-first":
        from lib.stages import plan_layout

        return plan_layout.validate_photo(job_id, job, attempt)

    from lib.stages.assemble import scene_key_for

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    scene = artifacts.get_json(job_id, scene_key_for(job, attempt))

    background_key = job["artifacts"][Stage.CLEAN_PHOTO]["background"]
    overlay_key = job["artifacts"][Stage.DECORATION_OVERLAY]["decoration"]
    background = artifacts.get_bytes(job_id, background_key)
    overlay = artifacts.get_bytes(job_id, overlay_key)
    concept = artifacts.get_bytes(job_id, jobs.approved_concept(job)["key"])

    shas = {
        "background": artifacts.sha256_hex(background),
        "decoration": artifacts.sha256_hex(overlay),
    }
    concept_sha = artifacts.sha256_hex(concept)
    compositing = design_state.compositing_hash(scene)
    entries = {}

    def bound(**extra):
        return {
            "assets": dict(shas), "concept": concept_sha, "compositing": compositing, **extra,
        }

    def checked(name, inputs, compute, *, bind):
        """Run a check unless this exact check already passed on these inputs.

        The key carries every input (asset hashes, concept, model, prompt and the
        validation-policy version), so a different photograph, overlay or prompt
        can never reuse another's result. Only a pass is cached: a failure is
        acted on, not remembered.
        """
        key = dependencies.cache_key(name, **inputs)
        hit = jobs.cache_get(job, "validationCache", key)
        if hit and hit.get("status") == "passed":
            jobs.add_metric(job_id, "validationCacheHits")
            entries[name] = _entry(key, bind, cache_hit=True)
            return hit.get("detail")
        detail = compute()
        jobs.cache_put(
            job_id, "validationCache", key,
            {"status": "passed", **({"detail": detail} if detail else {})},
        )
        jobs.add_metric(job_id, "validationCacheMisses")
        entries[name] = _entry(key, bind, cache_hit=False)
        return detail

    # Local, deterministic checks are recomputed (they cost milliseconds); their
    # results are still bound to the hashes they examined.
    validate_assets.validate_background(background, canvas)
    entries["photoValidation"] = _entry(
        dependencies.cache_key("photoValidation", background=shas["background"]),
        {"assets": {"background": shas["background"]}}, cache_hit=False,
    )
    try:
        stats = validate_assets.validate_overlay(overlay, scene, canvas)
        entries["overlayValidation"] = _entry(
            dependencies.cache_key(
                "overlayValidation", decoration=shas["decoration"],
                samples=dependencies.digest(scene.get("overlaySamples", [])),
                canvas=canvas,
            ),
            {"assets": {"decoration": shas["decoration"]},
             "compositing": compositing}, cache_hit=False,
        )
        checked(
            "composites",
            {"decoration": shas["decoration"], "concept": concept_sha,
             "model": conf.REVIEW_MODEL, "prompt": dependencies.digest(_COMPOSITE_PROMPT)},
            lambda: _no_detail(inspect_composites(overlay, concept, canvas, job_id, attempt)),
            bind={"assets": {"decoration": shas["decoration"]}, "concept": concept_sha},
        )
        # Regeneration can change alignment. Re-stacking the separated layers and
        # comparing with the approved concept catches material displacement, which
        # is repaired at the asset rather than excused by editing the scene.
        fidelity = checked(
            "fidelity",
            {**shas, "concept": concept_sha, "model": conf.REVIEW_MODEL,
             "prompt": dependencies.digest(_FIDELITY_PROMPT)},
            lambda: _fidelity_detail(
                inspect_fidelity(background, overlay, concept, job_id, attempt)
            ),
            bind={"assets": dict(shas), "concept": concept_sha},
        )
        code_owned = _own_missing_shading(job_id, job, scene, attempt, fidelity)
        if code_owned and code_owned["action"] == "not-added":
            # The layout cannot take the gradient (shapes disabled, or no stacking
            # slot), so it is still missing: the overlay repair is the fallback.
            error = InvalidTransparency(
                "The separated layers do not reconstruct the approved concept: the "
                f"dark shading along the {code_owned['edge']} edge is missing."
            )
            error.defect = (
                f"A soft dark shading fading in from the {code_owned['edge']} edge is "
                "missing; restore it."
            )
            raise error
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
        "entries": entries,
        "policyVersion": dependencies.VALIDATION_POLICY_VERSION,
        "codeOwned": code_owned,
    }
    key = artifacts.artifact_key(job_id, Stage.VALIDATE_ASSETS, attempt, "validation.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(
        job_id,
        Stage.VALIDATE_ASSETS,
        attempt,
        {
            "validation": stored["key"],
            "conceptSha256": concept_sha,
            "entries": entries,
            "cacheHits": sorted(n for n, e in entries.items() if e["cacheHit"]),
        },
    )
    return result


def _no_detail(_verdict):
    return None


def _fidelity_detail(verdict):
    shading = verdict.get("missingShading") or "none"
    return {"missingShading": shading} if shading != "none" else None


def _own_missing_shading(job_id, job, scene, attempt, detail):
    """Give a readability gradient the concept has and the layers lack to the layout.

    A soft dark fade behind the text is simple CSS. Asking an image model to repaint
    it is what sent earlier jobs through overlay regeneration that could not
    succeed, because the model keeps dropping it. When the fidelity check finds
    only that gradient missing, the layout draws it - one owner, never a second
    copy - and the final review confirms it on the rendered page. No image call.

    Returns a record of what was done (or why nothing was), for the validation
    document. Never raises: a scene that cannot take the shape simply goes on to
    be judged as it is.
    """
    edge = (detail or {}).get("missingShading")
    if not edge:
        return None
    existing = [e for e in scene.get("effects", []) if e.get("kind") == "readabilityGradient"]
    if existing:
        return {"effect": "readabilityGradient", "edge": edge, "action": "kept",
                "owner": existing[0]["owner"]}
    try:
        patched = scene_patch.add_shape(scene, f"gradient-{edge}")
    except InvalidScene as exc:
        return {"effect": "readabilityGradient", "edge": edge, "action": "not-added",
                "reason": exc.message}
    key = artifacts.artifact_key(job_id, Stage.VALIDATE_ASSETS, attempt, "scene-code-owned.json")
    stored = artifacts.put_json(key, patched)
    jobs.set_working_scene(
        job_id, {"key": stored["key"], "forAttempt": int(attempt), "baseVersion": 0}
    )
    jobs.add_metric(job_id, "codeOwnedEffects")
    return {"effect": "readabilityGradient", "edge": edge, "action": "added",
            "owner": "layout", "scene": stored["key"], "imageGenerationCalls": 0}


def _entry(key, bound_to, *, cache_hit):
    return {
        "status": "passed",
        "key": key,
        "boundTo": bound_to,
        "policyVersion": dependencies.VALIDATION_POLICY_VERSION,
        "model": conf.REVIEW_MODEL,
        "cacheHit": cache_hit,
    }


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
