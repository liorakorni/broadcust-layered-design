"""What a job did and what it cost: time, attempts, image calls, caches and routes.

Counters live on the job record as a flat map, written with atomic adds, so stages
running in parallel (the independent-asset Map) cannot overwrite each other. The
naming is the whole schema:

    stageMs:<STAGE>       total milliseconds spent in the stage, across attempts
    stageRuns:<STAGE>     how many times the stage ran
    imageCalls            image-generation or image-edit calls, in total
    imageCalls:<STAGE>    ... by stage
    validationCacheHits / validationCacheMisses
    stageCacheHits        a paid stage reused its stored output
    assetUrlsReused       a published asset URL was reused (identical bytes)
    lightRepairs          repairs that made no image-generation call

The point of `imageCalls` is that "a text-only repair makes zero image calls" is a
number you can read off a finished job, not a claim.
"""

from __future__ import annotations

import datetime

from lib import jobs


def record_stage(job_id, stage, duration_ms):
    jobs.add_metric(job_id, f"stageMs:{stage}", int(duration_ms))
    jobs.add_metric(job_id, f"stageRuns:{stage}", 1)


def record_image_call(job_id, stage):
    """Count one image-generation or image-edit call (paid, even if it fails)."""
    jobs.add_metric(job_id, "imageCalls", 1)
    jobs.add_metric(job_id, f"imageCalls:{stage}", 1)


def record_stage_cache_hit(job_id, stage):
    jobs.add_metric(job_id, "stageCacheHits", 1)
    jobs.add_metric(job_id, f"stageCacheHits:{stage}", 1)


def image_calls(job):
    return int((job.get("metrics") or {}).get("imageCalls", 0))


def routes(job):
    """The recorded routing decision of every review, in attempt order."""
    found = job.get("routes") or {}
    return [{"attempt": int(k), **found[k]} for k in sorted(found, key=int)]


def summary(job):
    """The caller-facing record of what the job did and cost."""
    m = job.get("metrics") or {}
    stages = {}
    for name, value in m.items():
        if name.startswith("stageMs:"):
            stages.setdefault(name.split(":", 1)[1], {})["durationMs"] = int(value)
        elif name.startswith("stageRuns:"):
            stages.setdefault(name.split(":", 1)[1], {})["attempts"] = int(value)
    by_stage = {
        name.split(":", 1)[1]: int(value)
        for name, value in m.items()
        if name.startswith("imageCalls:")
    }
    return {
        "totalDurationMs": _elapsed_ms(job),
        "stages": stages,
        "imageGenerationCalls": int(m.get("imageCalls", 0)),
        "imageGenerationCallsByStage": by_stage,
        "cache": {
            "validationHits": int(m.get("validationCacheHits", 0)),
            "validationMisses": int(m.get("validationCacheMisses", 0)),
            "stageOutputHits": int(m.get("stageCacheHits", 0)),
            "assetUrlsReused": int(m.get("assetUrlsReused", 0)),
        },
        "lightRepairs": int(m.get("lightRepairs", 0)),
        "repairCounts": jobs.repair_counts(job),
        "routes": routes(job),
        "selectedCandidate": job.get("selectedDesign"),
    }


def _elapsed_ms(job):
    try:
        started = datetime.datetime.fromisoformat(job["createdAt"])
    except (KeyError, ValueError, TypeError):
        return None
    return int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() * 1000)
