"""Structural tests for workflow.asl.json.

A dangling Next target or a stage name that does not match lib.errors.Stage only
shows up at deploy time, or worse, at the moment that branch is first taken.
These are cheap to catch here.
"""

import json
import pathlib

import pytest

import handler_stage
from lib.errors import Stage
from tests.fakes import FakeTable

ASL = json.loads((pathlib.Path(__file__).parent.parent / "workflow.asl.json").read_text())
STATES = ASL["States"]


def _walk(states):
    """Yield every state, descending into Parallel branches and Map iterators."""
    for name, state in states.items():
        yield name, state
        for branch in state.get("Branches", []):
            yield from _walk(branch["States"])
        if "Iterator" in state:
            yield from _walk(state["Iterator"]["States"])


def test_definition_is_valid_json_with_a_start_state():
    assert ASL["StartAt"] in STATES


def test_every_transition_target_exists():
    # Targets resolve within their own scope, so check each scope separately.
    def check(scope):
        for name, state in scope.items():
            targets = []
            if "Next" in state:
                targets.append(state["Next"])
            for choice in state.get("Choices", []):
                targets.append(choice["Next"])
            if "Default" in state:
                targets.append(state["Default"])
            for catch in state.get("Catch", []):
                targets.append(catch["Next"])
            for target in targets:
                assert target in scope, f"{name} -> {target} does not exist"
            for branch in state.get("Branches", []):
                check(branch["States"])
            if "Iterator" in state:
                check(state["Iterator"]["States"])

    check(STATES)


def test_every_task_invokes_the_stage_function():
    for name, state in _walk(STATES):
        if state.get("Type") == "Task":
            assert state["Resource"] == "${StageFunctionArn}", name


def test_all_fourteen_pipeline_stages_are_wired():
    # A Map declares the stage in its item selector rather than on the inner
    # Task, so look at every state that names one.
    used = {
        state["Parameters"]["stage"]
        for _, state in _walk(STATES)
        if "stage" in state.get("Parameters", {})
    }
    missing = set(Stage.PIPELINE) - used
    assert not missing, f"stages never invoked: {sorted(missing)}"


def test_stage_names_match_the_error_module():
    for name, state in _walk(STATES):
        params = state.get("Parameters", {})
        if "stage" in params:
            assert params["stage"] in Stage.ALL, f"{name} uses unknown stage"


def test_workflow_has_a_bounded_timeout():
    # SPEC section 6 bounds the workflow to 30 minutes initially.
    assert ASL["TimeoutSeconds"] == 1800


def test_independent_assets_is_a_bounded_map():
    m = STATES["IndependentAssets"]
    assert m["Type"] == "Map"
    assert m["MaxConcurrency"] == 2


def test_overlay_runs_before_the_photo_which_is_repainted_around_it():
    """CLEAN_PHOTO is masked by the overlay's shape, so the overlay comes first.

    The photo previously ran in parallel and came back zoomed and shifted against
    the concept; with the visible photograph locked by a mask it does not.
    """
    assert "SeparateLayers" not in STATES
    assert STATES["ExtractScene"]["Next"] == "DecorationOverlay"
    assert STATES["DecorationOverlay"]["Parameters"]["stage"] == "DECORATION_OVERLAY"
    assert STATES["DecorationOverlay"]["Next"] == "CleanPhoto"
    assert STATES["CleanPhoto"]["Parameters"]["stage"] == "CLEAN_PHOTO"
    assert STATES["CleanPhoto"]["Next"] == "IndependentAssets"


def test_a_repaired_overlay_gets_a_photo_repainted_around_it():
    """The photo's mask is the overlay's shape, so a repaired overlay needs a
    rebuilt photo before the layers are validated together."""
    assert STATES["RepairOverlay"]["Next"] == "RebuildPhoto"
    rebuild = STATES["RebuildPhoto"]
    assert rebuild["Parameters"]["operation"] == "REBUILD_PHOTO"
    assert rebuild["Parameters"]["stage"] == "CLEAN_PHOTO"
    assert rebuild["Next"] == "ValidateAssets"


def test_paid_image_stages_do_not_retry_on_unknown_outcome():
    """A retry after an unknown provider outcome is what pays twice."""
    for name in ("Concept", "DecorationOverlay", "CleanPhoto", "RebuildPhoto"):
        state = STATES[name]
        retries = state.get("Retry", [])
        for _, inner in _walk({name: state}):
            retries = retries + inner.get("Retry", [])
        for rule in retries:
            assert "ProviderOutcomeUnknown" not in rule["ErrorEquals"], name


def test_every_task_catches_failures_to_record_them():
    for name, state in STATES.items():
        if state.get("Type") in ("Task", "Parallel", "Map") and name != "RecordFailure":
            assert state.get("Catch"), f"{name} has no Catch"


def test_correction_loops_back_far_enough_to_change_the_design():
    """Re-entering at ValidateAssets would re-check identical assets and
    produce a byte-identical render, so the loop would burn both rounds
    achieving nothing."""
    assert STATES["Correct"]["Next"] == "NextAttempt"
    assert STATES["NextAttempt"]["Next"] == "ExtractScene"


def test_correction_advances_the_attempt_counter():
    """Artifact keys include the attempt and are immutable, so a second round
    reusing attempt 1 would fail on the conditional write."""
    assert (
        STATES["NextAttempt"]["Parameters"]["attempt.$"]
        == "$.correction.artifacts.nextAttempt"
    )


def test_stages_rerun_by_the_correction_loop_use_the_live_attempt():
    rerun = ("Concept", "ReviewConcept", "ExtractScene", "DecorationOverlay",
             "CleanPhoto", "RebuildPhoto", "IndependentAssets", "ValidateAssets",
             "Assemble", "Render", "Review", "Finalize")
    for name in rerun:
        params = STATES[name]["Parameters"]
        assert params.get("attempt.$") == "$.attempt", name


def test_stages_before_the_loop_stay_on_attempt_one():
    """Preflight, copy and reference analysis are never repeated. The concept now
    can be (a concept repair round); what keeps separation aligned is the approved
    concept frozen on the job, not the concept running only once."""
    for name in ("Preflight", "PrepareCopy", "AnalyzeReference"):
        assert STATES[name]["Parameters"]["attempt"] == 1


def test_map_iterates_the_ids_extract_scene_produced():
    assert (
        STATES["IndependentAssets"]["ItemsPath"]
        == "$.scene.artifacts.independentAssetIds"
    )


def test_unapproved_review_always_reaches_correct():
    """Correct is what raises CorrectionExhausted when no round is left, and its
    Catch is what supplies $.failure. Skipping it is what stranded a live job."""
    choice = STATES["ReviewPassed"]
    assert choice["Default"] == "Correct"
    assert [c["Next"] for c in choice["Choices"]] == ["Finalize", "CorrectConcept"]
    assert STATES["Correct"]["Catch"][0]["Next"] == "RecordFailure"


def test_record_failure_is_only_reached_through_a_catch():
    """RecordFailure reads $.failure, which only a Catch's ResultPath sets. An
    ordinary Next, a Choice branch or a Default into it ends the execution with
    States.Runtime and never records the failure, so the job stays RUNNING and
    a polling caller waits forever."""
    for name, state in STATES.items():
        direct = [state.get("Next"), state.get("Default")]
        direct += [c.get("Next") for c in state.get("Choices", [])]
        if name == "RecordFailure":
            continue
        assert "RecordFailure" not in direct, name
        for catch in state.get("Catch", []):
            if catch["Next"] == "RecordFailure":
                assert catch["ResultPath"] == "$.failure", name


def test_review_routes_a_concept_defect_to_concept_repair_after_approval():
    choices = STATES["ReviewPassed"]["Choices"]
    assert choices[1]["Variable"] == "$.review.artifacts.conceptDefect"
    assert choices[1]["BooleanEquals"] is True
    assert choices[1]["Next"] == "CorrectConcept"


def test_new_stages_sit_at_their_spec_positions():
    pipeline = list(Stage.PIPELINE)
    assert pipeline.index("ANALYZE_REFERENCE") == pipeline.index("PREPARE_COPY") + 1
    assert pipeline.index("CONCEPT") == pipeline.index("ANALYZE_REFERENCE") + 1
    assert pipeline.index("REVIEW_CONCEPT") == pipeline.index("CONCEPT") + 1
    assert pipeline.index("EXTRACT_SCENE") == pipeline.index("REVIEW_CONCEPT") + 1
    assert STATES["PrepareCopy"]["Next"] == "AnalyzeReference"
    assert STATES["AnalyzeReference"]["Next"] == "Concept"
    assert STATES["Concept"]["Next"] == "ReviewConcept"
    assert STATES["ReviewConcept"]["Next"] == "ConceptApproved"


def test_unapproved_concept_always_reaches_correct_concept():
    """ConceptApproved must not route to RecordFailure: CorrectConcept raises
    ReferenceAdherenceFailed when no round is left and its Catch supplies
    $.failure. A direct edge would strand the job as RUNNING."""
    choice = STATES["ConceptApproved"]
    assert choice["Default"] == "CorrectConcept"
    assert [c["Next"] for c in choice["Choices"]] == ["ExtractScene", "SelectConcept"]
    assert choice["Choices"][0]["Variable"] == "$.conceptReview.artifacts.approved"
    assert STATES["CorrectConcept"]["Catch"][0]["Next"] == "RecordFailure"
    assert STATES["CorrectConcept"]["Parameters"]["operation"] == "CORRECT_CONCEPT"


def test_concept_repair_regenerates_the_concept_at_a_new_attempt():
    assert STATES["CorrectConcept"]["Next"] == "NextConceptAttempt"
    nxt = STATES["NextConceptAttempt"]
    assert nxt["Next"] == "Concept"
    assert nxt["Parameters"]["attempt.$"] == "$.conceptCorrection.artifacts.nextAttempt"
    # Narrowed on purpose: drops the stale scene, assets, render and review.
    assert set(nxt["Parameters"]) == {"jobId.$", "attempt.$"}


def test_review_choice_requires_backend_approval_flag():
    choice = STATES["ReviewPassed"]
    first = choice["Choices"][0]
    assert first["Variable"] == "$.review.artifacts.approved"
    assert first["BooleanEquals"] is True
    assert first["Next"] == "Finalize"


# --- stage dispatch --------------------------------------------------------


def test_unimplemented_stage_raises_rather_than_faking_success(monkeypatch):
    """Invariant 1, enforced rather than trusted.

    All twelve stages are implemented, so this removes one to exercise the
    mechanism: an unregistered stage must raise, never return a plausible empty
    result that lets the pipeline continue.
    """
    from lib.errors import NotImplementedStage

    registry = dict(handler_stage._registry())
    registry.pop("CONCEPT")
    monkeypatch.setattr(handler_stage, "STAGE_HANDLERS", registry)

    with pytest.raises(NotImplementedStage) as exc:
        handler_stage.handler(
            {"jobId": "job-1", "stage": "CONCEPT", "attempt": 1}, None
        )
    assert exc.value.stage == "CONCEPT"
    assert exc.value.code == "NOT_IMPLEMENTED"


def test_unknown_stage_rejected():
    from lib.errors import LayeredDesignError

    with pytest.raises(LayeredDesignError):
        handler_stage.handler({"jobId": "j", "stage": "NOT_A_STAGE"}, None)


def test_record_failure_persists_the_real_stage_and_code(monkeypatch):
    from lib import jobs

    table = FakeTable()
    table.items["JOB#job-1"] = {"pk": "JOB#job-1"}
    monkeypatch.setattr(jobs, "_tbl", lambda: table)

    handler_stage.handler(
        {
            "jobId": "job-1",
            "recordFailure": True,
            "failure": {
                "Error": "NotImplementedStage",
                "Cause": json.dumps(
                    {"errorMessage": "Stage CONCEPT is not implemented yet."}
                ),
            },
        },
        None,
    )
    stored = table.items["JOB#job-1"]
    assert stored["status"] == "FAILED"
    assert stored["error"]["code"] == "NOT_IMPLEMENTED"
    assert stored["error"]["stage"] == "CONCEPT"


def _round_trip_failure(monkeypatch, raiser, stage):
    """Run a stage that fails, then record the failure the way the workflow does.

    Step Functions only carries the exception class name and message across the
    hop, so this reproduces that narrowing instead of passing the live exception.
    """
    from lib import jobs
    from lib.errors import LayeredDesignError

    table = FakeTable()
    table.items["JOB#job-x"] = {"pk": "JOB#job-x"}
    monkeypatch.setattr(jobs, "_tbl", lambda: table)
    monkeypatch.setattr(handler_stage, "_registry", lambda: {stage: raiser})

    with pytest.raises(LayeredDesignError) as caught:
        handler_stage.handler(
            {"jobId": "job-x", "stage": stage, "attempt": 1}, None
        )

    handler_stage.handler(
        {
            "jobId": "job-x",
            "recordFailure": True,
            "failure": {
                "Error": type(caught.value).__name__,
                "Cause": json.dumps({"errorMessage": str(caught.value)}),
            },
        },
        None,
    )
    return table.items["JOB#job-x"]["error"]


def test_an_unexpected_stage_error_is_reported_against_that_stage(monkeypatch):
    """A crash in PREFLIGHT must not be reported as a FINALIZE failure.

    The sanitized message deliberately carries no provider detail, so if it also
    carries no stage the recorded error defaults to the last stage and sends
    whoever is debugging to the wrong end of the pipeline.
    """

    def boom(**kwargs):
        raise KeyError("requestKey")

    error = _round_trip_failure(monkeypatch, boom, "PREFLIGHT")
    assert error["stage"] == "PREFLIGHT"
    assert error["code"] == "INTERNAL"


def test_a_structured_stage_error_keeps_its_stage(monkeypatch):
    from lib.errors import Code, RenderFailed

    def boom(**kwargs):
        raise RenderFailed("The headline overflows its box.")

    error = _round_trip_failure(monkeypatch, boom, "RENDER")
    assert error["stage"] == "RENDER"
    assert error["code"] == Code.RENDER_FAILED
    assert "headline overflows" in error["message"]


def test_a_retryable_failure_is_recorded_as_retryable(monkeypatch):
    """The flag is computed in the stage but only the class name crosses the hop.

    Recording every failure as non-retryable tells a caller not to try again when
    the service itself classified the failure as transient.
    """
    from lib.errors import ProviderRateLimited

    def boom(**kwargs):
        raise ProviderRateLimited("The image provider is rate limiting.")

    error = _round_trip_failure(monkeypatch, boom, "CONCEPT")
    assert error["retryable"] is True
    assert error["code"] == "PROVIDER_RATE_LIMITED"


def test_every_error_class_is_recoverable_by_name(monkeypatch):
    """A class missing from the lookup is reported as a generic internal error."""
    from lib import errors

    discovered = handler_stage._error_classes()
    for name, cls in vars(errors).items():
        if isinstance(cls, type) and issubclass(cls, errors.LayeredDesignError):
            assert discovered.get(name) is cls, f"{name} is not recoverable"


def test_a_non_retryable_failure_stays_non_retryable(monkeypatch):
    from lib.errors import InvalidScene

    def boom(**kwargs):
        raise InvalidScene("The scene is malformed.")

    error = _round_trip_failure(monkeypatch, boom, "EXTRACT_SCENE")
    assert error["retryable"] is False


def test_a_repairable_overlay_routes_to_the_repair_rather_than_failing():
    """SPEC section 4 requires bounded targeted overlay repairs.

    The counter and the retryable flag existed long before this edge did, so the
    repair was being accounted for and never performed.
    """
    catches = STATES["ValidateAssets"]["Catch"]
    assert catches[0]["ErrorEquals"] == ["OverlayRepairNeeded"]
    assert catches[0]["Next"] == "RepairOverlay"
    # The blanket catch must stay last, or it would swallow the repair route.
    assert catches[-1]["ErrorEquals"] == ["States.ALL"]
    assert catches[-1]["Next"] == "RecordFailure"


def test_the_repair_returns_to_validation():
    """A repair nobody re-checks is just a second way to ship a broken overlay.

    The photo is rebuilt around the repaired overlay on the way back."""
    assert STATES["RepairOverlay"]["Next"] == "RebuildPhoto"
    assert STATES["RebuildPhoto"]["Next"] == "ValidateAssets"
    assert STATES["RepairOverlay"]["Parameters"]["operation"] == "REPAIR_OVERLAY"


def test_the_repair_carries_the_current_attempt():
    assert STATES["RepairOverlay"]["Parameters"]["attempt.$"] == "$.attempt"


def test_a_failed_repair_ends_the_job():
    assert STATES["RepairOverlay"]["Catch"][0]["Next"] == "RecordFailure"
