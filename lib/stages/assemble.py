"""ASSEMBLE and RENDER.

ASSEMBLE publishes the validated assets to stable URLs and builds the HTML. The
assets must be published first because the HTML references them by URL, and an
HTML document pointing at S3 keys nobody can read is not a deliverable.

RENDER proves the document actually works, and persists both captures so REVIEW
inspects the real rendered pixels rather than a description of them.
"""

from __future__ import annotations

import copy

from lib import artifacts, assemble_html, jobs, media, render as renderer
from lib.errors import RenderFailed, Stage


def assemble(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.ASSEMBLE)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])

    published = _publish_assets(job_id, job, attempt)
    html = assemble_html.assemble(scene, published["urls"], canvas)

    scene, fitted = _fit_text(scene, html, canvas)
    if fitted:
        html = assemble_html.assemble(scene, published["urls"], canvas)

    key = artifacts.artifact_key(job_id, Stage.ASSEMBLE, attempt, "design.html")
    stored = artifacts.put_bytes(
        key, html.encode("utf-8"), "text/html; charset=utf-8"
    )
    # The scene is persisted here as well as at EXTRACT_SCENE because fitting may
    # have changed it, and RENDER and REVIEW must inspect the geometry that
    # actually describes this document rather than the geometry that was asked
    # for. The original stays readable at its own key.
    scene_key = artifacts.artifact_key(job_id, Stage.ASSEMBLE, attempt, "scene.json")
    stored_scene = artifacts.put_json(scene_key, scene)
    record = {
        "html": stored["key"],
        "htmlHash": stored["sha256"],
        "scene": stored_scene["key"],
        "fitted": fitted,
        # FINALIZE hashes this manifest, so it is persisted rather than only
        # returned through workflow state, which carries pointers alone.
        "assets": published["manifest"],
    }
    jobs.record_artifacts(job_id, Stage.ASSEMBLE, attempt, record)
    return record


def _fit_text(scene, html, canvas):
    """Resize any text that does not fit its box, measured in the browser.

    EXTRACT_SCENE reads box sizes off a concept image drawn in some other
    typeface and sizes each box for a single line. The design then sets in Heebo,
    which runs wider, so a box that is a few percent too narrow wraps to a second
    line and overflows by roughly double. Catching that here costs one browser
    launch; catching it at REVIEW costs a paid correction round.

    Returns the scene to assemble from and a record of what changed.
    """
    measured = renderer.fit_text_layers(html, canvas)
    if not measured:
        return scene, {}

    element_ids = assemble_html.element_ids(scene)
    adjusted = copy.deepcopy(scene)
    changes = {}

    for layer in adjusted["textLayers"]:
        change = measured.get(element_ids.get(layer["id"]))
        if not change:
            continue
        layer["fontSize"] = change["fitted"]
        changes[layer["id"]] = change

    return adjusted, changes


def _publish_assets(job_id, job, attempt):
    """Upload every delivered asset to stable media and collect its URL."""
    entries = []
    urls = {}

    for asset_id, key in _asset_keys(job).items():
        data = artifacts.get_bytes(job_id, key)
        delivered = media.publish(data, job_id=job_id, asset_id=asset_id)
        urls[asset_id] = delivered["url"]
        entries.append(
            {
                "assetId": asset_id,
                "key": key,
                "sha256": artifacts.sha256_hex(data),
                "url": delivered["url"],
            }
        )
    return {"urls": urls, "manifest": entries}


def _asset_keys(job):
    """Map each asset reference to the artifact key holding its bytes."""
    produced = job.get("artifacts", {})
    keys = {
        "background": produced[Stage.CLEAN_PHOTO]["background"],
        "decoration": produced[Stage.DECORATION_OVERLAY]["decoration"],
    }
    for entry in produced.get(Stage.INDEPENDENT_ASSETS, {}).get("items", []):
        keys[entry["assetId"]] = entry["key"]
    return keys


def render(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.RENDER)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    # ASSEMBLE's scene, not EXTRACT_SCENE's: text fitting may have changed font
    # sizes, and checking the document against superseded geometry would report
    # differences that are not defects.
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.ASSEMBLE]["scene"])
    html_key = job["artifacts"][Stage.ASSEMBLE]["html"]
    html = artifacts.get_bytes(job_id, html_key).decode("utf-8")

    result = renderer.render(html, canvas, scene)
    checks = result["checks"]

    if checks["textLayers"] != len(scene["textLayers"]):
        raise RenderFailed(
            f"The scene defines {len(scene['textLayers'])} text layers but "
            f"{checks['textLayers']} rendered."
        )

    native_key = artifacts.artifact_key(job_id, Stage.RENDER, attempt, "native.png")
    feed_key = artifacts.artifact_key(job_id, Stage.RENDER, attempt, "feed.png")
    native = artifacts.put_bytes(native_key, result["native"], "image/png")
    feed = artifacts.put_bytes(feed_key, result["feed"], "image/png")

    # Render problems are returned rather than raised: REVIEW weighs them
    # alongside its own findings and the correction loop may fix them.
    out = {
        "native": native["key"],
        "feed": feed["key"],
        "problems": checks["problems"],
        "checksPerformed": checks.get("performed", []),
    }
    jobs.record_artifacts(job_id, Stage.RENDER, attempt, out)
    return out
