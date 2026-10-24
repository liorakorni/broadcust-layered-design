"""EXPORT_DESIGN: the explicit save/export of an edited design.

This is the only place an edited design is turned back into HTML and a preview. It
runs on the stage Lambda (which has the browser), on an explicit request - never per
keystroke - and it makes no image-generation call: the assets are the stored ones,
checked by hash against what the snapshot references.
"""

from __future__ import annotations

from lib import (
    artifacts, assemble_html, design_state, jobs, media, render as renderer,
)
from lib.errors import AssetInvalid, DesignNotEditable, StaleDesignVersion, Stage
from lib.stages import assemble as assemble_stage


def export(job_id, version=None, owner_scope=None):
    job = jobs.get_job_unscoped(job_id)
    if owner_scope is not None and job.get("ownerScope") != owner_scope:
        raise DesignNotEditable("This design is not available.")
    pointer = design_state.current_pointer(job)
    if not pointer:
        raise DesignNotEditable("This job has no stored design to export.")
    current = int(job.get("designVersion", 0))
    if version is not None and int(version) != current:
        error = StaleDesignVersion(
            f"The design is at version {current}, not {version}; export the current version."
        )
        error.details = {"currentVersion": current, "expectedVersion": int(version)}
        raise error

    snapshot = design_state.load(job_id, pointer["key"])
    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    scene = design_state.scene_of(snapshot)

    # URLs come from what was already published, matched by content hash: an
    # export never publishes new image bytes, and never pairs a URL with a
    # different image than the snapshot references.
    published = {
        e["assetId"]: e
        for e in (job["artifacts"].get(Stage.ASSEMBLE) or {}).get("assets", [])
    }
    urls = {}
    for entry in snapshot["assets"]["entries"]:
        match = published.get(entry["assetId"])
        if not match or match["sha256"] != entry["sha256"] or not match.get("url"):
            raise AssetInvalid(
                f"Asset {entry['assetId']!r} has no published copy matching the design."
            )
        urls[entry["assetId"]] = match["url"]

    html = assemble_html.assemble(scene, urls, canvas)
    fitted_scene, fitted = assemble_stage._fit_text(scene, html, canvas)
    if fitted:
        html = assemble_html.assemble(fitted_scene, urls, canvas)

    name = f"export-v{snapshot['version']}"
    html_key = artifacts.artifact_key(job_id, Stage.DESIGN_STATE, snapshot["version"], f"{name}.html")
    png_key = artifacts.artifact_key(job_id, Stage.DESIGN_STATE, snapshot["version"], f"{name}.png")
    rendered = renderer.render(html, canvas, fitted_scene)
    html_bytes = html.encode("utf-8")
    for key, data, kind in ((html_key, html_bytes, "text/html; charset=utf-8"),
                            (png_key, rendered["native"], "image/png")):
        try:
            artifacts.put_bytes(key, data, kind)
        except AssetInvalid:
            pass  # an earlier export of this version: identical inputs, same name
    preview = media.publish(rendered["native"], job_id=job_id, asset_id=name)
    return {
        "jobId": job_id,
        "designVersion": snapshot["version"],
        "html": html,
        "htmlHash": artifacts.sha256_hex(html_bytes),
        "htmlKey": html_key,
        "previewKey": png_key,
        "previewUrl": preview["url"],
        "problems": rendered["checks"]["problems"],
        "fitted": fitted,
        "imageGenerationCalls": 0,
    }
