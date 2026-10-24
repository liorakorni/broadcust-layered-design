"""Interactive text and layout editing of a finished design (backend contract).

An editor changes words, typography or layout, many times, quickly. None of that
may cost a model call or a browser launch:

  * an UPDATE patches the stored design and writes a new immutable snapshot. It
    makes no image-generation call, renders nothing, and carries every asset
    reference and hash over unchanged (verified, not assumed);
  * a server-rendered preview exists only for an explicit save/export
    (`lib/stages/export_design.py`), never per keystroke.

Concurrency is optimistic. Every update names the design version it was made
against (`expectedVersion`). If the design has moved on, the update is refused with
STALE_DESIGN_VERSION and the current version, rather than overwriting someone
else's edit; the final write is a conditional update on that same version, so two
simultaneous edits cannot both win.

Idempotency is by `requestId`. Repeating an update with the SAME payload returns
the original result without writing again; reusing a requestId with a DIFFERENT
payload is an error, so every changed payload must be sent with a new requestId.
A retry is recognised until the next edit lands (the design records the request
that produced it).
"""

from __future__ import annotations

import conf
from lib import artifacts, dependencies, design_state, jobs, scene_patch, schemas
from lib.errors import (
    DesignNotEditable, IdempotencyConflict, InvalidEdit, StaleDesignVersion, Status,
)

CONTENT_MAX_CHARS = 1000
CATEGORIES = ("content", "typography", "layout")


def parse_request(payload):
    """Validate the shape of an update; returns `(jobId, requestId, expected, changes)`."""
    if not isinstance(payload, dict):
        raise InvalidEdit("The update must be a JSON object.")
    job_id = payload.get("jobId")
    request_id = payload.get("requestId")
    expected = payload.get("expectedVersion")
    changes = payload.get("changes")
    if not isinstance(job_id, str) or not job_id:
        raise InvalidEdit("jobId is required.")
    if not isinstance(request_id, str) or not schemas.ASSET_ID.match(request_id):
        raise InvalidEdit("requestId must be alphanumeric with . _ - (at most 64 characters).")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise InvalidEdit("expectedVersion must be the integer version the edit was made against.")
    if not isinstance(changes, dict) or not changes:
        raise InvalidEdit("changes must name at least one of content, typography, layout.")
    unknown = sorted(set(changes) - set(CATEGORIES))
    if unknown:
        raise InvalidEdit(f"Unknown change categories: {', '.join(unknown)}.")
    content = changes.get("content") or {}
    if not isinstance(content, dict):
        raise InvalidEdit("changes.content maps a contentKey to its new text.")
    for key in ("typography", "layout"):
        value = changes.get(key) or {}
        if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
            raise InvalidEdit(f"changes.{key} maps a layer id to {{property: value}}.")
    return job_id, request_id, expected, changes


def request_hash(job_id, expected, changes):
    """Identifies the payload (not the requestId) for idempotency."""
    return dependencies.digest({"jobId": job_id, "expectedVersion": expected, "changes": changes})


def apply_changes(scene, changes):
    """Return the scene with the changes applied, or raise InvalidEdit."""
    patched = scene
    layers = {l["contentKey"]: l for l in scene["textLayers"]}
    patched = _copy(patched)
    for content_key, text in (changes.get("content") or {}).items():
        if content_key not in layers:
            raise InvalidEdit(f"There is no text with content key {content_key!r}.")
        if not isinstance(text, str) or not text.strip():
            raise InvalidEdit(f"The text for {content_key!r} must not be empty.")
        if len(text) > CONTENT_MAX_CHARS or any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise InvalidEdit(f"The text for {content_key!r} is too long or has control characters.")
        for layer in patched["textLayers"]:
            if layer["contentKey"] == content_key:
                layer["text"] = text
    for target in ("typography", "layout"):
        for layer_id, properties in (changes.get(target) or {}).items():
            try:
                patched = scene_patch.apply_properties(
                    patched, layer_id, properties, target=target,
                    font_names=conf.APPROVED_FONT_FAMILIES,
                )
            except Exception as exc:  # InvalidScene from the patcher
                raise InvalidEdit(str(exc)) from exc
    try:
        validated = schemas.parse_scene(
            patched, scene["canvas"]["width"], scene["canvas"]["height"],
            allowed_fonts=set(conf.APPROVED_FONT_FAMILIES),
        )
    except Exception as exc:
        raise InvalidEdit(f"The edit produces an invalid design: {exc}") from exc
    return validated.model_dump(mode="json")


def _copy(scene):
    import copy

    return copy.deepcopy(scene)


def update(payload, owner_scope):
    """Apply one edit. Makes no image call and renders nothing."""
    job_id, request_id, expected, changes = parse_request(payload)
    job = jobs.get_job(job_id, owner_scope)
    if job.get("status") != Status.SUCCEEDED:
        raise DesignNotEditable(
            "Only a finished design can be edited; this job is "
            f"{str(job.get('status')).lower()}."
        )
    pointer = design_state.current_pointer(job)
    if not pointer:
        raise DesignNotEditable("This job has no stored design to edit.")
    current = int(job.get("designVersion", 0))
    digest_ = request_hash(job_id, expected, changes)
    base = design_state.load(job_id, pointer["key"])

    # An identical retry of the edit that produced the current design.
    previous = base.get("edit") or {}
    if previous.get("requestId") == request_id:
        if previous.get("requestHash") != digest_:
            raise IdempotencyConflict(
                f"requestId {request_id!r} was already used with a different update; "
                "send changed edits with a new requestId."
            )
        return _result(base, pointer["key"], changed=True, idempotent=True, previous=None)

    if expected != current:
        error = StaleDesignVersion(
            f"The design is at version {current}, but this edit was made against "
            f"version {expected}. Reload the design and apply the edit again."
        )
        error.details = {"currentVersion": current, "expectedVersion": expected}
        raise error

    scene = design_state.scene_of(base)
    patched = apply_changes(scene, changes)
    if patched == scene:
        return _result(base, pointer["key"], changed=False, idempotent=False, previous=base)

    derived = design_state.derive(
        base, scene=patched, reason="user-edit", attempt=base["attempt"], version=current + 1,
    )
    derived["edit"] = {"requestId": request_id, "requestHash": digest_,
                       "baseVersion": current, "categories": sorted(changes)}
    key = design_state.store(job_id, derived, name=f"edit-{digest_[:16]}.json")
    if not design_state.commit(job_id, derived, key, expected_version=current):
        error = StaleDesignVersion(
            "Another edit was saved while this one was being applied. Reload the "
            "design and apply the edit again."
        )
        error.details = {"expectedVersion": expected}
        raise error
    return _result(derived, key, changed=True, idempotent=False, previous=base, scene_before=scene)


def _result(snapshot, key, *, changed, idempotent, previous, scene_before=None):
    parts = {name: {"version": snapshot[name]["version"], "hash": snapshot[name]["hash"]}
             for name in ("content", "layout", "assets")}
    out = {
        "success": True,
        "jobId": snapshot["jobId"],
        "designVersion": snapshot["version"],
        "changed": changed,
        "idempotent": idempotent,
        "parts": parts,
        "assets": {e["assetId"]: e["sha256"] for e in snapshot["assets"]["entries"]},
        "snapshotKey": key,
        # The contract: an edit never generates an image and never renders.
        "imageGenerationCalls": 0,
        "previewRendered": False,
    }
    if previous is not None and scene_before is not None:
        kinds = dependencies.classify_changes(scene_before, design_state.scene_of(snapshot))
        out["changeKinds"] = sorted(kinds)
        out["invalidatedChecks"] = sorted(dependencies.invalidated_checks(kinds))
        out["changedParts"] = design_state.parts_changed(previous, snapshot)
    return out
