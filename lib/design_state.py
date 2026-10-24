"""The persistent state of one design candidate, in four independent parts.

  content     the editable words: business name, headline, CTA, contact details
  layout      positions, sizes, typography, colours, effects and stacking order
              of every layer (the scene document, without any copy in it)
  assets      the photograph and complex artwork, as artifact keys and sha256
  validation  the checks that were run, each bound to the exact hashes it examined

Each part has its own version, and a version only moves when that part's hash
moves. That is what makes "a text edit preserves every asset reference and hash"
checkable rather than a hope: the snapshot a text edit produces carries the same
`assets.hash` and `assets.version` as the one it came from.

A snapshot is immutable. Every candidate gets its own, stored append-only; a
repair writes a new one next to the old one and never rewrites it, so the last
valid assets and the last valid candidate are always still there.

Everything here that builds or compares snapshots is pure. Reading and writing
S3 and DynamoDB is confined to `store`, `load` and `commit`.
"""

from __future__ import annotations

import copy
import json

from lib import artifacts, jobs, scene_patch
from lib.dependencies import digest
from lib.errors import AssetInvalid, Stage

SCHEMA_VERSION = "1.0"


def asset_hashes(entries):
    """Mapping of asset id to sha256 for a manifest."""
    return {entry["assetId"]: entry["sha256"] for entry in entries}


def content_items(scene):
    """The approved wording the scene currently carries, ordered by content key."""
    return sorted(
        (
            {"contentKey": l["contentKey"], "role": l["role"], "text": l["text"]}
            for l in scene["textLayers"]
        ),
        key=lambda item: item["contentKey"],
    )


def build_snapshot(job_id, *, version, parent, scene, assets, validation, reason,
                   attempt, html=None):
    """Assemble an immutable snapshot of one candidate.

    `parent` is the snapshot this one derives from (or None). A part's version is
    inherited when its hash is unchanged and incremented when it is not.
    """
    content = {"items": content_items(scene)}
    layout = {"document": scene_patch.strip_text(scene)}
    asset_part = {"entries": sorted(
        ({"assetId": a["assetId"], "key": a["key"], "sha256": a["sha256"]} for a in assets),
        key=lambda a: a["assetId"],
    )}
    for part in (content, layout, asset_part):
        part["hash"] = digest({k: v for k, v in part.items() if k != "hash"})

    for name, part in (("content", content), ("layout", layout), ("assets", asset_part)):
        before = (parent or {}).get(name)
        if before is None:
            part["version"] = 1
        elif before["hash"] == part["hash"]:
            part["version"] = before["version"]
        else:
            part["version"] = before["version"] + 1

    return {
        "schema": SCHEMA_VERSION,
        "jobId": job_id,
        "version": int(version),
        "parentVersion": parent["version"] if parent else None,
        "attempt": int(attempt),
        "reason": reason,
        "content": content,
        "layout": layout,
        "assets": asset_part,
        "validation": validation or {},
        "html": html,
    }


def scene_of(snapshot):
    """The full scene document this snapshot describes."""
    return scene_patch.compose_scene(
        snapshot["layout"]["document"], snapshot["content"]["items"]
    )


def parts_changed(before, after):
    """Names of the parts whose hash differs between two snapshots."""
    return [
        name for name in ("content", "layout", "assets")
        if before[name]["hash"] != after[name]["hash"]
    ]


def require_assets_preserved(before, after):
    """Refuse a derived snapshot that changed any asset reference or hash."""
    if before["assets"]["hash"] != after["assets"]["hash"]:
        raise AssetInvalid(
            "A content or layout change altered the asset references; assets must "
            "be preserved by a text/layout edit."
        )


def derive(parent, *, scene, reason, attempt, version=None, html=None, validation=None):
    """A new snapshot from `parent` with a changed scene and the SAME assets.

    The assets are carried over untouched. Validation is carried over only for the
    entries still bound to what they examined (`validation_current`), so a stale
    result is dropped rather than inherited.
    """
    derived = build_snapshot(
        parent["jobId"],
        version=version if version is not None else parent["version"] + 1,
        parent=parent,
        scene=scene,
        assets=parent["assets"]["entries"],
        validation=validation if validation is not None else carried_validation(parent, scene),
        reason=reason,
        attempt=attempt,
        html=html,
    )
    require_assets_preserved(parent, derived)
    return derived


def compositing_hash(scene):
    """What the compositing checks depend on besides the asset bytes."""
    return digest(
        {
            "canvas": scene["canvas"],
            "imageLayers": scene["imageLayers"],
            "overlaySamples": scene.get("overlaySamples", []),
        }
    )


def carried_validation(parent, scene):
    """The parent's validation entries that still describe this scene."""
    carried = {}
    for name, entry in (parent.get("validation") or {}).items():
        bound = entry.get("boundTo", {})
        if bound.get("compositing") and bound["compositing"] != compositing_hash(scene):
            continue
        carried[name] = copy.deepcopy(entry)
    return carried


def validation_current(snapshot, name, *, assets, scene):
    """True only if the stored validation `name` was run on exactly these inputs.

    Compared by hash against the asset bytes and, where the check depends on it,
    the compositing inputs of the scene. A missing entry, a different asset or a
    different validation policy version all read as "not current".
    """
    from lib.dependencies import VALIDATION_POLICY_VERSION

    entry = (snapshot.get("validation") or {}).get(name)
    if not entry:
        return False
    if entry.get("policyVersion") != VALIDATION_POLICY_VERSION:
        return False
    bound = entry.get("boundTo", {})
    for asset_id, sha in bound.get("assets", {}).items():
        if assets.get(asset_id) != sha:
            return False
    if bound.get("compositing") and bound["compositing"] != compositing_hash(scene):
        return False
    return True


# --- persistence -----------------------------------------------------------


def snapshot_key(job_id, version):
    return artifacts.artifact_key(job_id, Stage.DESIGN_STATE, int(version), "snapshot.json")


def store(job_id, snapshot, name=None):
    """Write a snapshot append-only and return its key.

    A snapshot normally lives at its version's own key. A snapshot that is derived
    outside the pipeline (a user edit, a delivery copy) passes a `name` that is
    unique to its request, so a retried request finds its own earlier write, while
    a different request can never collide with it. An existing object is accepted
    only when it holds exactly the same content.
    """
    if name is None:
        stored = artifacts.put_json(snapshot_key(job_id, snapshot["version"]), snapshot)
        return stored["key"]
    key = artifacts.artifact_key(job_id, Stage.DESIGN_STATE, int(snapshot["version"]), name)
    try:
        artifacts.put_json(key, snapshot)
    except AssetInvalid:
        existing = artifacts.get_json(job_id, key)
        if existing != json.loads(json.dumps(snapshot, default=artifacts.json_default)):
            raise
    return key


def deliver(job_id, job, selected):
    """Make the DELIVERED candidate the job's current design.

    The candidate that finishes the job is not necessarily the one assembled last,
    but edits are made against the delivered design. When they differ, a new
    version is derived from the selected snapshot (same words, layout and assets),
    so version numbers keep increasing and an editor's expected version stays
    meaningful. Returns the current pointer.
    """
    pointer = current_pointer(job)
    chosen_key = selected.get("snapshotKey")
    if not chosen_key:
        return pointer
    if pointer and pointer["key"] == chosen_key:
        return pointer
    chosen = load(job_id, chosen_key)
    version = int(job.get("designVersion", 0) or 0) + 1
    delivered = derive(
        chosen, scene=scene_of(chosen), reason="delivered", attempt=chosen["attempt"],
        version=version,
    )
    delivered["delivered"] = {"fromVersion": chosen["version"]}
    key = store(job_id, delivered, name="delivered.json")
    commit(job_id, delivered, key)
    return {"version": version, "key": key}


def load(job_id, key):
    return artifacts.get_json(job_id, key)


def current_pointer(job):
    """The job's current snapshot pointer, or None before the first assembly."""
    pointer = job.get("designState")
    return pointer if pointer and pointer.get("key") else None


def commit(job_id, snapshot, key, *, expected_version=None):
    """Make `snapshot` the job's current design.

    With `expected_version` the update is conditional on the job still being at
    that version, so a concurrent edit is detected instead of overwritten. Returns
    True on success and False when another writer got there first.
    """
    pointer = {
        "version": snapshot["version"],
        "key": key,
        "contentVersion": snapshot["content"]["version"],
        "layoutVersion": snapshot["layout"]["version"],
        "assetsVersion": snapshot["assets"]["version"],
        "assetsHash": snapshot["assets"]["hash"],
    }
    return jobs.set_design_state(job_id, pointer, expected_version=expected_version)
