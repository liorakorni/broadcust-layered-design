"""Restricted diagnostics for one image-provider call.

A decomposition that comes back wrong has to be diagnosable from what was actually
sent and what actually came back, not from the prompt template and a guess. This
module records, per call:

* the provider, model, operation and the settings that were actually passed;
* the effective prompt (after substitution) and a version for the template;
* every attached input image, in order, with its sha256 and pixel size, and the
  mask the same way (or the fact that there was none);
* the raw provider output hash and size, and the hash of what was stored, so a
  processing step between the two is visible as a difference rather than assumed
  absent;
* the provider request id, each processing step and the durations.

The record is an artifact of the job, written under the stage that made the call. It
is RESTRICTED: it holds the effective prompt (which carries brief text), so it is
never returned in a status or result payload and never put in a manifest.

Never recorded, by construction: credentials, authorization headers, signed URLs,
or image bytes in any encoding. `_scrub` enforces that on everything that goes in,
so a future caller cannot leak one by passing it as a "note".
"""

from __future__ import annotations

import hashlib
import io
import logging
import re

from lib import artifacts, jobs, prompts

log = logging.getLogger("layered-design.provider-trace")

TRACE_VERSION = 1

#: Anything that looks like one of these is replaced before it is stored.
_SECRET_PATTERNS = (
    re.compile(r"https?://\S*[?&](?:X-Amz-Signature|Signature|sig|token|access_token)=\S*", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"data:image/[a-z+]+;base64,[A-Za-z0-9+/=]{64,}"),
    # A long unbroken base64 run is an image or a key, never prose.
    re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"),
)
_REDACTED = "[redacted]"


def prompt_version(prompt_name):
    """A short, stable version of a prompt template: a digest of its text.

    A name of the form "a+b" is a prompt built from two templates (a repair is the
    extraction prompt plus its correction); the version covers both, in order.
    """
    text = "\n".join(prompts.load(part) for part in prompt_name.split("+"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def describe_image(role, data):
    """The identity of one image: role, sha256, bytes and pixel size, never the bytes."""
    entry = {"role": role, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            entry.update(width=image.width, height=image.height, mode=image.mode)
    except Exception:  # noqa: BLE001 - a diagnostic must not fail the stage
        entry["decodable"] = False
    return entry


def build(*, stage, operation_id, prompt_name, prompt, inputs, mask=None, result,
          stored_sha256, stored_bytes, processing=(), notes=None, started_at=None):
    """Assemble the trace for one call. `inputs` is a list of (role, bytes) in the
    order they were attached; `mask` is bytes or None."""
    request = dict(result.get("request") or {})
    trace = {
        "traceVersion": TRACE_VERSION,
        "restricted": True,
        "stage": stage,
        "operationId": operation_id,
        "provider": request.get("provider", "openai"),
        "model": result.get("model") or request.get("model"),
        "operation": request.get("operation"),
        "settings": {
            key: request.get(key)
            for key in ("size", "quality", "background", "outputFormat",
                        "imageCount", "maskAttached")
        },
        "prompt": {
            "name": prompt_name,
            "version": prompt_version(prompt_name),
            "effective": prompt,
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "chars": len(prompt),
        },
        "inputs": [describe_image(role, data) for role, data in inputs],
        "mask": describe_image("mask", mask) if mask is not None else None,
        "output": {
            "rawSha256": result.get("rawSha256"),
            "rawBytes": result.get("rawBytes"),
            "storedSha256": stored_sha256,
            "storedBytes": stored_bytes,
            # Equal hashes mean nothing touched the provider's bytes between the
            # response and the artifact; unequal means a step is missing below.
            "storedIsRaw": (result.get("rawSha256") == stored_sha256
                            if result.get("rawSha256") else None),
        },
        "providerRequestId": result.get("requestId"),
        "processing": list(processing),
        "durationMs": result.get("durationMs"),
        "startedAt": started_at,
        "notes": notes or {},
    }
    return _scrub(trace)


def record(job_id, stage, attempt, name, trace):
    """Persist a trace. Never raises: diagnostics must not fail the stage they describe."""
    try:
        key = artifacts.artifact_key(job_id, stage, attempt, name)
        artifacts.put_json(key, trace, overwrite=True)
        jobs.add_metric(job_id, "providerTraces")
        return key
    except Exception as exc:  # noqa: BLE001
        log.warning("provider trace not stored jobId=%s stage=%s error=%s",
                    job_id, stage, type(exc).__name__)
        return None


def _scrub(value):
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(_REDACTED, value)
        return value
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value
