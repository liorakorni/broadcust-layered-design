"""Restricted diagnostics for image-provider calls (addendum section 1).

These prove what is RECORDED about a call - the bytes attached, the effective prompt,
the mask - not what the provider does with them. They exist because the fixture job
9281413450b748119609d627b61670f0 could not be diagnosed from stored data: nothing said
which images went to the provider or whether the stored output was the raw response.
"""

import hashlib
import json

from lib import artifacts, jobs, provider_trace
from lib.errors import Stage
from tests.test_failure_paths import through
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    pipeline,
    skip_external_probes,
)


def _traces(pipeline):
    prefix = artifacts.job_prefix(pipeline["jobId"])
    found = {}
    for key, entry in pipeline["s3"].objects.items():
        if "/provider-trace" in key and key.startswith(prefix):
            found[key.rsplit("/", 1)[1]] = json.loads(entry)
    return found


def test_each_decomposition_call_leaves_a_restricted_trace(pipeline):
    through(pipeline, Stage.CLEAN_PHOTO)
    traces = _traces(pipeline)
    assert set(traces) == {"provider-trace-decoration.json", "provider-trace-photo.json"}
    assert all(t["restricted"] is True for t in traces.values())
    assert jobs.get_job_unscoped(pipeline["jobId"])["metrics"]["providerTraces"] == 2


def test_the_trace_names_the_exact_bytes_attached_and_the_mask(pipeline):
    through(pipeline, Stage.CLEAN_PHOTO)
    job = jobs.get_job_unscoped(pipeline["jobId"])
    concept = artifacts.get_bytes(pipeline["jobId"], jobs.approved_concept(job)["key"])
    overlay_trace, photo_trace = (
        _traces(pipeline)[n] for n in ("provider-trace-decoration.json",
                                       "provider-trace-photo.json"))

    # The approved concept is the only image attached, in both calls.
    for trace in (overlay_trace, photo_trace):
        assert [i["role"] for i in trace["inputs"]] == ["approvedConcept"]
        assert trace["inputs"][0]["sha256"] == hashlib.sha256(concept).hexdigest()
        assert trace["inputs"][0]["width"] and trace["inputs"][0]["height"]
    # The overlay extraction is unmasked; the photograph is edited under a mask.
    assert overlay_trace["mask"] is None
    assert photo_trace["mask"]["sha256"] and photo_trace["mask"]["width"]


def test_the_trace_holds_the_effective_prompt_a_version_and_the_output_hash(pipeline):
    through(pipeline, Stage.CLEAN_PHOTO)
    job = jobs.get_job_unscoped(pipeline["jobId"])
    trace = _traces(pipeline)["provider-trace-decoration.json"]
    sent = next(c["prompt"] for c in pipeline["calls"]["image"]
                if c["operationId"].endswith(":DECORATION_OVERLAY:1"))

    assert trace["prompt"]["effective"] == sent
    assert trace["prompt"]["sha256"] == hashlib.sha256(sent.encode()).hexdigest()
    assert trace["prompt"]["version"] == provider_trace.prompt_version("decoration-overlay")
    stored = artifacts.get_bytes(
        pipeline["jobId"], job["artifacts"][Stage.DECORATION_OVERLAY]["decoration"])
    assert trace["output"]["storedSha256"] == hashlib.sha256(stored).hexdigest()
    assert trace["providerRequestId"] == "req_x"
    assert trace["processing"] == []


def test_a_secret_or_an_image_payload_cannot_be_recorded():
    trace = provider_trace._scrub({
        "notes": "see https://res.cloudinary.com/x/a.png?X-Amz-Signature=abcdef0123 now",
        "auth": "Bearer abcdefghijklmnop1234",
        "key": "sk-abcdefghijklmnopqrstuvwxyz",
        "image": "data:image/png;base64," + "QUJD" * 40,
        "blob": "A" * 400,
        "fine": "a normal sentence about a torn yellow border",
    })
    flat = json.dumps(trace)
    for leaked in ("X-Amz-Signature", "abcdefghijklmnop1234", "sk-abcdef", "QUJDQUJD"):
        assert leaked not in flat
    assert trace["fine"] == "a normal sentence about a torn yellow border"


def test_a_failing_trace_write_never_fails_the_stage(pipeline, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(artifacts, "put_json", broken)
    assert provider_trace.record("job-italy", Stage.CLEAN_PHOTO, 1, "x.json", {}) is None
