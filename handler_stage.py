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
    # Also workflow plumbing rather than a pipeline stage: a bounded targeted
    # edit of the overlay, after which the workflow re-enters VALIDATE_ASSETS.
    if event.get("operation") == "REPAIR_OVERLAY":
        from lib.stages import separation

        artifacts = separation.repair_overlay(job_id=job_id, attempt=attempt)
        _log(job_id, Stage.DECORATION_OVERLAY, attempt, started)
        return {"ok": True, "stage": Stage.DECORATION_OVERLAY, "artifacts": artifacts}

    # The photo is rebuilt after an overlay repair because its mask is the
    # overlay's shape: a repaired overlay needs a photo repainted around it.
    if event.get("operation") == "REBUILD_PHOTO":
        from lib.stages import separation

        artifacts = separation.clean_photo(
            job_id=job_id, attempt=attempt, after_repair=True
        )
        _log(job_id, Stage.CLEAN_PHOTO, attempt, started)
        return {"ok": True, "stage": Stage.CLEAN_PHOTO, "artifacts": artifacts}

    if event.get("operation") == "CORRECT":
        from lib.stages import review

        try:
            artifacts = review.correct(job_id=job_id, attempt=attempt)
        except LayeredDesignError as exc:
            # As for CORRECT_CONCEPT below: without the stage in the message
            # RecordFailure reports the exhaustion against FINALIZE.
            if Stage.REVIEW not in exc.message:
                exc.message = f"{exc.message} [{Stage.REVIEW}]"
                exc.args = (exc.message,)
            raise
        _log(job_id, Stage.REVIEW, attempt, started)
        return {"ok": True, "stage": Stage.REVIEW, "artifacts": artifacts}

    if event.get("operation") == "CORRECT_CONCEPT":
        from lib.stages import review_concept

        try:
            artifacts = review_concept.correct_concept(job_id=job_id, attempt=attempt)
        except LayeredDesignError as exc:
            # Carry the stage through Step Functions inside the message, as the
            # pipeline stages below do, so RecordFailure reports REVIEW_CONCEPT.
            if Stage.REVIEW_CONCEPT not in exc.message:
                exc.message = f"{exc.message} [{Stage.REVIEW_CONCEPT}]"
                exc.args = (exc.message,)
            raise
        _log(job_id, Stage.REVIEW_CONCEPT, attempt, started)
        return {"ok": True, "stage": Stage.REVIEW_CONCEPT, "artifacts": artifacts}

    if event.get("operation") == "SELECT_CONCEPT":
        from lib.stages import review_concept

        try:
            artifacts = review_concept.select_concept(job_id=job_id, attempt=attempt)
        except LayeredDesignError as exc:
            if Stage.REVIEW_CONCEPT not in exc.message:
                exc.message = f"{exc.message} [{Stage.REVIEW_CONCEPT}]"
                exc.args = (exc.message,)
            raise
        _log(job_id, Stage.REVIEW_CONCEPT, attempt, started)
        return {"ok": True, "stage": Stage.REVIEW_CONCEPT, "artifacts": artifacts}

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
        # Only the exception class name and message survive the hop to Step
        # Functions, so unless the stage travels inside the message RecordFailure
        # cannot tell where the job died and reports every failure as the last
        # stage. _stage_from_message is what reads this back.
        if stage not in exc.message:
            exc.message = f"{exc.message} [{stage}]"
            exc.args = (exc.message,)
        raise
    except Exception:
        # Never surface a raw exception message: it can carry provider payloads.
        # The stage name is our own constant and safe to include.
        log.exception("stage failed jobId=%s stage=%s", job_id, stage)
        _log(job_id, stage, attempt, started, error=Code.INTERNAL)
        raise LayeredDesignError(
            f"Internal error in {stage}.", stage=stage, code=Code.INTERNAL
        )

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
        "category": "technical",
    }
    if isinstance(failure, dict):
        name = failure.get("Error") or ""
        cause = failure.get("Cause") or ""
        try:
            parsed = json.loads(cause)
            message = parsed.get("errorMessage") or ""
        except (ValueError, TypeError):
            message = cause if isinstance(cause, str) else ""
        raised = _error_classes().get(name)
        if raised is not None:
            error["code"] = raised.code
            error["retryable"] = raised.retryable
            error["category"] = raised.category
        if message:
            error["message"] = message
        stage = _stage_from_message(message)
        if stage:
            error["stage"] = stage

    jobs.mark_failed(job_id, error)
    log.info(json.dumps({"jobId": job_id, "recordedFailure": error["code"]}))
    return {"ok": True, "stage": Stage.FINALIZE, "artifacts": {"error": error["code"]}}


#: Lambda surfaces a raised exception to Step Functions by class name, and the
#: name is the only handle left for recovering what the failure actually was.
_ERROR_CLASSES = None


def _error_classes():
    """Map every raisable error class name to the class itself.

    Both the reported code and whether the failure was retryable come from the
    class, rather than from a table maintained alongside it. The table this
    replaces had fallen nine classes behind - AssemblyFailed and CopyInvalid
    among them - and every one of those was being recorded as a generic internal
    error that callers could not act on.
    """
    global _ERROR_CLASSES
    if _ERROR_CLASSES is None:
        from lib import errors

        found = {}
        pending = [errors.LayeredDesignError]
        while pending:
            cls = pending.pop()
            found[cls.__name__] = cls
            pending.extend(cls.__subclasses__())
        _ERROR_CLASSES = found
    return _ERROR_CLASSES


def _stage_from_message(message):
    # Longest name first: CONCEPT is a substring of REVIEW_CONCEPT, so scanning in
    # pipeline order would record every REVIEW_CONCEPT failure against CONCEPT.
    for stage in sorted(Stage.PIPELINE, key=len, reverse=True):
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
