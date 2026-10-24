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

import conf
from lib import artifacts, jobs, reference, telemetry
from lib.errors import ImpossibleContentRequirement, Stage
from lib.providers import image

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
        return _use_supplied(job_id, attempt, supplied[asset_id])
    return _generate(job_id, attempt, asset_id, layer, request)


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
