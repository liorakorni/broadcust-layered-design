"""REPAIR_LIGHT and REPAIR_ASSET: repair a design without redoing the pipeline.

The lightweight path covers findings routed to content, typography or layout:

    selected candidate -> patch only the affected fields -> assemble -> render ->
    relevant checks and the final composition review

It never calls CONCEPT, EXTRACT_SCENE, CLEAN_PHOTO or DECORATION_OVERLAY, and
makes no image-generation call at all. The assets are not touched: the snapshot a
repair derives carries the same asset references and hashes as the one it came
from, and that is verified before anything is written.

Wording is never taken from a correction. Content repairs restore the approved
copy exactly; typography and layout repairs change only the properties a validated
constraint names, on only the elements it names.

A repair is recorded before its budget is spent and keyed by the attempt, so a
retried task finds its own record instead of repairing twice.
"""

from __future__ import annotations

import re

import conf
from lib import (
    artifacts, content_check, dependencies, design_state, jobs, policy_context,
    repair_routing, scene_patch, schemas,
)
from lib.errors import (
    AssetInvalid, CorrectionExhausted, MandatoryContentMissing, RepairRouteUnresolved, Stage,
)

#: Approved-copy roles to scene text roles. The copy manifest uses its own
#: vocabulary; a restored layer must carry a role the HTML contract allows.
_ROLE_FOR_COPY = {
    "business": "businessName",
    "title": "title",
    "subtitle": "subtitle",
    "body": "serviceItem",
    "cta": "cta",
    "contact": "userInformation",
}

_HEBREW = re.compile(r"[\u0590-\u05FF]")


def light(job_id, attempt, asset_id=None):
    """Apply the validated text/layout patch for the latest review.

    Returns `{route, nextAttempt, repair, ...}`; the workflow assembles the
    patched design at `nextAttempt`.
    """
    jobs.set_stage(job_id, Stage.REVIEW)
    job = jobs.get_job_unscoped(job_id)
    attempt = int(attempt)

    record_key = artifacts.artifact_key(job_id, Stage.REPAIR, attempt, "repair.json")
    existing = _existing(job_id, record_key)
    if existing:
        return existing["result"]

    review = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW]["review"])
    routing = review.get("routing") or {}
    if routing.get("route") != repair_routing.ROUTE_LIGHT:
        raise RepairRouteUnresolved(
            f"REPAIR_LIGHT was reached for a review routed {routing.get('route')!r}."
        )

    pointer = design_state.current_pointer(job)
    if not pointer:
        raise AssetInvalid("There is no design snapshot to repair.")
    base = design_state.load(job_id, pointer["key"])
    scene = design_state.scene_of(base)
    copy_items = artifacts.get_json(job_id, job["artifacts"][Stage.PREPARE_COPY]["copy"])["items"]
    canvas = {"width": int(job["canvasWidth"]), "height": int(job["canvasHeight"])}
    before_issues = content_check.check(scene, copy_items)

    usable = [r for r in routing["issues"] if r["route"] == repair_routing.ROUTE_LIGHT
              and r["target"] in routing["categories"]]
    patched, applied = apply_patches(job_id, job, scene, copy_items, usable, canvas)

    # A correction must not cost the design any mandatory text it still had.
    after_issues = content_check.check(patched, copy_items)
    introduced = [i for i in after_issues if i["affected"] not in {b["affected"] for b in before_issues}]
    if introduced:
        raise MandatoryContentMissing(
            "The correction would have removed approved text ("
            + ", ".join(i["affected"] for i in introduced) + "); it was not applied."
        )

    kinds = dependencies.classify_changes(scene, patched)
    invalidated = sorted(dependencies.invalidated_checks(kinds))
    derived = design_state.derive(
        base, scene=patched, reason="light-repair:" + ",".join(routing["categories"]),
        attempt=attempt + 1,
    )
    design_state.require_assets_preserved(base, derived)

    scene_key = artifacts.artifact_key(job_id, Stage.REPAIR, attempt, "scene.json")
    stored_scene = artifacts.put_json(scene_key, patched)
    record = {
        "attempt": attempt,
        "nextAttempt": attempt + 1,
        "route": repair_routing.ROUTE_LIGHT,
        "categories": routing["categories"],
        "reason": routing["reason"],
        "applied": applied,
        "changeKinds": sorted(kinds),
        "invalidatedChecks": invalidated,
        "retainedChecks": sorted(set(dependencies.ALL_CHECKS) - set(invalidated)),
        "baseSnapshotVersion": base["version"],
        "assetsHash": base["assets"]["hash"],
        "assetsPreserved": base["assets"]["hash"] == derived["assets"]["hash"],
        "patchDigest": routing.get("patchDigest"),
        "imageGenerationCalls": 0,
        "stagesSkipped": [
            Stage.CONCEPT, Stage.EXTRACT_SCENE, Stage.CLEAN_PHOTO, Stage.DECORATION_OVERLAY,
        ],
        "scene": stored_scene["key"],
    }
    result = {
        "route": repair_routing.ROUTE_LIGHT,
        "nextAttempt": attempt + 1,
        "repair": record_key,
        "categories": routing["categories"],
        "changeKinds": sorted(kinds),
        "imageGenerationCalls": 0,
    }
    # Persisted before the budget is spent so a retried task returns this record.
    artifacts.put_json(record_key, {**record, "result": result})

    for category in routing["categories"]:
        if jobs.bump_repair(job_id, category, conf.MAX_REPAIRS[category]) is None:
            raise CorrectionExhausted(
                f"The {category} repair budget is spent; the correction was not applied."
            )
    jobs.set_working_scene(
        job_id,
        {"key": stored_scene["key"], "forAttempt": attempt + 1, "baseVersion": base["version"]},
    )
    jobs.add_metric(job_id, "lightRepairs")
    return result


def asset(job_id, attempt, asset_id=None):
    """Plan the regeneration of exactly one asset the review named.

    This stage makes no image call itself: it validates the routed target, spends
    the asset budget, records the instruction and says which single regeneration
    step comes next - "photo" (the photograph alone) or "overlay" (the overlay,
    then the photograph, because the photograph is edited around the overlay).
    Nothing else is regenerated, and every other asset keeps its stored hash.

    An independent asset is deliberately not repairable here: it is generated from
    its own brief, and re-running that is not a targeted correction of a review
    finding. It stops with a specific diagnostic instead of regenerating guesses.
    """
    jobs.set_stage(job_id, Stage.REVIEW)
    job = jobs.get_job_unscoped(job_id)
    attempt = int(attempt)

    record_key = artifacts.artifact_key(job_id, Stage.REPAIR, attempt, "repair-asset.json")
    existing = _existing(job_id, record_key)
    if existing:
        return existing["result"]

    review = artifacts.get_json(job_id, job["artifacts"][Stage.REVIEW]["review"])
    routing = review.get("routing") or {}
    if routing.get("route") != repair_routing.ROUTE_ASSET:
        raise RepairRouteUnresolved(
            f"REPAIR_ASSET was reached for a review routed {routing.get('route')!r}."
        )
    routed = [r for r in routing["issues"] if r["route"] == repair_routing.ROUTE_ASSET]
    target = routed[0]["assetTarget"]
    layout_first = job.get("flow") == "layout-first"
    supported = {"background"} if layout_first else {"background", "decoration"}
    if target not in supported:
        raise RepairRouteUnresolved(
            f"The review points at asset '{target}', which cannot be repaired in place. "
            "Only the photograph"
            + ("" if layout_first else " and the decoration overlay")
            + " have a targeted repair; the preserved candidate stays available."
        )
    instruction = " ".join(
        (r.get("patch") or {}).get("instruction", "") for r in routed if r["assetTarget"] == target
    ).strip()[:1200]
    if not instruction:
        raise RepairRouteUnresolved(
            f"The review named asset '{target}' without saying what is wrong with it, "
            "so a targeted regeneration has nothing to act on."
        )

    step = "photo" if target == "background" else "overlay"
    result = {
        "route": repair_routing.ROUTE_ASSET,
        "assetTarget": target,
        "next": step,
        "nextAttempt": attempt + 1,
        "repair": record_key,
        "imageGenerationCalls": 0,
    }
    artifacts.put_json(record_key, {"attempt": attempt, "instruction": instruction,
                                    "patchDigest": routing.get("patchDigest"), "result": result})

    if jobs.bump_repair(job_id, "asset", conf.MAX_REPAIRS["asset"]) is None:
        raise CorrectionExhausted("The asset repair budget is spent; nothing was regenerated.")
    if step == "photo":
        jobs.set_photo_repair(job_id, {"instruction": instruction, "forAttempt": attempt + 1})
    else:
        # The overlay repair has its own bound, shared with validation's repairs.
        if jobs.bump_counter(job_id, "overlayRepairs", conf.MAX_OVERLAY_REPAIRS) is None:
            raise CorrectionExhausted("The overlay repair budget is spent; nothing was regenerated.")
        jobs.record_overlay_defect(job_id, instruction)
    return result


def _existing(job_id, key):
    """The record a previous (retried) invocation wrote, or None."""
    try:
        return artifacts.get_json(job_id, key)
    except AssetInvalid:
        return None


def apply_patches(job_id, job, scene, copy_items, routed, canvas):
    """Apply every routed light patch to `scene`, returning `(scene, applied)`.

    Content first (it can add layers the property patches then refer to), then
    properties. Each step is validated; nothing is applied that does not parse.
    """
    applied = []
    patched = scene

    content_keys = sorted(
        {k for r in routed if r["target"] == "content" for k in r["patch"]["contentKeys"]}
    )
    if content_keys:
        policy, analysis, _, placements = policy_context.load(job_id, job)
        patched, changed = scene_patch.apply_content(patched, copy_items)
        by_key = {item["contentKey"]: item for item in copy_items}
        present = {l["contentKey"] for l in patched["textLayers"]}
        restored = []
        for key in content_keys:
            if key not in present and key in by_key:
                patched = restore_text_layer(patched, by_key[key], placements.get(key), canvas)
                restored.append(key)
        applied.append({"target": "content", "restored": restored, "reworded": changed})

    for r in routed:
        for addition in (r["patch"] or {}).get("addShapes", []):
            patched = scene_patch.add_shape(patched, addition["preset"], effect_id=addition["effect"])
            applied.append({"target": "layout", "addedShape": addition["effect"], "preset": addition["preset"]})
    for r in routed:
        if r["target"] in ("typography", "layout") and r["patch"] and "layers" in r["patch"]:
            for layer_id, properties in r["patch"]["layers"].items():
                patched = scene_patch.apply_properties(
                    patched, layer_id, properties, target=r["target"],
                    font_names=conf.APPROVED_FONT_FAMILIES,
                )
                applied.append(
                    {"target": r["target"], "element": layer_id, "properties": properties}
                )
        elif r["patch"] and r["patch"].get("rerender"):
            applied.append({"target": r["target"], "rerender": True})

    allowed = {item["contentKey"] for item in copy_items}
    validated = schemas.parse_scene(
        patched, canvas["width"], canvas["height"],
        allowed_content_keys=allowed, allowed_fonts=set(conf.APPROVED_FONT_FAMILIES),
    )
    return validated.model_dump(mode="json"), applied


def restore_text_layer(scene, item, placement, canvas):
    """Add a text layer for an approved item that is missing from the design.

    Its position is the one planned at ANALYZE_REFERENCE (the same slot generation
    and review used, so it goes back where it was meant to be) and its typography
    is borrowed from the closest existing layer, so it matches the design instead
    of arriving in a default style. The wording is the approved text, verbatim.
    """
    import copy

    patched = copy.deepcopy(scene)
    role = _ROLE_FOR_COPY.get(item["role"], "informationItem")
    layers = patched["textLayers"]
    donor = next((l for l in layers if l["role"] == role), None) or (layers[0] if layers else None)

    if placement:
        x = int(placement["x"] * canvas["width"])
        y = int(placement["y"] * canvas["height"])
        width = max(40, int(placement["width"] * canvas["width"]))
        height = max(24, int(placement["height"] * canvas["height"]))
    elif donor:
        x, y, width, height = donor["x"], donor["y"], donor["width"], donor["height"]
    else:
        x, y, width, height = 40, 40, canvas["width"] // 2, 60
    width = min(width, canvas["width"] - x)
    height = min(height, canvas["height"] - y)

    hebrew = bool(_HEBREW.search(item["text"]))
    layer = {
        "id": re.sub(r"[^A-Za-z0-9._-]", "-", item["contentKey"]),
        "type": "text",
        "role": role,
        "contentKey": item["contentKey"],
        "text": item["text"],
        "x": x, "y": y, "width": width, "height": height,
        "zIndex": min(100, (max((l["zIndex"] for l in layers), default=9) + 1)),
        "fontFamily": (donor or {}).get("fontFamily") or conf.APPROVED_FONT_FAMILIES[0],
        "fontSize": max(8, min(int(height * 0.7), (donor or {}).get("fontSize", 40))),
        "fontWeight": (donor or {}).get("fontWeight", 700),
        "color": (donor or {}).get("color", "#FFFFFF"),
        "align": "right" if hebrew else "left",
        "direction": "rtl" if hebrew else "ltr",
        "lineHeight": (donor or {}).get("lineHeight", 1.2),
    }
    ids = {l["id"] for l in layers} | {l["id"] for l in patched["imageLayers"]}
    while layer["id"] in ids:
        layer["id"] += "-restored"
    patched["textLayers"].append(layer)
    return patched
