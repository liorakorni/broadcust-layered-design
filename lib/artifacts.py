"""Immutable S3 artifact storage.

Keys follow `layered-design/<jobId>/<stage>/<attempt>/<name>` and are append-only.
A retry writes a new attempt rather than overwriting, so every stage's inputs stay
byte-identical for the life of the job. CLEAN_PHOTO and DECORATION_OVERLAY both
depend on that: they must edit the same concept bytes (invariant 3).

All reads go through `job_key`, which refuses keys outside the job's own prefix.
SPEC section 6 requires reading generated assets by validated job-owned key rather
than by arbitrary URL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

import boto3
from botocore.exceptions import ClientError

import conf
from lib.errors import (
    ArtifactAccessDenied, ArtifactNotFound, AssetInvalid, LayeredDesignError, Stage,
)

log = logging.getLogger("layered-design.artifacts")

ROOT_PREFIX = "layered-design"

#: Applied to every object on write; see `put_bytes` and `retain`.
TEMPORARY_TAGGING = "lifecycle=temporary"

#: Job IDs appear in S3 keys, so they are restricted to characters that cannot
#: traverse or escape the prefix.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=conf.AWS_REGION)
    return _s3


def _check_segment(value, label):
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
        raise AssetInvalid(f"Unsafe {label} in artifact key.")
    return value


def job_prefix(job_id):
    return f"{ROOT_PREFIX}/{_check_segment(job_id, 'jobId')}/"


def artifact_key(job_id, stage, attempt, name):
    """Build the immutable key for one artifact.

    `name` may contain a single level of nesting (e.g. "assets/logo.png") but
    never a parent reference.
    """
    _check_segment(job_id, "jobId")
    if stage not in Stage.ALL:
        raise AssetInvalid(f"Unknown stage {stage!r} in artifact key.")
    if not isinstance(attempt, int) or attempt < 1:
        raise AssetInvalid("Attempt must be a positive integer.")
    if not isinstance(name, str) or not name or ".." in name or name.startswith("/"):
        raise AssetInvalid("Unsafe artifact name.")
    return f"{job_prefix(job_id)}{stage}/{attempt}/{name}"


def job_key(job_id, key):
    """Return `key` only if it belongs to this job, else raise.

    Every read of a model- or state-supplied key goes through this. Without it a
    crafted pointer in workflow state could read another caller's artifacts.
    """
    prefix = job_prefix(job_id)
    if not isinstance(key, str) or not key.startswith(prefix):
        raise AssetInvalid("Artifact key does not belong to this job.")
    if ".." in key:
        raise AssetInvalid("Unsafe artifact key.")
    return key


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put_bytes(key, data, content_type, *, overwrite=False):
    """Store bytes and return `{key, sha256, bytes}`.

    Writes are conditional by default so an accidental reuse of a key surfaces as
    an error instead of silently replacing an artifact another stage already read.
    """
    # Every object starts temporary. The bucket's lifecycle rule only expires
    # objects carrying this tag, and FINALIZE retags a saved design's objects
    # via `retain`, so saved designs outlive the 30-day expiry.
    extra = {"ContentType": content_type, "Tagging": TEMPORARY_TAGGING}
    if not overwrite:
        extra["IfNoneMatch"] = "*"
    try:
        _client().put_object(
            Bucket=conf.ARTIFACT_BUCKET, Key=key, Body=data, **extra
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise AssetInvalid(
                f"Artifact already exists at {key}; artifacts are immutable."
            ) from exc
        raise
    return {"key": key, "sha256": sha256_hex(data), "bytes": len(data)}


def get_bytes(job_id, key):
    """Read an artifact belonging to `job_id`.

    Absent and forbidden are different failures and are reported as different
    classes. S3 only answers NoSuchKey to a caller allowed to list the bucket;
    without that permission a missing key comes back as AccessDenied, so treating
    403 as "not there" turns a missing grant into a silent fallback.
    """
    safe = job_key(job_id, key)
    try:
        response = _client().get_object(Bucket=conf.ARTIFACT_BUCKET, Key=safe)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        # The bucket and key are ours and safe to log; nothing else from the
        # response is, so the raw error never reaches the message.
        if code in ("NoSuchKey", "404") or status == 404:
            log.info("artifact absent bucket=%s key=%s", conf.ARTIFACT_BUCKET, safe)
            raise ArtifactNotFound(f"Artifact not found: {safe}") from exc
        if code in ("AccessDenied", "403") or status == 403:
            log.warning(
                "artifact read denied bucket=%s key=%s s3Code=%s",
                conf.ARTIFACT_BUCKET, safe, code,
            )
            raise ArtifactAccessDenied(
                f"Access denied reading {safe} from {conf.ARTIFACT_BUCKET}. This is "
                "a permission fault, not a missing artifact."
            ) from exc
        log.warning(
            "artifact read failed bucket=%s key=%s s3Code=%s",
            conf.ARTIFACT_BUCKET, safe, code,
        )
        raise
    return response["Body"].read()


def json_default(value):
    """Serialise what DynamoDB hands back: numbers arrive as Decimal."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def put_json(key, obj, *, overwrite=False):
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=json_default).encode("utf-8")
    return put_bytes(
        key, data, "application/json; charset=utf-8", overwrite=overwrite
    )


_RETAINED = {"TagSet": [{"Key": "lifecycle", "Value": "retained"}]}


def retain(job_id, keys):
    """Exempt these objects from the temporary-artifact expiry.

    S3 replaces the whole tag set, so this is idempotent and safe to repeat.
    Every key is checked against the job prefix first.
    """
    for key in sorted(set(keys)):
        _client().put_object_tagging(
            Bucket=conf.ARTIFACT_BUCKET, Key=job_key(job_id, key), Tagging=_RETAINED
        )


def get_json(job_id, key):
    try:
        return json.loads(get_bytes(job_id, key).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AssetInvalid(f"Artifact at {key} is not valid JSON.") from exc


def get_json_optional(job_id, key):
    """`get_json`, or None when the object is genuinely absent.

    Only for artifacts that are optional by contract - a probe for a record a
    retried invocation may have written. A denied read, a malformed document and
    every other failure propagate, so a missing permission cannot be mistaken for
    a missing file.
    """
    try:
        return get_json(job_id, key)
    except ArtifactNotFound:
        return None


def canonical_manifest_hash(entries):
    """Hash the final asset manifest (SPEC section 4, CORRECTION AND FINALIZE).

    Ordering is canonical by asset ID so the same set of assets always produces
    the same hash regardless of the order stages happened to emit them.
    """
    ordered = sorted(
        (
            {
                "assetId": e["assetId"],
                "key": e["key"],
                "sha256": e["sha256"],
                "url": e.get("url", ""),
            }
            for e in entries
        ),
        key=lambda e: e["assetId"],
    )
    payload = json.dumps(ordered, ensure_ascii=False, sort_keys=True, default=json_default).encode("utf-8")
    return sha256_hex(payload)


__all__ = [
    "ROOT_PREFIX",
    "artifact_key",
    "canonical_manifest_hash",
    "get_bytes",
    "get_json",
    "job_key",
    "job_prefix",
    "put_bytes",
    "put_json",
    "sha256_hex",
]
