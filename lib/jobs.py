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
from lib.errors import IdempotencyConflict, JobNotFound, Status, Unauthorized

#: Jobs are diagnostic after completion; artifacts carry the real output.
JOB_TTL_DAYS = 30

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
        _tbl().put_item(
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


def create_job(job_id, owner_scope, request_hash, request_key, canvas):
    """Record the job as QUEUED before any execution starts."""
    _tbl().put_item(
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
            "attempts": {},
            "correctionRounds": 0,
            "overlayRepairs": 0,
            "createdAt": _now(),
            "updatedAt": _now(),
            "expiresAt": _ttl(),
        }
    )


def attach_execution(job_id, execution_arn):
    """Record the execution ARN, only if one is not already attached.

    The condition is the reconciliation guard: if a previous invocation already
    started an execution, this refuses to overwrite it, so a retry can never
    orphan a running job and start a second one.
    """
    try:
        _tbl().update_item(
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
    return item


def get_job_unscoped(job_id):
    """Read a job without an ownership check.

    Only for the stage Lambda, which runs inside the workflow for a job that was
    already authorized at acceptance. Never call this from a caller-facing path.
    """
    item = _tbl().get_item(Key={"pk": f"JOB#{job_id}"}).get("Item")
    if not item:
        raise JobNotFound(f"Job {job_id} was not found.")
    return item


def set_stage(job_id, stage):
    _tbl().update_item(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression="SET stage = :s, #st = :running, updatedAt = :now",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":s": stage,
            ":running": Status.RUNNING,
            ":now": _now(),
        },
    )


def record_artifacts(job_id, stage, attempt, artifacts):
    """Persist a stage's artifact pointers on the job record."""
    _tbl().update_item(
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


def bump_counter(job_id, field, limit):
    """Increment a bounded counter, refusing to exceed `limit`.

    Used for correction rounds and overlay repairs. The bound lives in the
    conditional expression so it holds per job, not per task retry - SPEC
    section 4 is explicit that repairs are bounded per job.
    """
    try:
        result = _tbl().update_item(
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


def mark_succeeded(job_id, result_key, html_hash, manifest_hash):
    """Terminal success.

    Called only after every final artifact is durable and the hashes match
    (SPEC section 4).
    """
    _tbl().update_item(
        Key={"pk": f"JOB#{job_id}"},
        UpdateExpression=(
            "SET #s = :ok, resultKey = :rk, htmlHash = :hh, "
            "assetManifestHash = :mh, stage = :stage, updatedAt = :now"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":ok": Status.SUCCEEDED,
            ":rk": result_key,
            ":hh": html_hash,
            ":mh": manifest_hash,
            ":stage": "FINALIZE",
            ":now": _now(),
        },
    )


def mark_failed(job_id, error):
    """Terminal failure carrying the standard error shape."""
    _tbl().update_item(
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
