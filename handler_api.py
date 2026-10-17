"""API Lambda: start jobs and read their status.

Two operations, per SPEC section 2:

  runLayeredDesign(request, trustedCallerContext)   -> jobId, immediately
  getLayeredDesignStatus(jobId, trustedCallerContext)

Acceptance is not completion. This returns as soon as the job is durable and the
workflow start is acknowledged; it never waits for image generation and never
holds a connection open for the pipeline (SPEC section 2).
"""

from __future__ import annotations

import json
import logging

import boto3
from botocore.exceptions import ClientError

import conf
from lib import artifacts, jobs
from lib.errors import (
    Code,
    InvalidRequest,
    LayeredDesignError,
    Stage,
    Status,
    as_error_dict,
)
from lib.schemas import canonical_request_hash, parse_start_request

logging.basicConfig()
log = logging.getLogger("layered-design.api")
log.setLevel(logging.INFO)

_sfn = None


def _states():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions", region_name=conf.AWS_REGION)
    return _sfn


def handler(event, context):
    """Dispatch on an explicit operation.

    SPEC section 7 requires the operation to be explicit: guessing it from the
    shape of the payload would make a malformed start request look like a status
    lookup.
    """
    try:
        conf.validate(conf.API_REQUIRED)
        operation = event.get("operation")
        if operation == "runLayeredDesign":
            return run_layered_design(
                event.get("request"), event.get("trustedCallerContext")
            )
        if operation == "getLayeredDesignStatus":
            return get_layered_design_status(
                event.get("jobId"), event.get("trustedCallerContext")
            )
        raise InvalidRequest(
            "Missing or unknown operation; expected runLayeredDesign or "
            "getLayeredDesignStatus."
        )
    except LayeredDesignError as exc:
        return {"success": False, "accepted": False, "error": exc.to_dict()}
    except Exception:
        log.exception("unhandled api error")
        return {
            "success": False,
            "accepted": False,
            "error": as_error_dict(None, default_stage=Stage.ACCEPT_REQUEST),
        }


def run_layered_design(payload, caller_context):
    owner_scope = jobs.resolve_owner_scope(caller_context)
    request = parse_start_request(payload)
    request_hash = canonical_request_hash(request)

    job_id, created = jobs.claim_idempotency(
        owner_scope, request.requestId, request_hash
    )

    if created:
        # Persist the canonical request before anything can consume it, so every
        # stage reads identical bytes regardless of retries.
        request_key = artifacts.artifact_key(
            job_id, Stage.ACCEPT_REQUEST, 1, "request.json"
        )
        artifacts.put_json(request_key, request.model_dump(mode="json"))
        jobs.create_job(
            job_id,
            owner_scope,
            request_hash,
            request_key,
            {"width": request.canvas.width, "height": request.canvas.height},
        )

    _ensure_execution(job_id)

    log.info(json.dumps({"jobId": job_id, "created": created, "op": "run"}))
    return {
        "success": True,
        "accepted": True,
        "jobId": job_id,
        "status": Status.QUEUED,
    }


def _ensure_execution(job_id):
    """Start the workflow, or reconcile an execution that already exists.

    The execution name is derived from the job ID, so a crash between job
    creation and StartExecution is recoverable: calling again either starts the
    one execution or gets ExecutionAlreadyExists back. Either way the job ends up
    with exactly one run, never two paid ones.
    """
    name = jobs.execution_name(job_id)
    try:
        response = _states().start_execution(
            stateMachineArn=conf.WORKFLOW_ARN,
            name=name,
            # `attempt` is seeded here so every downstream state can reference
            # $.attempt without a separate initialising Pass state.
            input=json.dumps({"jobId": job_id, "attempt": 1}),
        )
        arn = response["executionArn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ExecutionAlreadyExists":
            raise
        # Already running from an earlier attempt: adopt it rather than start a
        # second execution.
        arn = _existing_execution_arn(name)

    jobs.attach_execution(job_id, arn)
    return arn


def _existing_execution_arn(name):
    account = conf.WORKFLOW_ARN.split(":")[4]
    region = conf.WORKFLOW_ARN.split(":")[3]
    machine = conf.WORKFLOW_ARN.split(":")[-1]
    return f"arn:aws:states:{region}:{account}:execution:{machine}:{name}"


def get_layered_design_status(job_id, caller_context):
    """Read progress or the completed result.

    success:true means the lookup succeeded. A failed design job still returns
    success:true with status FAILED (SPEC section 2.6).
    """
    owner_scope = jobs.resolve_owner_scope(caller_context)
    if not job_id or not isinstance(job_id, str):
        raise InvalidRequest("Missing jobId.")

    item = jobs.get_job(job_id, owner_scope)
    status = item.get("status", Status.QUEUED)

    response = {
        "success": True,
        "jobId": job_id,
        "status": status,
        "updatedAt": item.get("updatedAt"),
    }

    if status == Status.FAILED:
        response["error"] = item.get("error") or {
            "stage": item.get("stage") or Stage.ACCEPT_REQUEST,
            "code": Code.INTERNAL,
            "message": "The job failed.",
            "retryable": False,
        }
        return response

    if status == Status.SUCCEEDED:
        result_key = item.get("resultKey")
        if result_key:
            response["result"] = artifacts.get_json(job_id, result_key)
        return response

    # Report the real stage rather than a fabricated percentage (SPEC 2.4).
    response["stage"] = item.get("stage")
    return response
