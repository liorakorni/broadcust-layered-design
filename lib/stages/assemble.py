"""ASSEMBLE and RENDER.

ASSEMBLE publishes the validated assets to stable URLs and builds the HTML. The
assets must be published first because the HTML references them by URL, and an
HTML document pointing at S3 keys nobody can read is not a deliverable.

RENDER proves the document actually works, and persists both captures so REVIEW
inspects the real rendered pixels rather than a description of them.
"""

from __future__ import annotations

import copy

from lib import (
    alignment, artifacts, assemble_html, geometry, jobs, layer_plan, media, milestones,
    render as renderer,
    typography_check,
)
from lib.errors import RenderFailed, Stage


def assemble(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.ASSEMBLE)

    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    scene = artifacts.get_json(job_id, scene_key_for(job, attempt))

    published = _publish_assets(job_id, job, attempt)
    # The concept's alignment relationships become positions here, in code, so the
    # same scene always assembles to the same document and a box that was sized a
    # few pixels differently from its neighbours does not move the shared edge.
    scene, aligned = alignment.apply(scene)
    scene, _plan_moved = layer_plan.sync(scene)       # the plan stays true to the scene
    html = assemble_html.assemble(scene, published["urls"], canvas)

    requested = {l["id"]: l["fontSize"] for l in scene["textLayers"]}
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
    effective = _effective_typography(scene, requested)
    # The job record keeps only the latest attempt's pointers, so this attempt's
    # asked-for and set sizes are also kept as their own artifact: a size that fitting
    # reduced in attempt 1 must stay visible after attempt 2 has assembled.
    typography_key = artifacts.artifact_key(job_id, Stage.ASSEMBLE, attempt, "typography.json")
    artifacts.put_json(typography_key, {"typography": effective, "fitted": fitted,
                                        "aligned": aligned})
    record = {
        "html": stored["key"],
        "htmlHash": stored["sha256"],
        "scene": stored_scene["key"],
        "typographyKey": typography_key,
        "fitted": fitted,
        "aligned": aligned,
        # What each layer will actually render with, after fitting: the size that
        # was asked for beside the size that was set, so a shrink is never silent.
        "typography": effective,
        # FINALIZE hashes this manifest, so it is persisted rather than only
        # returned through workflow state, which carries pointers alone.
        "assets": published["manifest"],
    }
    # The immutable snapshot of this candidate: its words, layout and assets as
    # versioned parts, and the validation each asset check was bound to. A repair
    # derives its next candidate from this, never by rewriting it.
    snapshot = _snapshot(job_id, job, attempt, scene, published["manifest"], stored)
    record["snapshot"] = snapshot
    record["assetsReused"] = published["reused"]
    jobs.record_artifacts(job_id, Stage.ASSEMBLE, attempt, record)
    return record


def scene_key_for(job, attempt):
    """The scene this attempt assembles.

    The first pass builds from the scene the pipeline produced. After a text or
    layout repair it builds from the patched scene that repair wrote for exactly
    this attempt - never from a stale one, and never by re-extracting.
    """
    working = job.get("workingScene") or {}
    if working.get("key") and int(working.get("forAttempt", 0)) == int(attempt):
        return working["key"]
    produced = job["artifacts"]
    if job.get("flow") == "layout-first":
        return produced[Stage.PLAN_LAYOUT]["scene"]
    return produced[Stage.EXTRACT_SCENE]["scene"]


def _snapshot(job_id, job, attempt, scene, manifest, html_stored):
    from lib import design_state

    pointer = design_state.current_pointer(job)
    parent = design_state.load(job_id, pointer["key"]) if pointer else None
    version = int(job.get("designVersion", 0) or 0) + 1
    validation = (job["artifacts"].get(Stage.VALIDATE_ASSETS) or {}).get("entries") or {}
    snapshot = design_state.build_snapshot(
        job_id,
        version=version,
        parent=parent,
        scene=scene,
        assets=manifest,
        validation=validation,
        reason="assembled",
        attempt=attempt,
        html={"key": html_stored["key"], "sha256": html_stored["sha256"]},
    )
    key = design_state.store(job_id, snapshot)
    design_state.commit(job_id, snapshot, key)
    return {"version": version, "key": key}


def _effective_typography(scene, requested):
    return {
        layer["id"]: {
            "requestedFontSize": requested.get(layer["id"], layer["fontSize"]),
            "fontSize": layer["fontSize"],
            "fontFamily": layer["fontFamily"], "fontWeight": layer.get("fontWeight", 400),
            "letterSpacing": layer.get("letterSpacing", 0.0),
            "lineHeight": layer.get("lineHeight", 1.2),
            "fill": layer.get("fill", "solid"), "strokeWidth": layer.get("strokeWidth", 0.0),
            "box": [layer["width"], layer["height"]],
        }
        for layer in scene["textLayers"]
    }


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
        # A layer that asked to keep its size ("visible") is never shrunk: its
        # overflow is reported by RENDER and routed as a typography or layout
        # correction rather than silently absorbed here.
        if layer.get("overflow", "shrink") == "visible":
            continue
        change = measured.get(element_ids.get(layer["id"]))
        if not change:
            continue
        layer["fontSize"] = change["fitted"]
        changes[layer["id"]] = change

    return adjusted, changes


def _publish_assets(job_id, job, attempt):
    """Upload every delivered asset to stable media and collect its URL.

    An asset whose bytes are unchanged since an earlier assemble of this job keeps
    the URL it was already published under: re-uploading identical bytes is slow,
    changes nothing, and would make a text-only repair look like it touched the
    assets. Reuse is decided by content hash, never by name.
    """
    entries = []
    urls = {}
    reused = []
    previous = {
        e["assetId"]: e
        for e in (job.get("artifacts", {}).get(Stage.ASSEMBLE, {}) or {}).get("assets", [])
    }

    for asset_id, key in _asset_keys(job).items():
        data = artifacts.get_bytes(job_id, key)
        sha = artifacts.sha256_hex(data)
        before = previous.get(asset_id)
        if before and before.get("sha256") == sha and before.get("url"):
            url = before["url"]
            reused.append(asset_id)
            jobs.add_metric(job_id, "assetUrlsReused")
        else:
            url = media.publish(data, job_id=job_id, asset_id=asset_id)["url"]
        urls[asset_id] = url
        entries.append({"assetId": asset_id, "key": key, "sha256": sha, "url": url})
    return {"urls": urls, "manifest": entries, "reused": reused}


def _asset_keys(job):
    """Map each asset reference to the artifact key holding its bytes."""
    produced = job.get("artifacts", {})
    if job.get("flow") == "layout-first":
        # No overlay: simple elements are code-owned shapes in the layout, so the
        # only generated asset is the text-free photograph.
        keys = {"background": produced[Stage.GENERATE_PHOTO]["background"]}
    else:
        keys = {"background": produced[Stage.CLEAN_PHOTO]["background"]}
        template = produced[Stage.DECORATION_OVERLAY]
        # A layered-v2 design whose base is CSS (a gradient-only base, or a template that
        # was replaced by the fallback) delivers no template raster at all.
        if not template.get("omitted"):
            keys["decoration"] = template["decoration"]
    for entry in jobs.asset_items(job):
        keys[entry["assetId"]] = entry["key"]
    return keys


def _concept_bytes(job_id, job):
    """The approved concept, or None for a flow that has none to compare against."""
    approved = job.get("approvedConcept") or {}
    return artifacts.get_bytes(job_id, approved["key"]) if approved.get("key") else None


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
    # The browser's own measurement of where the copy landed: shared edges that
    # drifted, lines that overlap, copy outside the canvas.
    measured = geometry.check_render(scene, checks.get("rects"), canvas)
    measured += typography_check.check(
        scene, _concept_bytes(job_id, job), result["native"], canvas,
        platform_fonts=checks.get("platformFonts"),
    )
    out = {
        "native": native["key"],
        "feed": feed["key"],
        "problems": list(checks["problems"]) + measured,
        "checksPerformed": checks.get("performed", []),
        # When THIS candidate was rendered, so the delivered one can report its own time.
        "renderedAt": jobs._now(),
    }
    jobs.record_artifacts(job_id, Stage.RENDER, attempt, out)
    # The first HTML that exists and has been rendered is a durable milestone (a draft).
    milestones.record_first_html(job_id, attempt, rendered_at=out["renderedAt"])
    return out
