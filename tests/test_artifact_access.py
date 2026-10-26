"""Absent, forbidden and required artifacts take three different paths.

S3 only answers NoSuchKey to a caller allowed to list the bucket. Without that
permission a missing key comes back as 403, so code that reads every failure as
"not there" turns a missing IAM grant into a silent fallback - which is exactly
how job 175103fd69c74612b21449cab518eb14 died while probing for a repair record
that genuinely did not exist.
"""

import pytest

from lib import artifacts, jobs
from lib.errors import ArtifactAccessDenied, ArtifactNotFound, AssetInvalid, Stage
from lib.stages import repair
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    pipeline, run_stage, skip_external_probes,
)


def test_a_missing_artifact_is_reported_as_not_found(pipeline):
    job_id = pipeline["jobId"]
    key = artifacts.artifact_key(job_id, Stage.REPAIR, 1, "repair.json")

    with pytest.raises(ArtifactNotFound) as raised:
        artifacts.get_bytes(job_id, key)
    assert key in raised.value.message
    # Still an AssetInvalid, so existing handlers keep working.
    assert isinstance(raised.value, AssetInvalid)
    assert artifacts.get_json_optional(job_id, key) is None


def test_a_denied_artifact_read_is_never_reported_as_missing(pipeline):
    job_id = pipeline["jobId"]
    key = artifacts.artifact_key(job_id, Stage.REPAIR, 1, "repair.json")
    pipeline["s3"].deny(artifacts.job_prefix(job_id))

    with pytest.raises(ArtifactAccessDenied) as raised:
        artifacts.get_bytes(job_id, key)
    assert "permission" in raised.value.message
    # Not an AssetInvalid: a permission fault must not be absorbed by a handler
    # that falls back on an absent optional artifact.
    assert not isinstance(raised.value, AssetInvalid)

    with pytest.raises(ArtifactAccessDenied):
        artifacts.get_json_optional(job_id, key)


def test_a_required_artifact_that_is_missing_still_fails(pipeline):
    """An optional-read helper exists; required reads are unchanged."""
    job_id = pipeline["jobId"]
    jobs.record_artifacts(job_id, Stage.ASSEMBLE, 1, {
        "html": artifacts.artifact_key(job_id, Stage.ASSEMBLE, 1, "design.html"),
        "scene": artifacts.artifact_key(job_id, Stage.ASSEMBLE, 1, "scene.json"),
    })
    with pytest.raises(ArtifactNotFound):
        run_stage(job_id, Stage.RENDER)


def test_the_repair_record_key_matches_what_the_repair_writes(pipeline):
    """The probe is for the key the repair itself writes.

    A missing permission does not prove the object was there, so the key is
    checked on its own: `_existing` must look exactly where the record is written.
    """
    job_id = pipeline["jobId"]
    key = artifacts.artifact_key(job_id, Stage.REPAIR, 7, "repair.json")
    assert repair._existing(job_id, key) is None

    artifacts.put_json(key, {"attempt": 7, "result": {"route": "light"}})
    assert repair._existing(job_id, key) == {"attempt": 7, "result": {"route": "light"}}
    assert key in pipeline["s3"].objects
