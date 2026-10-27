"""Image provider adapter.

Receives a prompt, real source bytes, a transparency requirement, canvas and
operation ID; returns real bytes plus the provider's request identity
(SPEC section 6).

Two things here are load-bearing and easy to get wrong:

* `background="transparent"` is only honoured by models that support it.
  gpt-image-2 rejects it outright, which is why conf pins a 2.5 model. If the
  provider says transparency is unsupported, that is terminal - never fall back
  to keying out white.
* A timeout after a paid generation leaves the outcome unknown. That raises
  ProviderOutcomeUnknown rather than retrying, because a retry is precisely what
  would pay twice.
"""

from __future__ import annotations

import base64
import logging

import conf
from lib.errors import (
    ProviderError,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderTimeout,
    TransparencyUnsupported,
)

log = logging.getLogger("layered-design.provider.image")

#: GPT Image renders only these. A requested canvas is generated at the nearest
#: aspect and resampled to target in lib/validate_assets.py.
_SUPPORTED = {
    "square": "1024x1024",
    "portrait": "1024x1536",
    "landscape": "1536x1024",
}

_client = None


def _openai():
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            api_key=conf.OPENAI_API_KEY,
            timeout=float(conf.IMAGE_PROVIDER_TIMEOUT_SECONDS),
            max_retries=0,  # Retries are the workflow's job, not the SDK's.
        )
    return _client


def generation_size(width, height):
    """Map a requested canvas onto the nearest supported generation size."""
    ratio = width / height
    if ratio > 1.15:
        return _SUPPORTED["landscape"]
    if ratio < 0.87:
        return _SUPPORTED["portrait"]
    return _SUPPORTED["square"]


def generate(prompt, width, height, *, transparent=False, operation_id=None,
             quality="high"):
    """Create an image from text. Returns `{bytes, mimeType, size, requestId}`."""
    params = {
        "model": conf.IMAGE_MODEL,
        "prompt": prompt,
        "size": generation_size(width, height),
        "quality": quality,
        "output_format": "png",
        "background": "transparent" if transparent else "opaque",
    }
    # output_compression is deliberately never sent: it is invalid for PNG.
    return _call(lambda c: c.images.generate(**params), operation_id, transparent,
                 request=_describe("images.generate", params, image_count=0))


def edit(prompt, source_bytes, width, height, *, transparent=False,
         operation_id=None, quality="high", mime="image/png", mask=None):
    """Edit real source bytes. Returns `{bytes, mimeType, size, requestId}`.

    `mask` is PNG bytes the same size as the source. Where its alpha is 0 the
    model repaints; everywhere else it keeps the source pixels. Without one the
    model re-composes the whole frame, which is how CLEAN_PHOTO came back zoomed
    and shifted relative to the concept it was cleaning.
    """
    extension = "png" if mime == "image/png" else "jpg"
    params = {
        "model": conf.IMAGE_MODEL,
        "image": (f"source.{extension}", source_bytes, mime),
        "prompt": prompt,
        "size": generation_size(width, height),
        "quality": quality,
        "output_format": "png",
        "background": "transparent" if transparent else "opaque",
    }
    if mask is not None:
        params["mask"] = ("mask.png", mask, "image/png")
    return _call(lambda c: c.images.edit(**params), operation_id, transparent,
                 request=_describe("images.edit", params, image_count=1,
                                   mask=mask is not None))


def _describe(operation, params, *, image_count, mask=False):
    """What was actually sent, minus the prompt and the bytes (see lib/provider_trace.py)."""
    return {
        "provider": "openai", "operation": operation, "model": params["model"],
        "size": params["size"], "quality": params["quality"],
        "background": params["background"], "outputFormat": params["output_format"],
        "imageCount": image_count, "maskAttached": bool(mask),
    }


def _call(invoke, operation_id, transparent, request=None):
    import hashlib
    import time

    import openai

    started = time.monotonic()
    try:
        response = invoke(_openai())
    except openai.APITimeoutError as exc:
        # The request may have completed and been billed; we cannot tell.
        raise ProviderOutcomeUnknown(
            "Image provider timed out; the generation outcome is unknown and "
            "will not be retried automatically.",
        ) from exc
    except openai.RateLimitError as exc:
        raise ProviderRateLimited("Image provider rate limited.") from exc
    except openai.BadRequestError as exc:
        message = str(getattr(exc, "message", "")) or str(exc)
        if transparent and _is_transparency_refusal(message):
            raise TransparencyUnsupported(
                f"Model {conf.IMAGE_MODEL} does not support transparent "
                "backgrounds. Configure a model that does; do not substitute an "
                "opaque overlay."
            ) from exc
        # Never surface the provider's raw message: it echoes the prompt back.
        raise ProviderError("Image provider rejected the request.") from exc
    except openai.APIConnectionError as exc:
        raise ProviderTimeout("Could not reach the image provider.") from exc
    except openai.APIError as exc:
        raise ProviderError("Image provider error.") from exc

    datum = response.data[0]
    payload = getattr(datum, "b64_json", None)
    if not payload:
        raise ProviderError("Image provider returned no image payload.")

    request_id = getattr(response, "_request_id", None) or operation_id
    log.info(
        "image provider ok model=%s operation=%s requestId=%s",
        conf.IMAGE_MODEL,
        operation_id,
        request_id,
    )
    raw = base64.b64decode(payload)
    return {
        "bytes": raw,
        "mimeType": "image/png",
        "requestId": request_id,
        "model": conf.IMAGE_MODEL,
        # For lib/provider_trace.py. The adapter returns the provider's bytes as
        # they came; the hash is here so a later step that changes them is visible.
        "rawSha256": hashlib.sha256(raw).hexdigest(),
        "rawBytes": len(raw),
        "durationMs": int((time.monotonic() - started) * 1000),
        "request": request or {},
    }


def _is_transparency_refusal(message):
    lowered = message.lower()
    return "transparent" in lowered and (
        "not supported" in lowered or "unsupported" in lowered
    )
