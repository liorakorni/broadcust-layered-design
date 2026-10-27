"""Durable milestones: the first rendered HTML (a draft), and the timing of the last.

FIRST_HTML_RENDERED is written once per job, after the first HTML has been stored AND
rendered, by a conditional write (`attribute_not_exists(firstHtmlRenderedAt)`), so two
racing or retried writers cannot move it and a later, better candidate never overwrites
it. If the checkpoint is interrupted (the render finished and the process died before
the write), `ensure_first_html` writes it on the next opportunity from the artifacts
that exist, and says that it did.

It is a DRAFT: exposed through the existing status polling together with its review
state, never as the final result and never as publish-ready. The draft carries the HTML
inline (its assets are already published by ASSEMBLE) and short-lived links to the stored
HTML and preview, made when the status is read and never stored (artifacts.link).

The final timing is about the candidate that was actually delivered:
`finalHtmlRenderedAt` is when THAT candidate was rendered (recorded in its RENDER
pointers), not when the job ended; `completedAt` and `totalDurationMs` are when it ended.
A job that rendered nothing reports nulls, not a guessed time.
"""

from __future__ import annotations

import datetime
import logging

from botocore.exceptions import ClientError

from lib import artifacts, jobs
from lib.errors import Stage

log = logging.getLogger("layered-design.milestones")

MILESTONE = "FIRST_HTML_RENDERED"

#: The persisted fields, so a consumer and a test read one list.
FIELDS = (
    "firstHtmlRenderedAt", "timeToFirstHtmlMs", "firstCandidateId", "firstHtmlKey",
    "firstHtmlHash", "firstPreviewKey", "firstAttempt", "firstHtmlRecovered",
)


def candidate_id(attempt):
    """The id of an attempt's candidate: it names designCandidates[attempt]."""
    return f"attempt-{int(attempt)}"


def _parse(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _elapsed_ms(job, at):
    started, ended = _parse(job.get("createdAt")), _parse(at)
    if started is None or ended is None:
        return None
    return max(0, int((ended - started).total_seconds() * 1000))


def record_first_html(job_id, attempt, *, rendered_at=None, recovered=False):
    """Write the milestone if it has not been written. Returns the stored fields.

    Never raises: a draft that could not be recorded must not fail the render that
    produced a usable design. Idempotent: a second call returns what the first stored.
    """
    try:
        job = jobs.get_job_unscoped(job_id)
        if not jobs.is_v2(job):
            return {}                      # a job accepted before the milestone existed
        if job.get("firstHtmlRenderedAt"):
            return {k: job.get(k) for k in FIELDS if k in job}
        produced = job.get("artifacts") or {}
        assembled = produced.get(Stage.ASSEMBLE) or {}
        rendered = produced.get(Stage.RENDER) or {}
        if not assembled.get("html") or not rendered.get("native"):
            return {}                      # nothing rendered yet: there is no milestone
        at = rendered_at or rendered.get("renderedAt") or jobs._now()
        fields = {
            "firstHtmlRenderedAt": at,
            "timeToFirstHtmlMs": _elapsed_ms(job, at),
            "firstCandidateId": candidate_id(attempt),
            "firstAttempt": int(attempt),
            "firstHtmlKey": assembled["html"],
            "firstHtmlHash": assembled.get("htmlHash"),
            "firstPreviewKey": rendered["native"],
            "firstHtmlRecovered": bool(recovered),
        }
        values = {f":v{i}": fields[name] for i, name in enumerate(fields)}
        try:
            jobs._update(
                Key={"pk": f"JOB#{job_id}"},
                UpdateExpression="SET " + ", ".join(
                    f"{name} = :v{i}" for i, name in enumerate(fields)) + ", updatedAt = :now",
                ConditionExpression="attribute_not_exists(firstHtmlRenderedAt)",
                ExpressionAttributeValues={**jobs.storable(values), ":now": jobs._now()},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return {}                      # another writer got there first: theirs stands
        jobs.add_metric(job_id, "firstHtmlRecorded")
        log.info("%s jobId=%s attempt=%s msSinceCreated=%s",
                 MILESTONE, job_id, attempt, fields["timeToFirstHtmlMs"])
        return fields
    except Exception:  # noqa: BLE001
        log.exception("could not record %s jobId=%s", MILESTONE, job_id)
        return {}


def ensure_first_html(job_id):
    """Recover a milestone whose write was interrupted, from the artifacts that exist.

    Called by stages that run after RENDER. The recovered time is when this ran, not when
    the render finished, and is marked so (`firstHtmlRecovered`).
    """
    job = jobs.get_job_unscoped(job_id)
    if job.get("firstHtmlRenderedAt"):
        return False
    attempt = (job.get("attempts") or {}).get(Stage.RENDER)
    if not attempt:
        return False
    return bool(record_first_html(job_id, int(attempt), recovered=True))


def final_timing(job, render_pointers):
    """Timing of the delivered candidate: all null when it was never rendered."""
    rendered_at = (render_pointers or {}).get("renderedAt")
    completed = jobs._now()
    return {
        "finalHtmlRenderedAt": rendered_at,
        "timeToFinalHtmlMs": _elapsed_ms(job, rendered_at) if rendered_at else None,
        "completedAt": completed,
        "totalDurationMs": _elapsed_ms(job, completed),
    }


def first_html_summary(job):
    """The milestone as the result and the status report it (no URLs, nothing expiring)."""
    if not job.get("firstHtmlRenderedAt"):
        return {"firstHtmlRenderedAt": None, "timeToFirstHtmlMs": None,
                "firstCandidateId": None}
    return {
        "firstHtmlRenderedAt": job["firstHtmlRenderedAt"],
        "timeToFirstHtmlMs": job.get("timeToFirstHtmlMs"),
        "firstCandidateId": job.get("firstCandidateId"),
        "firstHtmlRecovered": bool(job.get("firstHtmlRecovered")),
    }


def draft(job):
    """The first rendered HTML as a draft, or None when there is none yet.

    Read at status time. Links are made now and expire (artifacts.LINK_SECONDS); the HTML
    is returned inline because its assets are already published and the editor needs it.
    """
    if not job.get("firstHtmlRenderedAt") or not job.get("firstHtmlKey"):
        return None
    job_id = job["jobId"] if "jobId" in job else job["pk"].split("#", 1)[1]
    try:
        html = artifacts.get_bytes(job_id, job["firstHtmlKey"]).decode("utf-8")
    except Exception:  # noqa: BLE001 - the milestone stands even if its bytes cannot be read
        html = None
    attempt = int(job.get("firstAttempt") or 1)
    candidate = (job.get("designCandidates") or {}).get(str(attempt))
    return {
        "state": "draft",
        "candidateId": job.get("firstCandidateId"),
        "attempt": attempt,
        "renderedAt": job["firstHtmlRenderedAt"],
        "timeToFirstHtmlMs": job.get("timeToFirstHtmlMs"),
        "reviewState": _review_state(job, candidate),
        "final": False,
        "publishReady": False,
        "needsReview": True,
        "htmlHash": job.get("firstHtmlHash"),
        "html": html,
        "htmlUrl": artifacts.link(job_id, job["firstHtmlKey"]),
        "previewUrl": artifacts.link(job_id, job["firstPreviewKey"])
        if job.get("firstPreviewKey") else None,
        "linkExpiresInSeconds": artifacts.LINK_SECONDS,
        "note": "A draft: it has not been reviewed to completion and may still change. "
                "Use the final result when the status is SUCCEEDED or SUCCEEDED_WITH_WARNINGS.",
    }


def _review_state(job, candidate):
    if candidate is None:
        return "under-review" if job.get("stage") == Stage.REVIEW else "pending-review"
    if candidate.get("approved"):
        return "reviewed-approved"
    return "reviewed-repair-pending" if candidate.get("route") in ("light", "asset") \
        else "reviewed-findings-open"
