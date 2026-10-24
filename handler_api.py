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
from lib import artifacts, bedrock, jobs
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
    """Dispatch a Bedrock Action Group call or an explicit direct operation.

    SPEC section 7.7: both paths run the same validation, authorization and
    service logic. Only the envelope and the result projection differ.
    """
    # Decided before the try so error responses are enveloped too; a bare dict
    # in reply to an agent call is unreadable to the agent.
    from_agent = bedrock.is_bedrock_event(event)
    try:
        conf.validate(conf.API_REQUIRED)
        result = _dispatch_agent(event) if from_agent else _dispatch_direct(event)
    except LayeredDesignError as exc:
        result = {"success": False, "accepted": False, "error": exc.to_dict()}
    except Exception:
        log.exception("unhandled api error")
        result = {
            "success": False,
            "accepted": False,
            "error": as_error_dict(None, default_stage=Stage.ACCEPT_REQUEST),
        }
    return bedrock.envelope(event, result) if from_agent else result


def _dispatch_direct(event):
    """SPEC section 7 requires the operation to be explicit: guessing it from
    the shape of the payload would make a malformed start request look like a
    status lookup.

    `trustedCallerContext` is trusted only because IAM limits who may invoke
    this function directly; nothing here authenticates it.
    """
    if not isinstance(event, dict):
        raise InvalidRequest("Event must be a JSON object.")
    operation = event.get("operation")
    if operation == "runLayeredDesign":
        return run_layered_design(
            event.get("request"), event.get("trustedCallerContext")
        )
    if operation == "getLayeredDesignStatus":
        return get_layered_design_status(
            event.get("jobId"), event.get("trustedCallerContext")
        )
    if operation == "updateLayeredDesign":
        return update_layered_design(event.get("request"), event.get("trustedCallerContext"))
    if operation == "exportLayeredDesign":
        return export_layered_design(event.get("request"), event.get("trustedCallerContext"))
    raise InvalidRequest(
        "Missing or unknown operation; expected runLayeredDesign, "
        "getLayeredDesignStatus, updateLayeredDesign or exportLayeredDesign."
    )


def _dispatch_agent(event):
    function = event.get("function")
    params = bedrock.parameters(event)
    caller = bedrock.caller_context(event)
    if function == bedrock.RUN:
        payload = bedrock.parse_request_json(bedrock.required(params, "requestJson"))
        return run_layered_design(payload, caller)
    if function == bedrock.STATUS:
        response = get_layered_design_status(bedrock.required(params, "jobId"), caller)
        return bedrock.compact_result(response)
    raise InvalidRequest(f"Unsupported function {function!r}.")


def run_layered_design(payload, caller_context):
    owner_scope = jobs.resolve_owner_scope(caller_context)
    request = parse_start_request(payload)
    request_hash = canonical_request_hash(request)

    job_id, created = jobs.claim_idempotency(
        owner_scope, request.requestId, request_hash
    )

    # The idempotency claim commits before the job record exists, so a failure in
    # between would otherwise poison this requestId permanently: every retry sees
    # the claim, skips creation, and leaves a job with no requestKey that every
    # stage and status lookup then rejects. Finishing an unfinished creation here
    # makes acceptance self-healing, the same way _ensure_execution reconciles a
    # half-started execution.
    if created or not jobs.job_exists(job_id):
        # Persist the canonical request before anything can consume it, so every
        # stage reads identical bytes regardless of retries. Overwriting is safe
        # and only matters on the recovery path: the key is derived from the job
        # and the body is the canonical form of an already hash-matched request,
        # so any object already there is identical.
        request_key = artifacts.artifact_key(
            job_id, Stage.ACCEPT_REQUEST, 1, "request.json"
        )
        artifacts.put_json(
            request_key, request.model_dump(mode="json"), overwrite=True
        )
        jobs.create_job(
            job_id,
            owner_scope,
            request_hash,
            request_key,
            {"width": request.canvas.width, "height": request.canvas.height},
            design_freedom=request.designFreedom,
            review_strictness=request.reviewStrictness,
            flow=request.designFlow,
        )

    _ensure_execution(job_id, request.designFlow)

    log.info(json.dumps({"jobId": job_id, "created": created, "op": "run"}))
    return {
        "success": True,
        "accepted": True,
        "jobId": job_id,
        "status": Status.QUEUED,
    }


def update_layered_design(payload, caller_context):
    """Apply one text/typography/layout edit. No image call, no render.

    See lib/design_edit.py for the contract (expectedVersion, requestId).
    """
    from lib import design_edit

    owner_scope = jobs.resolve_owner_scope(caller_context)
    return design_edit.update(payload, owner_scope)


def export_layered_design(payload, caller_context):
    """Explicit save/export: server-render the current design and return its preview."""
    import boto3

    from lib import design_edit

    owner_scope = jobs.resolve_owner_scope(caller_context)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobId"), str):
        raise InvalidRequest("jobId is required.")
    job = jobs.get_job(payload["jobId"], owner_scope)  # ownership + existence
    if job.get("status") != "SUCCEEDED":
        raise design_edit.DesignNotEditable("Only a finished design can be exported.")
    response = boto3.client("lambda", region_name=conf.AWS_REGION).invoke(
        FunctionName=conf.STAGE_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "operation": "EXPORT_DESIGN", "jobId": payload["jobId"],
            "version": payload.get("expectedVersion"), "ownerScope": owner_scope,
        }).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        message = (body.get("errorMessage") if isinstance(body, dict) else None) or "Export failed."
        kind = (body.get("errorType") if isinstance(body, dict) else None) or ""
        if kind == "StaleDesignVersion":
            raise design_edit.StaleDesignVersion(message)
        raise LayeredDesignError(message, stage=Stage.RENDER, code=Code.RENDER_FAILED)
    return {"success": True, **body["artifacts"]}


def _ensure_execution(job_id, flow="legacy"):
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
            input=json.dumps({"jobId": job_id, "attempt": 1, "flow": flow}),
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
