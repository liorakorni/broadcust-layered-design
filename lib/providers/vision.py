"""Vision and text provider adapter.

Structured analysis only. A text model never creates raster images and never
writes HTML (SPEC section 6): pixels come from the image provider, assembly from
code, rendering from Chromium.

Every call goes through structured outputs (`response_format` with a json_schema
and `strict: true`), so the model cannot answer in prose.
"""

from __future__ import annotations

import base64
import json
import logging

import conf
from lib.errors import (
    InvalidModelJson,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
)

log = logging.getLogger("layered-design.provider.vision")

_client = None


def _openai():
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            api_key=conf.OPENAI_API_KEY,
            timeout=float(conf.VISION_PROVIDER_TIMEOUT_SECONDS),
            max_retries=0,
        )
    return _client


def image_part(png_bytes, *, detail="original", mime="image/png"):
    """Build an image content part.

    `detail="original"` is the default deliberately. Any other level resizes the
    image before the model sees it, so coordinates returned by EXTRACT_SCENE
    would describe the resized frame and every layer would be misplaced - with no
    error raised. See docs/PROBE_RESULTS.md for the verification.
    """
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": detail},
    }


def analyze(prompt, images=(), *, schema, schema_name, model=None,
            operation_id=None, detail="original"):
    """Run a structured vision/text call and return parsed JSON.

    `images` is a sequence of PNG byte strings; REVIEW passes three at once.
    """
    content = [{"type": "text", "text": prompt}]
    content.extend(image_part(img, detail=detail) for img in images)

    target = model or conf.SCENE_MODEL
    import openai

    try:
        response = _openai().chat.completions.create(
            model=target,
            messages=[{"role": "user", "content": content}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                },
            },
        )
    except openai.APITimeoutError as exc:
        raise ProviderTimeout(f"Vision model {target} timed out.") from exc
    except openai.RateLimitError as exc:
        raise ProviderRateLimited(f"Vision model {target} rate limited.") from exc
    except openai.APIConnectionError as exc:
        raise ProviderTimeout("Could not reach the vision provider.") from exc
    except openai.APIError as exc:
        raise ProviderError("Vision provider error.") from exc

    message = response.choices[0].message
    if getattr(message, "refusal", None):
        raise ProviderError("Vision model refused the request.")

    raw = message.content
    if not raw or not raw.strip():
        raise InvalidModelJson(f"Vision model {target} returned an empty response.")
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        # Should be impossible under strict structured outputs, so treat it as a
        # provider fault worth retrying rather than a content problem.
        raise InvalidModelJson(
            f"Vision model {target} returned content that is not valid JSON."
        ) from exc

    log.info(
        "vision ok model=%s schema=%s operation=%s images=%d",
        target,
        schema_name,
        operation_id,
        len(images),
    )
    return parsed


def capability_check(model):
    """Cheap liveness check used by PREFLIGHT.

    Deliberately text-only and tiny: PREFLIGHT must not perform paid image
    generation (SPEC section PREFLIGHT).
    """
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    result = analyze(
        "Reply with ok set to true.",
        (),
        schema=schema,
        schema_name="capability_v1",
        model=model,
    )
    return bool(result.get("ok"))
