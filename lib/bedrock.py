"""Bedrock Action Group adapter (SPEC section 7).

Translates agent invocations into the same service calls a direct invoke makes,
and wraps results in the agent response envelope. No design logic lives here,
and nothing in this module touches AWS.
"""

from __future__ import annotations

import json

import conf
from lib.errors import InvalidRequest

RUN = "runLayeredDesign"
STATUS = "getLayeredDesignStatus"


def is_bedrock_event(event):
    """True for an Action Group invocation.

    Keyed on all three envelope fields rather than guessing from payload shape:
    a malformed direct request must surface as an invalid request, not as a
    silently different operation.
    """
    return (
        isinstance(event, dict)
        and "messageVersion" in event
        and "actionGroup" in event
        and "function" in event
    )


def parameters(event):
    """Fold event.parameters name/value pairs into a dict."""
    out = {}
    for item in event.get("parameters") or []:
        if not isinstance(item, dict):
            raise InvalidRequest("Malformed parameter entry.")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidRequest("Parameter is missing a name.")
        if name in out:
            raise InvalidRequest(f"Duplicate parameter {name!r}.")
        out[name] = item.get("value")
    return out


def required(params, name):
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"Missing required parameter {name!r}.")
    return value


def parse_request_json(raw):
    """Parse the serialized start request (7.1)."""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise InvalidRequest("requestJson is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidRequest("requestJson must be a JSON object.")
    return payload


def caller_context(event):
    """Derive trusted caller context from session attributes.

    Session attributes are set by the application backend in its InvokeAgent
    call; the model cannot author them, and `ownerScope` is deliberately not an
    Action Group parameter, so no prompt can claim another tenant's jobs.
    `promptSessionAttributes` are preserved on the way out but never read for
    identity: they are rendered into the model's prompt.

    A missing attribute yields None, which `jobs.resolve_owner_scope` rejects as
    Unauthorized. This fails closed on purpose.
    """
    attrs = event.get("sessionAttributes") or {}
    return {"ownerScope": attrs.get(conf.BEDROCK_OWNER_SCOPE_ATTRIBUTE)}


#: What a compact status result may carry. Everything else (full HTML, assets,
#: layer map, QA detail) is retrieved through an authenticated direct call.
_COMPACT_FIELDS = (
    "resultKey",
    "previewKey",
    "previewUrl",
    "qaKey",
    "designFreedom",
    "reviewStrictness",
    "htmlHash",
    "assetManifestHash",
    # Whether the design may be published as it stands. A caller that reads only
    # the compact result must still be able to tell a clean success from a design
    # delivered with unresolved findings.
    "qualityPassed",
    "needsReview",
    "publishReady",
    "stopReason",
    "selectedAttempt",
    # Layered-v2 additions: small, and what a caller reading only the compact result
    # needs to tell what pipeline produced it and what was substituted.
    "pipelineVersion",
    "templateFlexibility",
    "fallbacksUsed",
    "rubricVersion",
    "milestones",
    "timing",
    "qualityScore",
    "qualityReason",
)

#: Kept when present even if null: "no score, and here is why" differs from "no field".
_COMPACT_NULLABLE = ("qualityScore", "qualityReason")

#: What a draft keeps in a compact status. The HTML and the links are left out: the agent
#: response is small, and a draft is for the application that polls directly.
_COMPACT_DRAFT_FIELDS = (
    "state", "candidateId", "attempt", "renderedAt", "timeToFirstHtmlMs", "reviewState",
    "final", "publishReady", "needsReview", "htmlHash",
)


def compact_result(response):
    """Reduce a status response to artifact references and hashes (7.5).

    Returns a new dict and never mutates the argument. Unavailable fields are
    omitted rather than set to null.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return _compact_draft(response)
    compact = _compact_draft({k: v for k, v in response.items() if k != "result"})
    # `is not None`, not truthiness: designFreedom 0 is the default and a real
    # value that a falsy filter would silently drop.
    compact["result"] = {
        k: result[k] for k in _COMPACT_FIELDS
        if k in result and (result[k] is not None or k in _COMPACT_NULLABLE)
    }
    return compact


def _compact_draft(response):
    """The response with its draft reduced to references (a new dict when it has one)."""
    draft = response.get("draft")
    if not isinstance(draft, dict):
        return response
    return {**response, "draft": {k: draft[k] for k in _COMPACT_DRAFT_FIELDS if k in draft}}


def envelope(event, result):
    """Wrap a service result in the agent response envelope (7.3).

    Deliberately no statusCode/body wrapper: that is the API Gateway shape and
    Bedrock rejects it.

    `responseState` is set only when `success` is exactly False. A failed design
    job answers success:true with status FAILED (7.5) and is not an envelope
    failure.
    """
    event = event if isinstance(event, dict) else {}
    function_response = {
        "responseBody": {"TEXT": {"body": json.dumps(result, ensure_ascii=False)}}
    }
    if result.get("success") is False:
        function_response["responseState"] = "FAILURE"
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "function": event.get("function"),
            "functionResponse": function_response,
        },
        "sessionAttributes": event.get("sessionAttributes") or {},
        "promptSessionAttributes": event.get("promptSessionAttributes") or {},
    }
