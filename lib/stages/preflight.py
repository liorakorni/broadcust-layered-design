"""PREFLIGHT: prove the deployment can actually do the job before spending money.

SPEC section 4: validate request, authorized asset origins, configured image
editing, true-alpha capability, vision model, renderer, fonts and stable media
publishing - but do NOT perform paid image generation. So capability is checked
by configuration and by cheap liveness calls; the expensive proof that the image
model really returns alpha lives in scripts/probe_image.py and the integration
tests, and its result is recorded in docs/PROBE_RESULTS.md.

Every missing capability is collected and reported together. A deployment with
three problems should take one invocation to diagnose, not three.
"""

from __future__ import annotations

import conf
from lib import artifacts, jobs, schemas
from lib.errors import CapabilityMissing, Stage

#: Image models known to reject background="transparent". DECORATION_OVERLAY
#: depends on real alpha entirely, so a deployment configured with one of these
#: cannot work and should say so here rather than four stages later.
_NO_TRANSPARENCY = ("gpt-image-1", "gpt-image-2", "dall-e-2", "dall-e-3")

#: Deprecated with a 2027-01-14 shutdown. Refuse it before it starts failing.
_RETIRED_VISION = ("gpt-4o", "gpt-4-vision-preview", "gpt-4-turbo")


def run(job_id, attempt, asset_id=None, *, check_renderer=True):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.PREFLIGHT)
    request = artifacts.get_json(job_id, job["requestKey"])

    problems = []
    problems.extend(_check_configuration())
    problems.extend(_check_models())
    problems.extend(_check_origins(request))
    problems.extend(_check_fonts(request))
    problems.extend(_check_media())
    if check_renderer:
        problems.extend(_check_renderer())

    if problems:
        raise CapabilityMissing(
            "Preflight found missing capabilities: " + "; ".join(problems)
        )

    result = {
        "imageModel": conf.IMAGE_MODEL,
        "sceneModel": conf.SCENE_MODEL,
        "reviewModel": conf.REVIEW_MODEL,
        "copyModel": conf.COPY_MODEL,
        "canvas": {
            "width": request["canvas"]["width"],
            "height": request["canvas"]["height"],
        },
    }
    key = artifacts.artifact_key(job_id, Stage.PREFLIGHT, attempt, "preflight.json")
    stored = artifacts.put_json(key, result)
    jobs.record_artifacts(job_id, Stage.PREFLIGHT, attempt, {"preflight": stored["key"]})
    return {"preflight": stored["key"], **result}


def _check_configuration():
    problems = []
    for names, label in (
        (conf.STAGE_REQUIRED, "pipeline"),
        (conf.MEDIA_REQUIRED, "stable media publishing"),
    ):
        try:
            conf.validate(names)
        except conf.ConfigurationError as exc:
            problems.append(f"{label}: {exc}")
    return problems


def _check_models():
    problems = []
    if conf.IMAGE_MODEL in _NO_TRANSPARENCY:
        problems.append(
            f"image model {conf.IMAGE_MODEL} cannot return transparent backgrounds, "
            "which the decoration overlay requires"
        )
    for name, model in (
        ("scene", conf.SCENE_MODEL),
        ("review", conf.REVIEW_MODEL),
        ("copy", conf.COPY_MODEL),
    ):
        if model in _RETIRED_VISION:
            problems.append(f"{name} model {model} is retired")
        if not model:
            problems.append(f"{name} model is not configured")
    return problems


def _check_origins(request):
    """The reference image must come from an authorized origin (SPEC section 6)."""
    problems = []
    urls = [request["referenceImageUrl"]]
    urls.extend(a["url"] for a in request.get("suppliedAssets", []))
    for url in urls:
        try:
            schemas.check_authorized_origin(url)
        except Exception as exc:
            problems.append(str(exc))
    return problems


def _check_fonts(request):
    """Brand fonts must be ones the renderer actually has installed.

    Chromium silently substitutes a missing family, so an unapproved font does
    not fail at render time - it produces a design in the wrong typeface. Catch
    it here instead.
    """
    brand = request.get("brand") or {}
    requested = brand.get("fontFamilies") or []
    unknown = [f for f in requested if f not in conf.APPROVED_FONT_FAMILIES]
    if unknown:
        return [
            "brand fonts are not installed in the renderer: "
            + ", ".join(sorted(unknown))
            + f" (installed: {', '.join(conf.APPROVED_FONT_FAMILIES)})"
        ]
    return []


def _check_media():
    """Confirm the credentials actually work, not merely that they are set.

    SPEC section 4 lists stable media publishing as a preflight capability. A
    ping is free, and discovering a bad key here rather than at ASSEMBLE saves
    the entire paid pipeline.
    """
    from lib import media

    try:
        media.check_available()
    except Exception as exc:
        return [f"stable media publishing: {exc}"]
    return []


def _check_renderer():
    """Confirm Chromium and the font set are present in this container.

    Imported lazily and kept cheap: this launches the browser on a blank page
    rather than rendering a design.
    """
    try:
        from lib import render
    except ImportError as exc:
        return [f"renderer is not available: {exc}"]
    try:
        render.check_available()
    except Exception as exc:
        return [f"renderer is not usable: {exc}"]
    return []
