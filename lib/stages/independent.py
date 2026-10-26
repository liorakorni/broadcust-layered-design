"""INDEPENDENT_ASSETS: produce the separately placed images the scene asks for.

An independent asset is an inset photo, product shot or cut-out subject that the
scene positions in its own layer rather than as part of the background. Each is
generated with a transparent background so it composites over the photograph.

The workflow drives this as a Map over `assetIds`, bounded to three items at
concurrency two (SPEC section 3). Both bounds exist because every item is a paid
generation: a scene that asks for twelve insets should produce a clear refusal,
not a surprise bill.

Supplied assets are NOT regenerated. If the caller gave a logo, the layer uses
the caller's file; asking the model to reproduce a logo yields an approximation
of a trademark.
"""

from __future__ import annotations

import logging

import conf
from lib import artifacts, icon_extract, jobs, reference, telemetry
from lib.errors import ImpossibleContentRequirement, Stage
from lib.providers import image

log = logging.getLogger("layered-design.independent")

_ICON_PROMPT = """Redraw ONLY the pictogram symbol from this image - the phone, envelope, pin,
globe or arrow glyph - as a single flat icon on a fully transparent background.

Requirements:
- Keep the glyph's exact shape, weight and colour as it appears here.
- Remove the coloured circle, square or pill behind it completely, leaving
  genuine alpha transparency. Not white, not a drawn checkerboard, no backdrop,
  no border, no shadow.
- The glyph fills the frame with a small margin, centred.
- No text, no words, no numbers, no extra ornament."""

_PROMPT = """Create a single isolated {kind} on a fully transparent background.

{description}

Requirements:
- The subject fills the frame with a small margin, centred.
- Genuine alpha transparency everywhere around the subject. Not white, not a
  drawn checkerboard, no backdrop, no frame, no drop shadow on a panel.
- No text, no words, no logos, no watermarks.
- Photographic quality and lighting consistent with a premium marketing design."""


def plan_from_scene(scene, job_id):
    """List the asset IDs the workflow's Map should iterate.

    Supplied assets are included: each one still needs fetching and storing, it
    is simply not generated. The bound counts only generated items, since those
    are what cost money.
    """
    job = jobs.get_job_unscoped(job_id)
    request = artifacts.get_json(job_id, job["requestKey"])
    supplied = {a["id"] for a in request.get("suppliedAssets", [])}

    needed = [
        layer["assetId"]
        for layer in scene["imageLayers"]
        if layer["type"] == "image" and layer.get("assetId")
    ]
    generated = [a for a in needed if a not in supplied]
    if len(generated) > conf.MAX_INDEPENDENT_ASSETS:
        raise ImpossibleContentRequirement(
            f"The design needs {len(generated)} generated images; the limit is "
            f"{conf.MAX_INDEPENDENT_ASSETS}. Simplify the brief or supply the "
            "images."
        )
    return needed


def run(job_id, attempt, asset_id=None):
    if not asset_id:
        raise ImpossibleContentRequirement(
            "An independent asset task was dispatched without an assetId."
        )

    job = jobs.get_job_unscoped(job_id)
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])
    request = artifacts.get_json(job_id, job["requestKey"])

    supplied = {a["id"]: a for a in request.get("suppliedAssets", [])}
    layer = _find_layer(scene, asset_id)

    if asset_id in supplied:
        entry = _use_supplied(job_id, attempt, supplied[asset_id])
    elif _component_kind(scene, layer) == "rasterIcon":
        entry = _icon(job_id, attempt, asset_id, layer, scene, job)
    else:
        entry = _generate(job_id, attempt, asset_id, layer, request)
    # Recorded here rather than from the Map's collected output: ASSEMBLE reads
    # the job, and a Map result that never reaches it is an asset with no
    # delivered URL.
    return jobs.record_asset_item(job_id, attempt, entry)


def _component_kind(scene, layer):
    """How the decomposition plan says this layer is delivered, if it says."""
    for component in scene.get("components") or []:
        if component["layerId"] == layer["id"]:
            return component["kind"]
    return None


def _icon(job_id, attempt, asset_id, layer, scene, job):
    """Cut the pictogram out of the approved concept; generate only if that fails.

    Extraction keeps the icon the customer approved and costs no image call. The
    fallback is one bounded generation from the same crop. If neither produces an
    isolated pictogram the job fails: a contact icon that cannot be separated is
    missing editability, which is not a defect to downgrade to a warning, and
    leaving it baked into the artwork is exactly what this work removes.
    """
    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    concept = artifacts.get_bytes(job_id, jobs.approved_concept(job)["key"])
    box = {k: int(layer[k]) for k in ("x", "y", "width", "height")}

    data, fraction = icon_extract.extract(
        concept, box, canvas, circle_color=_circle_color(scene, layer)
    )
    if data and icon_extract.looks_like_a_glyph(fraction):
        log.info(
            "icon extracted without a model call jobId=%s assetId=%s alpha=%.3f",
            job_id, asset_id, fraction,
        )
        return _store_icon(job_id, attempt, asset_id, data, source="extracted",
                           alphaFraction=round(fraction, 4))

    crop = icon_extract.crop_png(concept, box, canvas)
    if crop is None:
        raise ImpossibleContentRequirement(
            f"The contact icon {asset_id!r} cannot be separated: its box "
            f"{box} is too small to read in the approved concept."
        )
    log.info(
        "icon extraction fell back to one generation jobId=%s assetId=%s alpha=%.3f",
        job_id, asset_id, fraction,
    )
    result = image.edit(
        _ICON_PROMPT,
        crop,
        max(64, box["width"]),
        max(64, box["height"]),
        transparent=True,
        operation_id=f"{job_id}:icon:{asset_id}:{attempt}",
    )
    telemetry.record_image_call(job_id, Stage.INDEPENDENT_ASSETS)
    generated = icon_extract.glyph_fraction(result["bytes"])
    if not icon_extract.looks_like_a_glyph(generated):
        raise ImpossibleContentRequirement(
            f"The contact icon {asset_id!r} could not be produced as an "
            f"independent transparent image (opaque fraction {generated:.0%} after "
            "one extraction and one generation). It must not be left inside the "
            "artwork, where it cannot be edited."
        )
    return _store_icon(job_id, attempt, asset_id, result["bytes"], source="generated",
                       alphaFraction=round(generated, 4),
                       requestId=result["requestId"])


def _circle_color(scene, layer):
    """The colour of the shape behind this pictogram, when the plan pairs them."""
    components = scene.get("components") or []
    icon = next((c for c in components if c["layerId"] == layer["id"]), None)
    if not icon or not icon.get("pairsWith"):
        return None
    for component in components:
        if component["kind"] != "editableShape" or component.get("pairsWith") != icon["pairsWith"]:
            continue
        shape = next(
            (s for s in scene.get("shapeLayers") or [] if s["id"] == component["layerId"]),
            None,
        )
        if shape and shape.get("color"):
            return shape["color"]
    return None


def _store_icon(job_id, attempt, asset_id, data, **extra):
    name = f"assets/{asset_id}.png"
    key = artifacts.artifact_key(job_id, Stage.INDEPENDENT_ASSETS, attempt, name)
    stored = artifacts.put_bytes(key, data, "image/png")
    return {
        "assetId": asset_id,
        "key": stored["key"],
        "sha256": stored["sha256"],
        **extra,
    }


def _find_layer(scene, asset_id):
    for layer in scene["imageLayers"]:
        if layer.get("assetId") == asset_id:
            return layer
    raise ImpossibleContentRequirement(
        f"No scene layer references asset {asset_id!r}."
    )


def _use_supplied(job_id, attempt, asset):
    """Store the caller's own file rather than generating a lookalike."""
    fetched = reference.fetch(asset["url"])
    name = f"assets/{asset['id']}.png"
    key = artifacts.artifact_key(job_id, Stage.INDEPENDENT_ASSETS, attempt, name)
    stored = artifacts.put_bytes(key, fetched["bytes"], fetched["mimeType"])
    return {
        "assetId": asset["id"],
        "key": stored["key"],
        "sha256": stored["sha256"],
        "source": "supplied",
    }


def _generate(job_id, attempt, asset_id, layer, request):
    prompt = _PROMPT.format(
        kind=layer.get("role") or "image",
        description=_describe(asset_id, request),
    )
    result = image.generate(
        prompt,
        layer["width"],
        layer["height"],
        transparent=True,
        operation_id=f"{job_id}:independent:{asset_id}:{attempt}",
    )
    telemetry.record_image_call(job_id, Stage.INDEPENDENT_ASSETS)
    name = f"assets/{asset_id}.png"
    key = artifacts.artifact_key(job_id, Stage.INDEPENDENT_ASSETS, attempt, name)
    stored = artifacts.put_bytes(key, result["bytes"], "image/png")
    return {
        "assetId": asset_id,
        "key": stored["key"],
        "sha256": stored["sha256"],
        "source": "generated",
        "requestId": result["requestId"],
    }


def _describe(asset_id, request):
    """Ground the asset in the brief rather than in the asset's own id.

    The brief is the only description available; an id like "image_2" says
    nothing, so the model gets the brief and infers what belongs in the inset.
    """
    return (
        f"The image is part of this marketing design:\n{request['brief']['post']}\n\n"
        f"It is the element identified as {asset_id!r} in the layout."
    )
