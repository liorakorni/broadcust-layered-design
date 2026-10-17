"""Resolve credentials from AWS Secrets Manager.

Keeping provider credentials out of Lambda environment variables means they do
not appear in the console, in `GetFunctionConfiguration`, in CloudFormation
state, or in a local config file. Rotating a key then takes effect on the next
cold start rather than requiring a redeploy.

The cost is one API call per cold start, so results are cached for the life of
the container. The cache is keyed by secret ID, not global, so a second secret
can be added without the two interfering.
"""

from __future__ import annotations

import json
import logging

import boto3
from botocore.exceptions import ClientError

import conf
from lib.errors import ConfigurationInvalid

log = logging.getLogger("layered-design.secrets")

_cache = {}
_client = None


def _secrets():
    global _client
    if _client is None:
        _client = boto3.client("secretsmanager", region_name=conf.AWS_REGION)
    return _client


def get_json(secret_id, *, required_keys=()):
    """Fetch a JSON secret and return it as a dict.

    `required_keys` are checked here so a half-populated secret fails with a
    message naming what is missing, rather than surfacing later as an
    authentication error from the provider - which is far harder to trace back
    to its cause.
    """
    if secret_id in _cache:
        return _cache[secret_id]

    try:
        response = _secrets().get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            raise ConfigurationInvalid(
                f"Secret {secret_id!r} does not exist in this account or region."
            ) from exc
        if code in ("AccessDeniedException", "UnrecognizedClientException"):
            raise ConfigurationInvalid(
                f"This function is not permitted to read secret {secret_id!r}."
            ) from exc
        raise ConfigurationInvalid(
            f"Could not read secret {secret_id!r} from Secrets Manager."
        ) from exc

    payload = response.get("SecretString")
    if not payload:
        raise ConfigurationInvalid(
            f"Secret {secret_id!r} holds binary data; a JSON string is expected."
        )
    try:
        values = json.loads(payload)
    except ValueError as exc:
        raise ConfigurationInvalid(
            f"Secret {secret_id!r} is not valid JSON."
        ) from exc
    if not isinstance(values, dict):
        raise ConfigurationInvalid(
            f"Secret {secret_id!r} is not a JSON object of key/value pairs."
        )

    missing = [k for k in required_keys if not str(values.get(k, "")).strip()]
    if missing:
        raise ConfigurationInvalid(
            f"Secret {secret_id!r} is missing: {', '.join(sorted(missing))}"
        )

    # Never log the values, and never log the full ARN either - it names the
    # account. The secret's short name is enough to diagnose.
    log.info("loaded secret %s", secret_id.rsplit(":", 1)[-1])
    _cache[secret_id] = values
    return values


def reset_cache():
    """Drop cached secrets. For tests; a container never needs this."""
    _cache.clear()
