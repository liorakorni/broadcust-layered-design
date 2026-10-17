"""Every pipeline stage is implemented and importable.

Cheap, but it is the test that would have caught a stage wired into the workflow
but never written - which fails at runtime, mid-job, after paid work.
"""

import inspect

import handler_stage
from lib.errors import Stage
from lib.stages import build_registry


def test_all_twelve_stages_are_registered():
    registry = build_registry()
    missing = set(Stage.PIPELINE) - set(registry)
    assert not missing, f"unimplemented stages: {sorted(missing)}"


def test_registry_has_no_stages_outside_the_pipeline():
    assert not set(build_registry()) - set(Stage.ALL)


def test_every_stage_accepts_the_frozen_invocation_envelope():
    """docs/CONTRACTS.md section 1 freezes {jobId, stage, attempt, assetId}."""
    for stage, fn in build_registry().items():
        params = inspect.signature(fn).parameters
        assert "job_id" in params, stage
        assert "attempt" in params, stage
        assert "asset_id" in params, f"{stage} cannot be dispatched by the Map"


def test_handler_loads_the_registry_lazily():
    handler_stage.STAGE_HANDLERS.clear()
    assert set(handler_stage._registry()) == set(Stage.PIPELINE)
