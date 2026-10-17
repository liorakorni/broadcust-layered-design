"""ASSEMBLE and RENDER.

ASSEMBLE publishes the validated assets to stable URLs and builds the HTML. The
assets must be published first because the HTML references them by URL, and an
HTML document pointing at S3 keys nobody can read is not a deliverable.

RENDER proves the document actually works, and persists both captures so REVIEW
inspects the real rendered pixels rather than a description of them.
"""

from __future__ import annotations

from lib import artifacts, assemble_html, jobs, media, render as renderer
from lib.errors import RenderFailed, Stage


def assemble(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.ASSEMBLE)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])

    published = _publish_assets(job_id, job, attempt)
    html = assemble_html.assemble(scene, published["urls"], canvas)

    key = artifacts.artifact_key(job_id, Stage.ASSEMBLE, attempt, "design.html")
    stored = artifacts.put_bytes(
        key, html.encode("utf-8"), "text/html; charset=utf-8"
    )
    record = {
        "html": stored["key"],
        "htmlHash": stored["sha256"],
        # FINALIZE hashes this manifest, so it is persisted rather than only
        # returned through workflow state, which carries pointers alone.
        "assets": published["manifest"],
    }
    jobs.record_artifacts(job_id, Stage.ASSEMBLE, attempt, record)
    return record


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
    scene = artifacts.get_json(job_id, job["artifacts"][Stage.EXTRACT_SCENE]["scene"])
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
    }
    jobs.record_artifacts(job_id, Stage.RENDER, attempt, out)
    return out
