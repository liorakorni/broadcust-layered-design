"""Reuse a paid stage's stored output when its inputs are exactly the same.

A retried task, or a loop that comes back to a stage with nothing changed, should
resume from the stored result instead of paying for it again. The key carries every
input the output depends on - the bytes read, the prompt text, the model and the
validation-policy version - so a changed input can never be answered with an old
output, and an unchanged one never costs a second generation.

This is also what makes retries idempotent against the append-only artifact store:
a re-run that finds its own earlier output records the pointer instead of trying to
write the same immutable key again.
"""

from __future__ import annotations

from lib import jobs, telemetry
from lib.dependencies import cache_key, digest


def key(stage, **inputs):
    return cache_key(f"stage:{stage}", **inputs)


def lookup(job, stage, cache_id):
    """The stored pointers for this exact input, or None."""
    return jobs.cache_get(job, "stageCache", cache_id)


def remember(job_id, cache_id, pointers):
    jobs.cache_put(job_id, "stageCache", cache_id, pointers)


def hit(job_id, stage):
    telemetry.record_stage_cache_hit(job_id, stage)


def prompt_digest(prompt):
    return digest(prompt)
