"""Stage Lambda: execute exactly one bounded workflow stage per invocation.

Receives `{jobId, stage, attempt, assetId}` and returns small artifact pointers.
Image bytes, scene documents and HTML never travel through workflow state.

Invariant 1: a stage with no implementation raises NotImplementedStage. It never
returns a plausible-looking empty result, because a pipeline that reports success
while producing nothing is far more expensive to diagnose than one that fails.
"""

from __future__ import annotations

import json
import logging
import time

import conf
from lib.errors import (
    Code,
    LayeredDesignError,
    NotImplementedStage,
    Stage,
    as_error_dict,
)

logging.basicConfig()
log = logging.getLogger("layered-design.stage")
log.setLevel(logging.INFO)

#: Populated on first use from lib.stages. A stage absent from this map raises
#: NotImplementedStage naming the real stage, so an unfinished pipeline fails
#: loudly at the right place.
STAGE_HANDLERS = {}


def register(stage):
    """Decorator binding a callable to a stage name."""

    def wrap(fn):
        STAGE_HANDLERS[stage] = fn
        return fn

    return wrap


def _registry():
    """Load stage implementations lazily.

    Deferred so importing this module needs neither pydantic nor boto3, which
    keeps the workflow's structural tests fast and makes a missing dependency
    surface as a stage failure rather than a container that will not start.
    """
    if not STAGE_HANDLERS:
        from lib.stages import build_registry

        STAGE_HANDLERS.update(build_registry())
    return STAGE_HANDLERS


def handler(event, context):
    started = time.monotonic()
    job_id = event.get("jobId")
    stage = event.get("stage")
    attempt = event.get("attempt", 1)
    asset_id = event.get("assetId")

    # The workflow's terminal catch routes here to persist the structured error
    # on the job, so getLayeredDesignStatus can report the real failing stage.
    if event.get("recordFailure"):
        return _record_failure(job_id, event.get("failure"))

    # The correction branch is workflow plumbing rather than a pipeline stage:
    # it consumes a bounded round and advances the attempt counter.
    if event.get("operation") == "CORRECT":
        from lib.stages import review

        artifacts = review.correct(job_id=job_id, attempt=attempt)
        _log(job_id, Stage.REVIEW, attempt, started)
        return {"ok": True, "stage": Stage.REVIEW, "artifacts": artifacts}

    if stage not in Stage.PIPELINE:
        raise LayeredDesignError(
            f"Unknown stage {stage!r}.",
            stage=Stage.ACCEPT_REQUEST,
            code=Code.INVALID_REQUEST,
        )

    conf.validate(conf.STAGE_REQUIRED)

    try:
        run = _registry().get(stage)
        if run is None:
            raise NotImplementedStage(stage)
        artifacts = run(job_id=job_id, attempt=attempt, asset_id=asset_id)
    except LayeredDesignError as exc:
        _log(job_id, stage, attempt, started, error=exc.code)
        raise
    except Exception:
        # Never surface a raw exception message: it can carry provider payloads.
        log.exception("stage failed jobId=%s stage=%s", job_id, stage)
        _log(job_id, stage, attempt, started, error=Code.INTERNAL)
        raise LayeredDesignError("Internal error.", stage=stage, code=Code.INTERNAL)

    _log(job_id, stage, attempt, started)
    return {"ok": True, "stage": stage, "artifacts": artifacts}


def _record_failure(job_id, failure):
    """Persist a terminal error on the job record.

    Step Functions reports a raised exception as `{Error, Cause}`, where Cause is
    a JSON string containing the message. The structured error we raised is
    recovered from there when possible; otherwise the stage name in `Error` is
    still enough to report something truthful rather than a generic message.
    """
    from lib import jobs

    error = {
        "stage": Stage.FINALIZE,
        "code": Code.INTERNAL,
        "message": "The job failed.",
        "retryable": False,
    }
    if isinstance(failure, dict):
        name = failure.get("Error") or ""
        cause = failure.get("Cause") or ""
        try:
            parsed = json.loads(cause)
            message = parsed.get("errorMessage") or ""
        except (ValueError, TypeError):
            message = cause if isinstance(cause, str) else ""
        error["code"] = _CODE_BY_EXCEPTION.get(name, Code.INTERNAL)
        if message:
            error["message"] = message
        stage = _stage_from_message(message)
        if stage:
            error["stage"] = stage

    jobs.mark_failed(job_id, error)
    log.info(json.dumps({"jobId": job_id, "recordedFailure": error["code"]}))
    return {"ok": True, "stage": Stage.FINALIZE, "artifacts": {"error": error["code"]}}


#: Lambda surfaces a raised exception to Step Functions by class name.
_CODE_BY_EXCEPTION = {
    "NotImplementedStage": Code.NOT_IMPLEMENTED,
    "TransparencyUnsupported": Code.TRANSPARENCY_UNSUPPORTED,
    "InvalidTransparency": Code.INVALID_TRANSPARENCY,
    "InvalidScene": Code.INVALID_SCENE,
    "AssetInvalid": Code.ASSET_INVALID,
    "RenderFailed": Code.RENDER_FAILED,
    "FontMissing": Code.FONT_MISSING,
    "ReviewBlocked": Code.REVIEW_BLOCKED,
    "CorrectionExhausted": Code.CORRECTION_EXHAUSTED,
    "MediaDeliveryUnavailable": Code.MEDIA_DELIVERY_UNAVAILABLE,
    "ProviderOutcomeUnknown": Code.PROVIDER_OUTCOME_UNKNOWN,
    "ProviderTimeout": Code.PROVIDER_TIMEOUT,
    "ProviderRateLimited": Code.PROVIDER_RATE_LIMITED,
    "ProviderError": Code.PROVIDER_ERROR,
    "InvalidModelJson": Code.INVALID_MODEL_JSON,
    "ConfigurationInvalid": Code.CONFIGURATION_INVALID,
    "CapabilityMissing": Code.CAPABILITY_MISSING,
}


def _stage_from_message(message):
    for stage in Stage.PIPELINE:
        if stage in message:
            return stage
    return None


def _log(job_id, stage, attempt, started, error=None):
    """Structured log line. Never contains secrets, base64 or customer content."""
    log.info(
        json.dumps(
            {
                "jobId": job_id,
                "stage": stage,
                "attempt": attempt,
                "durationMs": int((time.monotonic() - started) * 1000),
                "error": error,
            }
        )
    )


def to_error(exc, stage):
    """Expose the standard error shape for the workflow's catch handler."""
    return as_error_dict(exc, default_stage=stage)
