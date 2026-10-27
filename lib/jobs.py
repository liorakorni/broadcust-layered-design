"""Job metadata, ownership and idempotency in DynamoDB.

One table holds two record kinds under a single partition key:

  JOB#<jobId>                     job state
  IDEM#<ownerScope>#<requestId>   idempotency claim

Idempotency is scoped to the authenticated caller (SPEC section 2.1), so two
tenants can use the same requestId without colliding, and an owner scope can
never be asserted by the request body.

Creation is deliberately split from workflow start so a crash between the two
leaves a reconcilable record rather than an orphaned paid job (SPEC section 3).
"""

from __future__ import annotations

import datetime
import hashlib
import time
import uuid

import boto3
from botocore.exceptions import ClientError

import conf
from lib.errors import (
    AssetInvalid,
    IdempotencyConflict,
    JobNotFound,
    Stage,
    Status,
    Unauthorized,
)

#: Failed and in-flight jobs expire after this. A SUCCEEDED job is a saved design
#: and has its expiry removed by `mark_succeeded`.
JOB_TTL_DAYS = 30

#: Categories a repair can be routed to. Each is counted and bounded on its own.
REPAIR_CATEGORIES = ("content", "typography", "layout", "asset", "concept")

_table = None


def _tbl():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=conf.AWS_REGION).Table(
            conf.JOBS_TABLE
        )
    return _table


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _ttl():
    return int(time.time()) + JOB_TTL_DAYS * 86400


def execution_name(job_id):
    """Deterministic execution name.

    Derived from the job ID so a retried StartExecution for the same job is
    rejected by Step Functions as a duplicate rather than starting a second paid
    run. Step Functions allows 80 characters.
    """
    return f"ld-{job_id}"[:80]


def claim_idempotency(owner_scope, request_id, request_hash):
    """Claim (ownerScope, requestId), or return the existing job.

    Returns `(job_id, created)`. A conditional write is what makes this safe
    under concurrency: two simultaneous identical requests produce one job, not
    two paid runs.

    Raises IdempotencyConflict when the same requestId was used with different
    content, which is a caller bug worth surfacing loudly.
    """
    pk = f"IDEM#{owner_scope}#{request_id}"
    job_id = uuid.uuid4().hex
    try:
        _put(
            Item={
                "pk": pk,
                "jobId": job_id,
                "requestHash": request_hash,
                "createdAt": _now(),
                "expiresAt": _ttl(),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return job_id, True
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    existing = _tbl().get_item(Key={"pk": pk}).get("Item")
    if not existing:
        # The claim vanished between write and read (TTL expiry). Treat the
        # caller as new rather than failing them.
        return claim_idempotency(owner_scope, request_id, request_hash)
    if existing.get("requestHash") != request_hash:
        raise IdempotencyConflict(
            f"requestId {request_id!r} was already used with different content."
        )
    return existing["jobId"], False


def record_overlay_defect(job_id, description):
    """Store what the inspection wants removed from the decoration layer.

    Kept on the job rather than in S3 because it is transient state for the next
    repair, not an artifact of the design, and each repair supersedes the last.
    """
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET overlayDefect = :d, updatedAt = :now",
        ExpressionAttributeValues={":d": description or "", ":now": _now()},
    )


def set_photo_repair(job_id, plan):
    """Remember the targeted photograph correction the next stage must apply."""
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET photoRepair = :p, updatedAt = :now",
        ExpressionAttributeValues={":p": plan, ":now": _now()},
    )


def job_exists(job_id):
    """True only when `job_id` has a complete job record.

    The workflow updates jobs with UpdateItem, which creates a bare item when
    none exists. Such a stub has a status and a stage but no requestKey and no
    ownerScope, and is useless to both the pipeline and the caller. Requiring
    requestKey is what separates a real creation from that stub.
    """
    item = _tbl().get_item(Key={"pk": f"JOB#{job_id}"}).get("Item")
    return bool(item and item.get("requestKey"))


def create_job(job_id, owner_scope, request_hash, request_key, canvas,
               design_freedom=0, review_strictness=None, flow="legacy",
               layout_policy=None, reuse_concept_from=None, template_flexibility=None,
               pipeline_version=None):
    """Record the job as QUEUED before any execution starts.

    `layout_policy` is the direction policy resolved once at acceptance
    (lib/direction_policy.resolve_for_request). A job created without one - one
    accepted before it existed - keeps resolving its direction the way it always
    did; it is never reinterpreted.

    `pipeline_version` and `template_flexibility` are likewise resolved once, here, and
    stored: a job created without them (every job accepted before they existed) has no
    pipelineVersion attribute and is read as layered-v1, and no templateTolerance, so it
    is judged exactly as it always was. Nothing migrates it.
    """
    from lib import reference_policy, review_policy

    if review_strictness is None:
        review_strictness = review_policy.DEFAULT_STRICTNESS
    review_strictness = review_policy.validate_strictness(review_strictness)

    _put(
        Item={
            "pk": f"JOB#{job_id}",
            "jobId": job_id,
            "ownerScope": owner_scope,
            "status": Status.QUEUED,
            "stage": None,
            "requestHash": request_hash,
            "requestKey": request_key,
            "canvasWidth": canvas["width"],
            "canvasHeight": canvas["height"],
            # Both maps must exist up front: record_artifacts writes into them by
            # stage name, and DynamoDB rejects a nested write whose parent map is
            # absent rather than creating it.
            "artifacts": {},
            "attempts": {},
            "correctionRounds": 0,
            "overlayRepairs": 0,
            # Must be seeded: bump_counter's condition `conceptRounds < :limit`
            # is false when the attribute is absent, which would read as exhausted.
            "conceptRounds": 0,
            "designFreedom": design_freedom,
            "reviewStrictness": review_strictness,
            "policyVersion": reference_policy.POLICY_VERSION,
            # Every concept attempt and where its review stands, keyed by attempt.
            # Seeded for the same reason as the maps above: a nested write to a
            # missing parent is rejected.
            "conceptCandidates": {},
            # One entry per independent asset, written by the Map's own
            # invocations (see record_asset_item). Same reason for seeding it.
            "assets": {},
            # Non-blocking findings recorded by stages (see record_warning).
            "stageWarnings": {},
            # Design state (lib/design_state.py). `designVersion` is the optimistic
            # concurrency counter for edits; the maps below must exist up front for
            # the same reason as the ones above.
            "designVersion": 0,
            "designCandidates": {},
            "validationCache": {},
            "stageCache": {},
            "routes": {},
            "metrics": {},
            # Repair attempts are bounded per category, flat so the bound can be a
            # simple conditional update.
            **{f"repairs_{category}": 0 for category in REPAIR_CATEGORIES},
            "flow": flow,
            **({"layoutPolicy": layout_policy} if layout_policy else {}),
            **({"pipelineVersion": pipeline_version} if pipeline_version else {}),
            **({"templateTolerance": _tolerance(template_flexibility)}
               if template_flexibility is not None else {}),
            **(_budget_attributes() if pipeline_version else {}),
            **({"reuseConceptFrom": {"jobId": reuse_concept_from}} if reuse_concept_from else {}),
            "createdAt": _now(),
            "updatedAt": _now(),
            "expiresAt": _ttl(),
        }
    )


def _tolerance(flexibility):
    from lib import template_policy

    return template_policy.resolve(flexibility)


def _budget_attributes():
    from lib import repair_budget

    # `fallbacksUsed` is seeded for the same reason as the other maps: a nested write to
    # a missing parent is rejected.
    return {**repair_budget.initial_attributes(), "fallbacksUsed": {}}


def pipeline_version(job):
    """The execution path a job was accepted for. No attribute means layered-v1."""
    return (job or {}).get("pipelineVersion") or conf.LEGACY_PIPELINE_VERSION


def is_v2(job):
    return pipeline_version(job) != conf.LEGACY_PIPELINE_VERSION


def attach_execution(job_id, execution_arn):
    """Record the execution ARN, only if one is not already attached.

    The condition is the reconciliation guard: if a previous invocation already
    started an execution, this refuses to overwrite it, so a retry can never
    orphan a running job and start a second one.
    """
    try:
        _update(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET executionArn = :arn, #s = :running, updatedAt = :now",
            ConditionExpression="attribute_not_exists(executionArn)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":arn": execution_arn,
                ":running": Status.RUNNING,
                ":now": _now(),
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def plain(value):
    """The inverse of `ddb_safe`: Decimals read back as int or float.

    Arithmetic between a Decimal and a float raises, and DynamoDB returns every
    number as a Decimal, so a stage that ranks on a stored ratio would fail on the
    service and pass against any fake that returned what it was given.
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: plain(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [plain(inner) for inner in value]
    return value


def get_job(job_id, owner_scope):
    """Read a job, enforcing ownership.

    An unknown job and another caller's job are deliberately indistinguishable:
    both raise JobNotFound, so job IDs cannot be probed for existence.
    """
    item = _tbl().get_item(Key={"pk": f"JOB#{job_id}"}).get("Item")
    if not item:
        raise JobNotFound(f"Job {job_id} was not found.")
    if item.get("ownerScope") != owner_scope:
        raise JobNotFound(f"Job {job_id} was not found.")
    return plain(item)


def get_job_unscoped(job_id):
    """Read a job without an ownership check.

    Only for the stage Lambda, which runs inside the workflow for a job that was
    already authorized at acceptance. Never call this from a caller-facing path.
    """
    item = _tbl().get_item(Key={"pk": f"JOB#{job_id}"}).get("Item")
    if not item:
        raise JobNotFound(f"Job {job_id} was not found.")
    return plain(item)


def set_stage(job_id, stage):
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET stage = :s, #st = :running, updatedAt = :now",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":s": stage,
            ":running": Status.RUNNING,
            ":now": _now(),
        },
    )


def ddb_safe(value):
    """The same data with every float as a Decimal, which is all DynamoDB accepts.

    A measured ratio or a letter-spacing in a stage's record is a float, and boto3
    refuses it with a TypeError that surfaces as an INTERNAL failure of that stage.
    """
    from decimal import Decimal

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: ddb_safe(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [ddb_safe(inner) for inner in value]
    return value


def _update(**kwargs):
    """`update_item` with every float in the values turned into a Decimal."""
    if "ExpressionAttributeValues" in kwargs:
        kwargs["ExpressionAttributeValues"] = ddb_safe(kwargs["ExpressionAttributeValues"])
    return _tbl().update_item(**kwargs)


def _put(**kwargs):
    if "Item" in kwargs:
        kwargs["Item"] = ddb_safe(kwargs["Item"])
    return _tbl().put_item(**kwargs)


def record_artifacts(job_id, stage, attempt, artifacts):
    """Persist a stage's artifact pointers on the job record."""
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression=(
            "SET artifacts.#stage = :a, attempts.#stage = :attempt, updatedAt = :now"
        ),
        ExpressionAttributeNames={"#stage": stage},
        ExpressionAttributeValues={
            ":a": artifacts,
            ":attempt": attempt,
            ":now": _now(),
        },
    )


def record_asset_item(job_id, attempt, entry):
    """Add one independent asset to the job, without disturbing its siblings.

    The workflow produces these in a Map at concurrency two, so two invocations
    can be recording at the same moment. Writing the stage's whole pointer the
    way `record_artifacts` does would let the later write drop the earlier
    asset - which is how a contact icon reached ASSEMBLE with no delivered URL.
    Each asset is written under its own key in `assets` instead.
    """
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression=(
            "SET assets.#asset = :entry, attempts.#stage = :attempt, updatedAt = :now"
        ),
        ExpressionAttributeNames={
            "#asset": entry["assetId"], "#stage": Stage.INDEPENDENT_ASSETS},
        ExpressionAttributeValues={
            ":entry": storable(entry), ":attempt": attempt, ":now": _now(),
        },
    )
    return entry


def storable(value):
    """The same data with no Python floats in it.

    DynamoDB has no float type and rejects the whole write when it meets one, so
    a measurement a stage wants to keep (an alpha fraction, say) would fail the
    stage rather than be recorded. Numbers come back as `Decimal`, which
    lib/artifacts.py already serialises as plain numbers.
    """
    from decimal import Decimal

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: storable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [storable(v) for v in value]
    return value


def record_warning(job_id, warning):
    """Keep a non-blocking finding a stage made, for the final result to report.

    Stages other than the reviews can find something worth saying - a component
    the repair could not separate, say. Keyed by code and subject so a stage that
    runs again does not report the same thing twice.
    """
    key = f"{warning['code']}:{warning.get('affected') or ''}"
    try:
        _update(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET stageWarnings.#k = :w, updatedAt = :now",
            ExpressionAttributeNames={"#k": key},
            ExpressionAttributeValues={":w": storable(warning), ":now": _now()},
        )
    except ClientError as exc:
        # The map is seeded at creation; a job created before it existed gets it
        # now rather than failing over a warning.
        if exc.response.get("Error", {}).get("Code") != "ValidationException":
            raise
        _update(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET stageWarnings = :m, updatedAt = :now",
            ExpressionAttributeValues={":m": {key: storable(warning)}, ":now": _now()},
        )
    return warning


def record_fallback(job_id, entry):
    """Record that a deterministic fallback replaced a generated asset.

    Keyed by kind and code so a stage that runs again does not list it twice. Returns the
    stored entry.
    """
    key = f"{entry['kind']}:{entry.get('code') or ''}"
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET fallbacksUsed.#k = :v, updatedAt = :now",
        ExpressionAttributeNames={"#k": key},
        ExpressionAttributeValues={":v": storable({**entry, "recordedAt": _now()}),
                                   ":now": _now()},
    )
    return entry


def fallbacks_used(job):
    """Every fallback recorded for this job, in a stable order."""
    recorded = job.get("fallbacksUsed") or {}
    return [recorded[key] for key in sorted(recorded)]


def stage_warnings(job):
    """Non-blocking findings recorded by stages, oldest key order."""
    recorded = job.get("stageWarnings") or {}
    return [recorded[key] for key in sorted(recorded)]


def asset_items(job):
    """Every independent asset recorded for this job, as a list of entries."""
    assets = job.get("assets") or {}
    return [assets[asset_id] for asset_id in sorted(assets)]


def freeze_concept(job_id, approved):
    """Pin the approved concept and its policy review on the job.

    SPEC REVIEW_CONCEPT: the approved concept identity is frozen before
    EXTRACT_SCENE or any separation. Downstream stages read this, not the latest
    CONCEPT artifact, so both separation branches edit the same bytes even though
    the concept stage can now run again at a later attempt.
    """
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET approvedConcept = :c, updatedAt = :now",
        ExpressionAttributeValues={":c": approved, ":now": _now()},
    )


def record_concept_candidate(job_id, attempt, record):
    """Store one concept candidate and its current review under its attempt.

    Candidates are kept separately from the stage pointers, which only ever hold
    the latest attempt: selection compares them all, so each must stay reachable.
    """
    try:
        _write_candidate(job_id, attempt, record)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationException":
            raise
        # A job created before candidates were tracked has no parent map.
        _update(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET conceptCandidates = :m, updatedAt = :now",
            ExpressionAttributeValues={":m": {}, ":now": _now()},
        )
        _write_candidate(job_id, attempt, record)


def _write_candidate(job_id, attempt, record):
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET conceptCandidates.#a = :r, updatedAt = :now",
        ExpressionAttributeNames={"#a": str(int(attempt))},
        ExpressionAttributeValues={":r": record, ":now": _now()},
    )


def concept_candidates(job):
    """All recorded candidates, ordered by attempt."""
    found = job.get("conceptCandidates") or {}
    return [found[k] for k in sorted(found, key=int)]


def set_concept_correction(job_id, correction):
    """Point the next CONCEPT attempt at the correction plan it must follow."""
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET conceptCorrection = :c, updatedAt = :now",
        ExpressionAttributeValues={":c": correction, ":now": _now()},
    )


# --- design state, candidates, caches, routes and metrics -------------------


def _nested_write(job_id, parent, expression, names, values):
    """Run a nested write, creating `parent` first if the job predates it.

    DynamoDB refuses a write into a map that does not exist, and jobs created
    before these maps were added do not have them.
    """
    key = {"pk": f"JOB#{job_id}"}
    try:
        _update(
            Key=key, UpdateExpression=expression,
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ValidationException":
            raise
        _update(
            Key=key,
            UpdateExpression="SET #parent = :empty, updatedAt = :now",
            ExpressionAttributeNames={"#parent": parent},
            ExpressionAttributeValues={":empty": {}, ":now": _now()},
        )
        _update(
            Key=key, UpdateExpression=expression,
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        )


def set_design_state(job_id, pointer, expected_version=None):
    """Point the job at its current design snapshot.

    With `expected_version` the write is conditional on `designVersion` still
    being that value: it returns False when another writer has moved on, so a
    concurrent edit is detected rather than silently overwritten.
    """
    kwargs = {}
    if expected_version is not None:
        kwargs["ConditionExpression"] = "designVersion = :expected"
    values = {":p": pointer, ":v": int(pointer["version"]), ":now": _now()}
    if expected_version is not None:
        values[":expected"] = int(expected_version)
    try:
        _update(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression="SET designState = :p, designVersion = :v, updatedAt = :now",
            ExpressionAttributeValues=values,
            **kwargs,
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def record_design_candidate(job_id, attempt, record):
    """Store one assembled-and-reviewed candidate under its attempt."""
    _nested_write(
        job_id, "designCandidates", "SET designCandidates.#a = :r, updatedAt = :now",
        {"#a": str(int(attempt))}, {":r": record, ":now": _now()},
    )


def design_candidates(job):
    found = job.get("designCandidates") or {}
    return [found[k] for k in sorted(found, key=int)]


def select_design(job_id, selected):
    """Record which candidate the job will deliver, and why."""
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET selectedDesign = :s, updatedAt = :now",
        ExpressionAttributeValues={":s": selected, ":now": _now()},
    )


def set_working_scene(job_id, pointer):
    """Name the scene the next ASSEMBLE must build from (a repaired one)."""
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET workingScene = :w, updatedAt = :now",
        ExpressionAttributeValues={":w": pointer, ":now": _now()},
    )


def record_diagnostics(job_id, diagnostics):
    """Persist why a repair loop ended, with the candidate it preserved.

    A raised error's details do not survive the hop through Step Functions (only
    the message does), so the full diagnostic is stored on the job as well.
    """
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET repairDiagnostics = :d, updatedAt = :now",
        ExpressionAttributeValues={":d": diagnostics, ":now": _now()},
    )


def bump_repair(job_id, category, limit):
    """Consume one repair of `category`, or return None when its budget is spent."""
    if category not in REPAIR_CATEGORIES:
        raise ValueError(f"unknown repair category {category!r}")
    return bump_counter(job_id, f"repairs_{category}", limit)


def repair_counts(job):
    return {c: int(job.get(f"repairs_{c}", 0) or 0) for c in REPAIR_CATEGORIES}


def record_route(job_id, attempt, entry):
    """Persist the routing decision for one review (repair reason and route)."""
    _nested_write(
        job_id, "routes", "SET routes.#a = :r, updatedAt = :now",
        {"#a": str(int(attempt))}, {":r": entry, ":now": _now()},
    )


def cache_get(job, kind, key):
    """A cached result by key, or None. `kind` is validationCache or stageCache."""
    return (job.get(kind) or {}).get(key)


def cache_put(job_id, kind, key, value):
    _nested_write(
        job_id, kind, f"SET {kind}.#k = :v, updatedAt = :now",
        {"#k": key}, {":v": value, ":now": _now()},
    )


def add_metric(job_id, name, amount=1):
    """Atomically add to one numeric metric. Best effort: never fails a stage."""
    try:
        _nested_write(
            # "metrics" is a DynamoDB reserved word, so it is always aliased.
            job_id, "metrics", "ADD #root.#m :n",
            {"#root": "metrics", "#m": name}, {":n": amount},
        )
    except Exception:  # noqa: BLE001 - observability must not break the pipeline
        import logging

        logging.getLogger("layered-design.metrics").exception(
            "could not record metric %s", name
        )


def approved_concept(job):
    """The frozen approved concept pointer, or an error if none was approved.

    A missing pointer means a stage ran before REVIEW_CONCEPT approved anything,
    which must fail loudly rather than fall back to an unreviewed concept.
    """
    approved = job.get("approvedConcept")
    if not approved or not approved.get("key"):
        raise AssetInvalid("No approved concept is frozen for this job.")
    return approved


def bump_counter(job_id, field, limit):
    """Increment a bounded counter, refusing to exceed `limit`.

    Used for correction rounds and overlay repairs. The bound lives in the
    conditional expression so it holds per job, not per task retry - SPEC
    section 4 is explicit that repairs are bounded per job.
    """
    try:
        result = _update(
            Key={"pk": f"JOB#{job_id}"},
            UpdateExpression=f"SET {field} = {field} + :one, updatedAt = :now",
            ConditionExpression=f"{field} < :limit",
            ExpressionAttributeValues={":one": 1, ":limit": limit, ":now": _now()},
            ReturnValues="UPDATED_NEW",
        )
        return int(result["Attributes"][field])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


def mark_succeeded(job_id, result_key, html_hash, manifest_hash, *,
                   status=None, outcome=None):
    """Terminal success, with or without warnings.

    Called only after every final artifact is durable and the hashes match
    (SPEC section 4).

    Removes `expiresAt`: a saved design must stay retrievable by jobId after the
    30-day window that applies to diagnostic and failed jobs. Without this the
    status call would answer JOB_NOT_FOUND for a design whose files still exist.
    A design delivered with warnings is kept for the same reason: it exists to be
    opened and fixed by hand.

    `outcome` is the small summary status polling needs without opening the result
    (`stopReason`, `needsReview`, `publishReady`); it is also cleared of any error
    an earlier failed attempt recorded, since the job did deliver.
    """
    status = status or Status.SUCCEEDED
    if status not in Status.DELIVERED:
        raise ValueError(f"{status!r} is not a delivered status")
    names = {"#s": "status"}
    values = {
        ":ok": status,
        ":rk": result_key,
        ":hh": html_hash,
        ":mh": manifest_hash,
        ":stage": "FINALIZE",
        ":now": _now(),
    }
    expression = (
        "SET #s = :ok, resultKey = :rk, htmlHash = :hh, "
        "assetManifestHash = :mh, stage = :stage, updatedAt = :now"
    )
    if outcome:
        expression += ", outcome = :outcome"
        values[":outcome"] = outcome
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression=expression + " REMOVE expiresAt",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def mark_failed(job_id, error):
    """Terminal failure carrying the standard error shape."""
    _update(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET #s = :failed, #e = :err, stage = :stage, updatedAt = :now",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={
            ":failed": Status.FAILED,
            ":err": error,
            ":stage": error.get("stage"),
            ":now": _now(),
        },
    )


def resolve_owner_scope(caller_context):
    """Derive the owner scope from authenticated caller context only.

    SPEC section 6: a caller cannot establish ownership by putting a tenantId in
    the request body. The deployment adapter supplies this; the request never
    does.
    """
    if not isinstance(caller_context, dict):
        raise Unauthorized("Missing trusted caller context.")
    scope = caller_context.get("ownerScope")
    if not scope or not isinstance(scope, str):
        raise Unauthorized("Trusted caller context carries no owner scope.")
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
