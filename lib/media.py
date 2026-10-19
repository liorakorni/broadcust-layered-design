"""Stable media publishing via Cloudinary.

The HTML deliverable references assets by URL, so those URLs must outlive the
job and be publicly readable. The S3 artifact bucket is private and expires its
temporary artifacts after 30 days, which makes it the wrong place to serve from.

Uploads are idempotent by public_id: a retried stage overwrites the same object
with identical bytes rather than accumulating duplicates, and the returned URL is
stable across retries - which matters because the HTML hash depends on it.

Credentials come from Secrets Manager when CLOUDINARY_SECRET_ARN is set, so they
never sit in a Lambda environment variable. See lib/secrets.py.
"""

from __future__ import annotations

import logging

import conf
from lib import secrets
from lib.errors import MediaDeliveryUnavailable

log = logging.getLogger("layered-design.media")

_configured = False


#: Field names inside the Secrets Manager secret.
_SECRET_FIELDS = ("cloud_name", "api_key", "api_secret")


def _credentials():
    """Resolve credentials from Secrets Manager, or from the environment.

    Secrets Manager is the deployed path. The environment variables are the
    local path, where there is no role to read a secret with.
    """
    if conf.CLOUDINARY_SECRET_ARN:
        values = secrets.get_json(
            conf.CLOUDINARY_SECRET_ARN, required_keys=_SECRET_FIELDS
        )
        return {field: values[field] for field in _SECRET_FIELDS}

    conf.validate(conf.MEDIA_REQUIRED)
    return {
        "cloud_name": conf.CLOUDINARY_CLOUD_NAME,
        "api_key": conf.CLOUDINARY_API_KEY,
        "api_secret": conf.CLOUDINARY_API_SECRET,
    }


def _configure():
    global _configured
    if _configured:
        return
    import cloudinary

    cloudinary.config(secure=True, **_credentials())
    _configured = True


def reset():
    """Forget the configured client. For tests only."""
    global _configured
    _configured = False
    secrets.reset_cache()


def public_id(job_id, asset_id):
    """Deterministic identifier, so a retry replaces rather than duplicates."""
    return f"{conf.CLOUDINARY_FOLDER}/{job_id}/{asset_id}"


def publish(data, *, job_id, asset_id):
    """Upload PNG bytes and return `{url, publicId, bytes}`."""
    _configure()
    import cloudinary.uploader

    identifier = public_id(job_id, asset_id)
    try:
        response = cloudinary.uploader.upload(
            data,
            public_id=identifier,
            resource_type="image",
            format="png",
            overwrite=True,
            invalidate=True,
        )
    except Exception as exc:
        raise MediaDeliveryUnavailable(
            f"Could not publish asset {asset_id!r} to stable media."
        ) from exc

    url = response.get("secure_url")
    if not url or not url.startswith("https://"):
        raise MediaDeliveryUnavailable(
            f"Stable media returned no https URL for asset {asset_id!r}."
        )
    log.info("published assetId=%s publicId=%s", asset_id, identifier)
    return {"url": url, "publicId": identifier, "bytes": len(data)}


def check_available():
    """PREFLIGHT probe: confirm credentials work without uploading anything."""
    _configure()
    import cloudinary.api

    try:
        cloudinary.api.ping()
    except Exception as exc:
        raise MediaDeliveryUnavailable(
            "Stable media publishing is not reachable with the configured "
            "credentials."
        ) from exc
    return True
