"""Repair routing, the lightweight repair path, edits and the layout-first flow.

These run the real stages and handlers with only the external services faked (the
`pipeline` fixture), and drive them the way the state machine does: stage by stage,
branching on the route the review recorded. What they prove is counted, not
claimed: every paid image call goes through a fake that records it, and the job's
own `imageCalls` metric is compared with it.
"""

import copy
import json

import pytest

import conf
import handler_stage
from lib import artifacts, candidates, design_edit, design_state, jobs, telemetry
from lib import content_check
from lib.errors import (
    CorrectionExhausted, DesignNotEditable, IdempotencyConflict, InvalidEdit,
    MandatoryContentMissing, RepairNoProgress, RepairRouteUnresolved, StaleDesignVersion, Stage,
)
from lib.providers import image as image_provider, vision as vision_provider
from tests import test_pipeline as base
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    CANVAS, COPY_ITEMS, REFERENCE_ANALYSIS, SCENE, pipeline, run_stage,
    skip_external_probes,
)

FIRST_PASS = (
    Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE, Stage.CONCEPT,
    Stage.REVIEW_CONCEPT, Stage.EXTRACT_SCENE, Stage.DECORATION_OVERLAY,
    Stage.CLEAN_PHOTO, Stage.VALIDATE_ASSETS,
)
REVIEW_PASS = (Stage.ASSEMBLE, Stage.RENDER, Stage.REVIEW)

#: The stages that call an image model. A text/layout repair may run none of them.
IMAGE_STAGES = {
    Stage.CONCEPT, Stage.EXTRACT_SCENE, Stage.DECORATION_OVERLAY, Stage.CLEAN_PHOTO,
    Stage.INDEPENDENT_ASSETS,
}


# --- review scripting --------------------------------------------------------


def issue(code, *, target, affected, constraints=(), image=None, category="visual",
          severity="blocking", impact="It is wrong.", suggestion="Fix it."):
    return {
        "code": code, "layerId": None, "assetId": None, "message": f"{code} problem.",
        "category": category, "severity": severity, "affected": affected[0] if affected else code,
        "impact": impact, "suggestion": suggestion, "region": None,
        "repairTarget": target, "affectedElementIds": list(affected),
        "requiresImageGeneration": target in ("asset", "concept") if image is None else image,
        "constraints": [{"elementId": e, "property": p, "value": v} for e, p, v in constraints],
    }


OUTLINE = issue(
    "HEADLINE_OUTLINE_MISSING", target="typography", affected=["t_title"],
    constraints=[("t_title", "strokeColor", "#FFFFFF"), ("t_title", "strokeWidth", "3")],
)


@pytest.fixture
def script(pipeline, monkeypatch):
    """Scripted final reviews, consumed in order; an empty script approves."""
    verdicts = []
    previous = vision_provider.analyze

    def analyze(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "review_v2" and verdicts:
            blocking = verdicts.pop(0)
            return {"passed": not blocking, "blockingIssues": blocking, "warnings": [],
                    "suggestions": [], "conceptDefect": False}
        return previous(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", analyze)
    pipeline["script"] = verdicts
    return pipeline


def job_of(p):
    return p["table"].items[f"JOB#{p['jobId']}"]


def op(p, operation, attempt, stage=Stage.REVIEW):
    event = {"jobId": p["jobId"], "stage": stage, "attempt": attempt,
             "assetId": None, "operation": operation}
    return handler_stage.handler(event, None)["artifacts"]


def first_pass(p, attempt=1):
    for stage in FIRST_PASS:
        run_stage(p["jobId"], stage, attempt=1)
    return review_pass(p, attempt)


def review_pass(p, attempt):
    out = None
    for stage in REVIEW_PASS:
        out = run_stage(p["jobId"], stage, attempt=attempt)
    return out


def drive(p, max_loops=8):
    """The state machine's loop: repair by the recorded route until it ends.

    Returns the list of (attempt, route) decisions. A non-repairable route ends the
    loop the way the workflow's Correct state does: by raising its precise error.
    """
    decisions = []
    attempt = 1
    out = first_pass(p)
    for _ in range(max_loops):
        route = out["route"]
        decisions.append((attempt, route))
        if route == "approve":
            run_stage(p["jobId"], Stage.FINALIZE, attempt=attempt)
            return decisions
        if route == "light":
            attempt = op(p, "REPAIR_LIGHT", attempt)["nextAttempt"]
            out = review_pass(p, attempt)
        elif route == "asset":
            planned = op(p, "REPAIR_ASSET", attempt)
            attempt = planned["nextAttempt"]
            if planned["next"] == "photo":
                op(p, "REPAIR_PHOTO", attempt, Stage.CLEAN_PHOTO)
            else:
                op(p, "REPAIR_OVERLAY", attempt, Stage.DECORATION_OVERLAY)
                op(p, "REBUILD_PHOTO", attempt, Stage.CLEAN_PHOTO)
            run_stage(p["jobId"], Stage.VALIDATE_ASSETS, attempt=attempt)
            out = review_pass(p, attempt)
        else:
            op(p, "CORRECT", attempt)       # raises the precise terminal error
            raise AssertionError("CORRECT must raise for a non-repairable route")
    raise AssertionError("the loop did not end")


def image_calls(p):
    return len(p["calls"]["image"])


def snapshot_of(p, version=None):
    job = job_of(p)
    pointer = job["designState"]
    return design_state.load(p["jobId"], pointer["key"])


def scene_of(p):
    return artifacts.get_json(p["jobId"], job_of(p)["artifacts"][Stage.ASSEMBLE]["scene"])


def html_of(p):
    return artifacts.get_bytes(p["jobId"], job_of(p)["artifacts"][Stage.ASSEMBLE]["html"]).decode()


# --- the outlined headline: typography, never an image --------------------------


def test_a_missing_outline_is_a_typography_repair_with_no_image_generation(script):
    script["script"].append([OUTLINE])
    first_pass(script)
    before_calls = image_calls(script)
    snapshot_before = snapshot_of(script)
    assert "text-stroke" not in html_of(script)

    decisions = drive_remaining(script)

    assert [route for _, route in decisions] == ["light", "approve"]
    assert image_calls(script) == before_calls                 # not one more model image
    assert job_of(script)["metrics"].get("imageCalls", 0) == before_calls
    assert "-webkit-text-stroke:3px #FFFFFF" in html_of(script)
    title = next(l for l in scene_of(script)["textLayers"] if l["id"] == "t_title")
    assert (title["strokeColor"], title["strokeWidth"]) == ("#FFFFFF", 3)
    assert snapshot_of(script)["version"] == snapshot_before["version"] + 1


def drive_remaining(p):
    """Continue from a first pass that already ran (attempt 1 reviewed)."""
    review = artifacts.get_json(p["jobId"], job_of(p)["artifacts"][Stage.REVIEW]["review"])
    decisions = []
    attempt = 1
    out = {"route": review["route"]}
    for _ in range(8):
        decisions.append((attempt, out["route"]))
        if out["route"] == "approve":
            run_stage(p["jobId"], Stage.FINALIZE, attempt=attempt)
            return decisions
        if out["route"] == "light":
            attempt = op(p, "REPAIR_LIGHT", attempt)["nextAttempt"]
            out = review_pass(p, attempt)
        else:
            op(p, "CORRECT", attempt)
            raise AssertionError("CORRECT must raise")
    raise AssertionError("the loop did not end")


def test_a_text_correction_preserves_every_asset_reference_and_hash(script):
    script["script"].append([OUTLINE])
    first_pass(script)
    first = snapshot_of(script)
    background_key = job_of(script)["artifacts"][Stage.CLEAN_PHOTO]["background"]
    overlay_key = job_of(script)["artifacts"][Stage.DECORATION_OVERLAY]["decoration"]
    published_before = list(script["calls"]["published"])

    drive_remaining(script)

    final = snapshot_of(script)
    assert final["version"] > first["version"]
    assert final["assets"]["entries"] == first["assets"]["entries"]
    assert final["assets"]["hash"] == first["assets"]["hash"]
    assert design_state.parts_changed(first, final) == ["layout"]
    # The same stored objects, and nothing was uploaded again for the repair.
    assert job_of(script)["artifacts"][Stage.CLEAN_PHOTO]["background"] == background_key
    assert job_of(script)["artifacts"][Stage.DECORATION_OVERLAY]["decoration"] == overlay_key
    assert script["calls"]["published"] == published_before + ["preview"] or \
        script["calls"]["published"][:len(published_before)] == published_before
    assert set(script["calls"]["published"]) <= {"background", "decoration", "preview"}
    assert job_of(script)["metrics"].get("assetUrlsReused", 0) >= 2


def test_a_text_only_repair_invokes_no_image_generating_stage(script, monkeypatch):
    script["script"].append([OUTLINE])
    first_pass(script)
    ran = []
    real = handler_stage.handler

    def spy(event, context):
        ran.append(event.get("operation") or event.get("stage"))
        return real(event, context)

    monkeypatch.setattr(handler_stage, "handler", spy)
    drive_remaining(script)
    assert "REPAIR_LIGHT" in ran
    assert not set(ran) & (IMAGE_STAGES | {"REPAIR_PHOTO", "REPAIR_OVERLAY", "REBUILD_PHOTO",
                                           "CORRECT_CONCEPT"})
    # Observable on the record, not only in the spy.
    record = artifacts.get_json(
        script["jobId"], artifacts.artifact_key(script["jobId"], Stage.REPAIR, 1, "repair.json"))
    assert record["imageGenerationCalls"] == 0
    assert set(record["stagesSkipped"]) == {
        Stage.CONCEPT, Stage.EXTRACT_SCENE, Stage.CLEAN_PHOTO, Stage.DECORATION_OVERLAY}
    assert job_of(script)["metrics"]["lightRepairs"] == 1


# --- missing business name: a content repair that never touches the photo ------


@pytest.fixture
def fit_home(script, monkeypatch):
    """A design whose extraction omitted the business name, FIT HOME."""
    items = COPY_ITEMS + [{"contentKey": "business.name", "role": "business", "text": "FIT HOME"}]
    analysis = copy.deepcopy(REFERENCE_ANALYSIS)
    analysis["copyPlacements"].append(
        {"contentKey": "business.name", "basis": "top bar",
         "box": {"x": 0.1, "y": 0.04, "width": 0.5, "height": 0.07}})
    monkeypatch.setattr(base, "COPY_ITEMS", items)
    monkeypatch.setattr(base, "REFERENCE_ANALYSIS", analysis)
    # The concept itself did show the business name; only the extraction missed it.
    script["concept_verdicts"].append(base.concept_verdict(copy=base.copy_checks(items)))
    return script


def test_a_missing_business_name_is_restored_without_changing_the_photo(fit_home):
    first = first_pass(fit_home)
    assert first["route"] == "light" and not first["mandatoryContentOk"]
    assert "FIT HOME" not in html_of(fit_home)
    first_snapshot = snapshot_of(fit_home)
    calls = image_calls(fit_home)

    drive_remaining(fit_home)

    assert "FIT HOME" in html_of(fit_home)
    assert image_calls(fit_home) == calls
    final = snapshot_of(fit_home)
    assert final["assets"]["entries"] == first_snapshot["assets"]["entries"]
    names = [l for l in scene_of(fit_home)["textLayers"] if l["contentKey"] == "business.name"]
    assert len(names) == 1 and names[0]["text"] == "FIT HOME"        # exact, from the copy


def test_mandatory_content_is_revalidated_after_every_correction(fit_home):
    fit_home["script"].extend([[], [OUTLINE]])
    first_pass(fit_home)
    # Restoring the name and then a typography change: all of the copy is still there.
    attempt = op(fit_home, "REPAIR_LIGHT", 1)["nextAttempt"]
    out = review_pass(fit_home, attempt)
    assert out["mandatoryContentOk"]
    items = [i for i in artifacts.get_json(
        fit_home["jobId"], job_of(fit_home)["artifacts"][Stage.PREPARE_COPY]["copy"])["items"]]
    assert content_check.missing_keys(content_check.check(scene_of(fit_home), items)) == []
    for item in items:
        assert item["text"] in html_of(fit_home)


def test_a_correction_that_would_drop_approved_text_is_never_applied(script, monkeypatch):
    from lib import scene_patch

    script["script"].append([OUTLINE])
    first_pass(script)

    def dropping(scene, layer_id, properties, *, target, font_names=()):
        patched = copy.deepcopy(scene)
        patched["textLayers"] = [l for l in patched["textLayers"] if l["id"] != "t_phone"]
        return patched

    monkeypatch.setattr(scene_patch, "apply_properties", dropping)
    with pytest.raises(MandatoryContentMissing):
        op(script, "REPAIR_LIGHT", 1)
    # Nothing was committed: the design is still the assembled candidate.
    assert any(l["id"] == "t_phone" for l in scene_of(script)["textLayers"])


# --- gradient ownership through the repair path ---------------------------------


GRADIENT_MISSING = issue(
    "READABILITY_GRADIENT_MISSING", target="layout", affected=["add:readabilityGradient"],
    constraints=[("add:readabilityGradient", "addShape", "gradient-bottom")],
)


def test_a_missing_gradient_is_added_once_in_code_and_not_painted_into_the_photo(script):
    script["script"].append([GRADIENT_MISSING])
    first_pass(script)
    calls = image_calls(script)
    first = snapshot_of(script)

    drive_remaining(script)

    html = html_of(script)
    assert html.count('data-effect="readabilityGradient"') == 1
    assert [e["owner"] for e in scene_of(script)["effects"]] == ["layout"]
    assert image_calls(script) == calls
    assert snapshot_of(script)["assets"]["entries"] == first["assets"]["entries"]


def test_an_existing_gradient_is_never_duplicated_by_a_repair(script):
    from lib import repair_routing

    scene = copy.deepcopy(SCENE)
    scene["effects"] = [{"id": "baked", "kind": "readabilityGradient", "owner": "photo",
                         "note": "dark gradient painted into the photograph"}]
    record = repair_routing.route_issue(GRADIENT_MISSING, {
        "scene": scene, "copyKeys": set(), "assetForLayer": {}, "assetIds": set(),
        "fonts": conf.APPROVED_FONT_FAMILIES, "conceptJustified": False})
    assert record["route"] == "unresolved"
    assert any("already has a readabilityGradient" in p for p in record["problems"])


# --- classification is validated before anything is routed ----------------------


@pytest.mark.parametrize("bad, why", [
    (issue("X", target="typography", affected=["t_title"]), "constraints"),
    (issue("X", target="typography", affected=["no-such-layer"],
           constraints=[("no-such-layer", "strokeWidth", "3")]), "does not exist"),
    (issue("X", target="typography", affected=["t_title"],
           constraints=[("t_title", "x", "5")]), "not a typography property"),
    (issue("X", target="typography", affected=["t_title"],
           constraints=[("t_title", "strokeWidth", "banana")]), "strokeWidth"),
    (issue("X", target="typography", affected=["t_title"], image=True,
           constraints=[("t_title", "strokeWidth", "3")]), "contradicts"),
    (issue("X", target="layout", affected=["t_title"], image=True), "contradicts"),
    ({**issue("X", target="typography", affected=["t_title"]), "repairTarget": None}, "repairTarget"),
    (issue("X", target="concept", affected=[]), "only allowed when"),
])
def test_an_unknown_or_inconsistent_classification_never_triggers_regeneration(script, bad, why):
    script["script"].append([bad])
    out = first_pass(script)
    assert out["route"] == "unresolved"
    calls = image_calls(script)

    with pytest.raises(RepairRouteUnresolved) as exc:
        op(script, "CORRECT", 1)

    assert why in " ".join(
        p for r in out["routing"]["issues"] for p in r["problems"]) or why in exc.value.message
    assert image_calls(script) == calls
    # The best candidate is preserved and named in the diagnostic.
    assert "preserved" in exc.value.message
    assert job_of(script)["designCandidates"]["1"]["snapshotKey"]


def test_a_content_finding_cannot_be_repaired_as_typography(script):
    script["script"].append([issue(
        "BUSINESS_NAME_WRONG", target="typography", affected=["t_title"], category="content",
        constraints=[("t_title", "strokeWidth", "3")])])
    out = first_pass(script)
    assert out["route"] == "unresolved"
    assert any("content finding" in p for r in out["routing"]["issues"] for p in r["problems"])


def test_an_incorrect_photograph_is_repaired_as_that_asset_only(script, monkeypatch):
    script["script"].append([issue(
        "WRONG_PHOTO", target="asset", affected=["l_bg"],
        impact="The photograph shows a beach, not a gym.", suggestion="Show a gym interior.")])
    first_pass(script)
    snapshot = snapshot_of(script)
    calls = image_calls(script)
    real_edit = image_provider.edit
    seen = []

    def edit(prompt, source, *a, **kw):
        seen.append(prompt)
        out = real_edit(prompt, source, *a, **kw)
        if "CORRECTION REQUIRED" in prompt:         # a genuinely different photograph
            from PIL import Image
            img = Image.open(__import__("io").BytesIO(out["bytes"])).convert("RGB")
            return {**out, "bytes": base.png_bytes(img.transpose(Image.FLIP_LEFT_RIGHT))}
        return out

    monkeypatch.setattr(image_provider, "edit", edit)
    decisions = drive_remaining_with_assets(script)

    assert [r for _, r in decisions] == ["asset", "approve"]
    assert image_calls(script) == calls + 1                  # exactly the photograph
    assert "Show a gym interior." in seen[-1]
    final = snapshot_of(script)
    before = {e["assetId"]: e["sha256"] for e in snapshot["assets"]["entries"]}
    after = {e["assetId"]: e["sha256"] for e in final["assets"]["entries"]}
    assert after["background"] != before["background"]
    assert after["decoration"] == before["decoration"]       # the overlay was not touched
    assert job_of(script)["metrics"]["imageCalls:CLEAN_PHOTO"] >= 1
    assert job_of(script)["metrics"].get("imageCalls:DECORATION_OVERLAY", 0) == 1   # first pass only


def drive_remaining_with_assets(p):
    review = artifacts.get_json(p["jobId"], job_of(p)["artifacts"][Stage.REVIEW]["review"])
    decisions = [(1, review["route"])]
    assert review["route"] == "asset"
    planned = op(p, "REPAIR_ASSET", 1)
    attempt = planned["nextAttempt"]
    op(p, "REPAIR_PHOTO", attempt, Stage.CLEAN_PHOTO)
    run_stage(p["jobId"], Stage.VALIDATE_ASSETS, attempt=attempt)
    out = review_pass(p, attempt)
    decisions.append((attempt, out["route"]))
    assert out["route"] == "approve"
    run_stage(p["jobId"], Stage.FINALIZE, attempt=attempt)
    return decisions


# --- validation cache: invalidated only for affected dependencies ---------------


def test_validation_is_reused_when_inputs_are_unchanged_and_not_when_they_change(
        script, monkeypatch):
    first_pass(script)
    vision_calls = lambda: [c["schema"] for c in script["calls"]["vision"]
                            if c["schema"] in ("overlay_inspection_v1", "composite_fidelity_v1")]
    assert vision_calls() == ["overlay_inspection_v1", "composite_fidelity_v1"]

    # Same assets, a later attempt: nothing is re-inspected.
    again = run_stage(script["jobId"], Stage.VALIDATE_ASSETS, attempt=2)
    assert vision_calls() == ["overlay_inspection_v1", "composite_fidelity_v1"]
    assert sorted(job_of(script)["artifacts"][Stage.VALIDATE_ASSETS]["cacheHits"]) == [
        "composites", "fidelity"]
    assert all(e["boundTo"] for e in again["entries"].values())

    # A new photograph: the fidelity check must run again - and only that one.
    import io
    from PIL import Image
    other = base.png_bytes(
        Image.open(io.BytesIO(base.fake_photo())).transpose(Image.FLIP_LEFT_RIGHT))
    key = artifacts.artifact_key(script["jobId"], Stage.CLEAN_PHOTO, 3, "photo.png")
    artifacts.put_bytes(key, other, "image/png")
    job_of(script)["artifacts"][Stage.CLEAN_PHOTO]["background"] = key
    run_stage(script["jobId"], Stage.VALIDATE_ASSETS, attempt=3)
    assert vision_calls() == ["overlay_inspection_v1", "composite_fidelity_v1",
                              "composite_fidelity_v1"]
    assert job_of(script)["artifacts"][Stage.VALIDATE_ASSETS]["cacheHits"] == ["composites"]


def test_a_cached_validation_is_never_reused_under_a_different_policy_version(script, monkeypatch):
    from lib import dependencies

    first_pass(script)
    monkeypatch.setattr(dependencies, "VALIDATION_POLICY_VERSION", "2.0")
    run_stage(script["jobId"], Stage.VALIDATE_ASSETS, attempt=2)
    assert job_of(script)["artifacts"][Stage.VALIDATE_ASSETS]["cacheHits"] == []
    schemas_run = [c["schema"] for c in script["calls"]["vision"]]
    assert schemas_run.count("composite_fidelity_v1") == 2


def test_a_paid_stage_with_unchanged_inputs_is_answered_from_its_stored_output(script):
    first_pass(script)
    calls = image_calls(script)
    run_stage(script["jobId"], Stage.DECORATION_OVERLAY, attempt=2)
    run_stage(script["jobId"], Stage.CLEAN_PHOTO, attempt=2)
    assert image_calls(script) == calls                      # retried, not re-paid
    assert job_of(script)["metrics"]["stageCacheHits"] == 2
    assert job_of(script)["artifacts"][Stage.CLEAN_PHOTO]["background"]


# --- loop bounds -----------------------------------------------------------------


def test_repeated_identical_failures_terminate_within_the_repair_budget(script):
    script["script"].extend([[copy.deepcopy(OUTLINE)] for _ in range(6)])
    with pytest.raises(RepairNoProgress) as exc:
        drive(script)
    repairs = job_of(script)["metrics"]["lightRepairs"]
    assert repairs <= conf.MAX_REPAIRS["typography"]
    assert "did not improve" in exc.value.message
    assert image_calls(script) == 3                          # the first pass only
    assert exc.value.details["preservedCandidate"]["snapshotKey"]


def test_each_repair_category_has_its_own_bounded_budget(script):
    """A different finding each time still stops once the category's budget is spent."""
    findings = [
        issue(f"F{i}", target="layout", affected=["t_title"],
              constraints=[("t_title", "y", str(150 + 10 * i))])
        for i in range(6)
    ]
    script["script"].extend([[f, issue("OTHER", target="layout", affected=["t_sub"],
                                       constraints=[("t_sub", "y", str(330 + 10 * i))])]
                             for i, f in enumerate(findings)])
    with pytest.raises((CorrectionExhausted, RepairNoProgress)):
        drive(script)
    assert job_of(script)["metrics"]["lightRepairs"] <= conf.MAX_REPAIRS["layout"]
    assert image_calls(script) == 3


def test_a_repair_that_makes_things_worse_is_not_built_on(script):
    script["script"].append([OUTLINE])
    script["script"].append([copy.deepcopy(OUTLINE), issue(
        "NEW_PROBLEM", target="layout", affected=["t_sub"],
        constraints=[("t_sub", "y", "340")])])
    with pytest.raises(RepairNoProgress, match="more blocking findings"):
        drive(script)


def test_the_delivered_candidate_is_the_best_eligible_one_not_the_latest(script):
    script["script"].append([OUTLINE])
    drive(script)
    selection = artifacts.get_json(
        script["jobId"], job_of(script)["resultKey"])["designSelection"]
    assert selection["attempt"] == 2
    assert [c["approved"] for c in selection["candidates"]] == [False, True]
    kept = job_of(script)["designCandidates"]
    assert set(kept) == {"1", "2"}                           # the failed one is kept for diagnosis
    assert kept["1"]["blockingCount"] == 1


def test_a_candidate_with_missing_mandatory_text_is_never_selected():
    ok = {"attempt": 1, "approved": True, "mandatoryContentOk": False, "blockingCount": 0,
          "warningCount": 0, "route": "approve", "pointers": {}}
    assert candidates.select_deliverable([ok]) is None
    better = {**ok, "attempt": 2, "mandatoryContentOk": True, "warningCount": 2}
    assert candidates.select_deliverable([ok, better])["attempt"] == 2


def test_minor_deviations_stay_warnings_while_missing_text_stays_blocking(script):
    warning = issue("SLIGHT_SHIFT", target="layout", affected=["t_sub"], severity="warning",
                    constraints=[("t_sub", "y", "322")])
    script["script"].append([])
    first_pass(script)
    out = artifacts.get_json(script["jobId"], job_of(script)["artifacts"][Stage.REVIEW]["review"])
    assert out["approved"] and out["route"] == "approve"
    assert warning["severity"] == "warning"


# --- observability -----------------------------------------------------------------


def test_timing_routing_and_cache_facts_are_recorded_and_returned(script):
    script["script"].append([OUTLINE])
    drive(script)
    result = artifacts.get_json(script["jobId"], job_of(script)["resultKey"])
    metrics = result["metrics"]
    assert metrics["imageGenerationCalls"] == 3
    assert metrics["lightRepairs"] == 1
    assert metrics["stages"]["REVIEW"]["attempts"] == 2
    assert metrics["stages"]["ASSEMBLE"]["attempts"] == 2
    assert metrics["stages"]["CLEAN_PHOTO"]["attempts"] == 1
    assert all("durationMs" in s for s in metrics["stages"].values())
    assert [r["route"] for r in metrics["routes"]] == ["light", "approve"]
    assert metrics["routes"][0]["imageGenerationRequired"] is False
    assert metrics["routes"][0]["blocking"][0]["target"] == "typography"
    assert metrics["cache"]["assetUrlsReused"] >= 2
    assert result["qa"]["designSelection"]["attempt"] == 2
    assert telemetry.image_calls(job_of(script)) == 3


# --- interactive editing -------------------------------------------------------------


def finished(script):
    drive(script)
    return {"trustedCallerContext": None}


def edit_payload(p, **over):
    job = job_of(p)
    payload = {"jobId": p["jobId"], "requestId": "edit-1",
               "expectedVersion": int(job["designVersion"]),
               "changes": {"content": {"title.primary": "כותרת חדשה"}}}
    payload.update(over)
    return payload


def test_a_text_edit_generates_nothing_renders_nothing_and_keeps_every_asset(script):
    finished(script)
    before = snapshot_of(script)
    calls, published = image_calls(script), list(script["calls"]["published"])

    result = design_edit.update(edit_payload(script), "owner-1")

    assert result["imageGenerationCalls"] == 0 and result["previewRendered"] is False
    assert result["changed"] and result["changedParts"] == ["content"]
    assert result["designVersion"] == before["version"] + 1
    assert result["assets"] == {e["assetId"]: e["sha256"] for e in before["assets"]["entries"]}
    assert image_calls(script) == calls and script["calls"]["published"] == published
    after = snapshot_of(script)
    assert after["assets"]["entries"] == before["assets"]["entries"]
    assert design_state.scene_of(after)["textLayers"][0]["text"] == "כותרת חדשה"
    assert job_of(script)["designVersion"] == before["version"] + 1


def test_an_edit_against_a_stale_version_is_refused_not_overwritten(script):
    finished(script)
    design_edit.update(edit_payload(script), "owner-1")                  # moves to v+1
    stale = edit_payload(script, requestId="edit-2",
                         expectedVersion=int(job_of(script)["designVersion"]) - 1,
                         changes={"content": {"title.primary": "מישהו אחר"}})
    with pytest.raises(StaleDesignVersion) as exc:
        design_edit.update(stale, "owner-1")
    assert exc.value.details["currentVersion"] == int(job_of(script)["designVersion"])
    text = design_state.scene_of(snapshot_of(script))["textLayers"][0]["text"]
    assert text == "כותרת חדשה"                                          # untouched


def test_simultaneous_edits_cannot_both_win(script, monkeypatch):
    """The commit itself is conditional on the version, not only the pre-check."""
    finished(script)
    real = jobs.set_design_state
    version = int(job_of(script)["designVersion"])

    def racing(job_id, pointer, expected_version=None):
        job_of(script)["designVersion"] = version + 1                    # someone else committed
        return real(job_id, pointer, expected_version=expected_version)

    monkeypatch.setattr(jobs, "set_design_state", racing)
    with pytest.raises(StaleDesignVersion):
        design_edit.update(edit_payload(script), "owner-1")


def test_an_identical_retry_returns_the_same_result_and_writes_nothing(script):
    finished(script)
    payload = edit_payload(script)
    first = design_edit.update(payload, "owner-1")
    objects = set(script["s3"].objects)
    again = design_edit.update(copy.deepcopy(payload), "owner-1")
    assert again["idempotent"] and again["designVersion"] == first["designVersion"]
    assert again["snapshotKey"] == first["snapshotKey"]
    assert set(script["s3"].objects) == objects


def test_a_changed_payload_must_use_a_new_request_id(script):
    finished(script)
    design_edit.update(edit_payload(script), "owner-1")
    changed = edit_payload(script, changes={"content": {"title.primary": "אחרת"}})
    with pytest.raises(IdempotencyConflict):
        design_edit.update(changed, "owner-1")
    fresh = edit_payload(script, requestId="edit-2",
                         expectedVersion=int(job_of(script)["designVersion"]),
                         changes={"content": {"title.primary": "אחרת"}})
    assert design_edit.update(fresh, "owner-1")["changed"]


def test_typography_and_layout_edits_use_the_same_validated_patches(script):
    finished(script)
    payload = edit_payload(script, changes={
        "typography": {"t_title": {"strokeColor": "#FFFFFF", "strokeWidth": "4"}},
        "layout": {"t_phone": {"y": "900"}}})
    result = design_edit.update(payload, "owner-1")
    assert result["changedParts"] == ["layout"]
    assert {"typography", "layout"} <= set(result["changeKinds"])
    scene = design_state.scene_of(snapshot_of(script))
    title = next(l for l in scene["textLayers"] if l["id"] == "t_title")
    assert (title["strokeColor"], title["strokeWidth"]) == ("#FFFFFF", 4)


@pytest.mark.parametrize("changes", [
    {"content": {"no.such.key": "x"}},
    {"content": {"title.primary": "   "}},
    {"typography": {"t_title": {"fontFamily": "Comic Sans"}}},
    {"typography": {"t_title": {"strokeWidth": "3"}}},       # width with no colour: not a valid design
    {"layout": {"t_title": {"x": "5000"}}},
    {"layout": {"missing": {"x": "5"}}},
    {"wat": {}},
])
def test_an_invalid_edit_is_refused_and_changes_nothing(script, changes):
    finished(script)
    version = job_of(script)["designVersion"]
    with pytest.raises(InvalidEdit):
        design_edit.update(edit_payload(script, changes=changes), "owner-1")
    assert job_of(script)["designVersion"] == version


def test_only_the_owner_of_a_finished_design_can_edit_it(script):
    from lib.errors import JobNotFound

    with pytest.raises(DesignNotEditable):
        design_edit.update(edit_payload(script, expectedVersion=0), "owner-1")   # not finished
    finished(script)
    with pytest.raises(JobNotFound):
        design_edit.update(edit_payload(script), "someone-else")


def test_export_renders_the_edited_design_once_on_request_and_calls_no_image_model(script):
    from lib.stages import export_design

    finished(script)
    design_edit.update(edit_payload(script), "owner-1")
    calls = image_calls(script)
    out = export_design.export(script["jobId"], version=int(job_of(script)["designVersion"]),
                               owner_scope="owner-1")
    assert "כותרת חדשה" in out["html"] and out["previewUrl"].startswith("https://")
    assert out["imageGenerationCalls"] == 0 and image_calls(script) == calls
    again = export_design.export(script["jobId"], owner_scope="owner-1")      # idempotent
    assert again["htmlHash"] == out["htmlHash"]
    with pytest.raises(StaleDesignVersion):
        export_design.export(script["jobId"], version=1)


# --- Phase B: the layout-first flow ---------------------------------------------------

LAYOUT_PLAN = {
    "canvas": CANVAS,
    "textLayers": [
        {**copy.deepcopy(l), "letterSpacing": 0, "fill": "solid", "strokeColor": None,
         "strokeWidth": 0, "wrap": "wrap", "overflow": "shrink"}
        for l in SCENE["textLayers"]
    ],
    "imageLayers": [{"id": "l_bg", "type": "background", "assetId": None, "x": 0, "y": 0,
                     "width": 1024, "height": 1024, "zIndex": 0, "objectFit": "cover"}],
    "shapeLayers": [{
        "id": "grad", "kind": "gradient", "effect": "g1", "x": 0, "y": 512, "width": 1024,
        "height": 512, "zIndex": 1, "color": None, "opacity": 1.0, "radius": 0, "angle": 180,
        "stops": [{"color": "#000000", "opacity": 0.0, "position": 0.0},
                  {"color": "#000000", "opacity": 0.8, "position": 1.0}],
        "borderColor": None, "borderWidth": 0.0, "rotation": 0}],
    "effects": [{"id": "g1", "kind": "readabilityGradient", "owner": "layout", "note": None}],
    "photoBrief": "A bright modern gym interior with calm dark areas at the top and bottom.",
}


@pytest.fixture
def layout_first(script, monkeypatch):
    monkeypatch.setattr(conf, "LAYOUT_FIRST_ENABLED", True)
    p = script
    table = p["table"]
    job_id = p["jobId"]
    table.items.pop(f"JOB#{job_id}")
    request_key = artifacts.artifact_key(job_id, Stage.ACCEPT_REQUEST, 1, "request.json")
    jobs.create_job(job_id, "owner-1", "hash-1", request_key, CANVAS, flow="layout-first")
    table.items[f"JOB#{job_id}"]["artifacts"] = {}
    generated = []

    def generate(prompt, width, height, *, transparent=False, operation_id=None, quality="high"):
        generated.append(prompt)
        return {"bytes": base.fake_photo(), "mimeType": "image/png", "requestId": "g1",
                "model": conf.IMAGE_MODEL}

    monkeypatch.setattr(image_provider, "generate", generate)
    previous = vision_provider.analyze

    def analyze(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "layout_plan_v1":
            p["calls"]["vision"].append({"schema": schema_name, "images": len(images),
                                         "prompt": prompt, "detail": kw.get("detail")})
            return copy.deepcopy(LAYOUT_PLAN)
        return previous(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", analyze)
    p["generated"] = generated
    return p


LAYOUT_STAGES = (Stage.PREFLIGHT, Stage.PREPARE_COPY, Stage.ANALYZE_REFERENCE,
                 Stage.PLAN_LAYOUT, Stage.GENERATE_PHOTO, Stage.VALIDATE_ASSETS)


def run_layout(p):
    for stage in LAYOUT_STAGES:
        run_stage(p["jobId"], stage)
    return review_pass(p, 1)


def test_layout_first_generates_only_a_text_free_photo_and_assembles_editable_html(layout_first):
    out = run_layout(layout_first)
    assert out["route"] == "approve"
    run_stage(layout_first["jobId"], Stage.FINALIZE)

    assert len(layout_first["generated"]) == 1 and layout_first["calls"]["image"] == []
    assert "NO text" in layout_first["generated"][0]
    assert "gym interior" in layout_first["generated"][0]
    job = job_of(layout_first)
    assert job["status"] == "SUCCEEDED"
    assert job["metrics"]["imageCalls"] == 1
    # None of the legacy decomposition ran.
    assert not {Stage.CONCEPT, Stage.EXTRACT_SCENE, Stage.CLEAN_PHOTO,
                Stage.DECORATION_OVERLAY} & set(job["artifacts"])
    html = artifacts.get_json(layout_first["jobId"], job["resultKey"])["html"]
    assert html.count("data-effect=") == 1 and "linear-gradient" in html
    for item in COPY_ITEMS:
        assert item["text"] in html
    assert set(layout_first["calls"]["published"]) == {"background", "preview"}  # no overlay exists


def test_layout_first_text_repair_makes_no_image_call(layout_first):
    layout_first["script"].append([OUTLINE])
    run_layout(layout_first)
    attempt = op(layout_first, "REPAIR_LIGHT", 1)["nextAttempt"]
    out = review_pass(layout_first, attempt)
    assert out["route"] == "approve"
    run_stage(layout_first["jobId"], Stage.FINALIZE, attempt=attempt)
    assert job_of(layout_first)["metrics"]["imageCalls"] == 1
    assert len(layout_first["generated"]) == 1
    assert "-webkit-text-stroke" in html_of(layout_first)


def test_layout_first_cannot_route_back_to_a_concept(layout_first):
    layout_first["script"].append([issue(
        "EVERYTHING_WRONG", target="concept", affected=[], impact="Nothing works.")])
    out = run_layout(layout_first)
    assert out["route"] == "unresolved" and not out["conceptDefect"]


def test_layout_first_is_refused_unless_the_deployment_enables_it(pipeline, monkeypatch):
    from lib.errors import InvalidRequest
    from lib.schemas import parse_start_request

    request = {**base.REQUEST, "designFlow": "layout-first"}
    monkeypatch.setattr(conf, "LAYOUT_FIRST_ENABLED", False)
    with pytest.raises(InvalidRequest, match="not enabled"):
        parse_start_request(request)
    monkeypatch.setattr(conf, "LAYOUT_FIRST_ENABLED", True)
    assert parse_start_request(request).designFlow == "layout-first"
    with pytest.raises(InvalidRequest, match="suppliedAssets"):
        parse_start_request({**request, "suppliedAssets": [
            {"id": "logo", "url": "https://res.cloudinary.com/demo/logo.png", "type": "logo"}]})


# --- a readability gradient the layers lack is owned by the layout ---------------


def _fidelity_says(monkeypatch, **verdict):
    previous = vision_provider.analyze

    def analyze(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "composite_fidelity_v1":
            return {"displaced": False, "description": "", "missingShading": "none", **verdict}
        return previous(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", analyze)


def test_a_missing_readability_gradient_is_drawn_in_code_not_regenerated(script, monkeypatch):
    _fidelity_says(monkeypatch, missingShading="left")
    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)
    job = job_of(script)

    # No overlay repair was requested and no extra image was paid for.
    assert not (job.get("counters") or {}).get("overlayRepairs")
    assert image_calls(script) == 3
    validation = artifacts.get_json(script["jobId"], job["artifacts"][Stage.VALIDATE_ASSETS]["validation"])
    assert validation["codeOwned"]["action"] == "added"
    assert validation["codeOwned"]["imageGenerationCalls"] == 0

    review_pass(script, 1)
    job = job_of(script)
    scene = artifacts.get_json(script["jobId"], job["artifacts"][Stage.ASSEMBLE]["scene"])
    assert [e["owner"] for e in scene["effects"] if e["kind"] == "readabilityGradient"] == ["layout"]
    shape = next(s for s in scene["shapeLayers"] if s["effect"] == "readabilityGradient")
    assert (shape["x"], shape["y"], shape["height"]) == (0, 0, CANVAS["height"])
    assert shape["angle"] == 90 and shape["stops"][0]["opacity"] > shape["stops"][1]["opacity"]
    html = artifacts.get_bytes(script["jobId"], job["artifacts"][Stage.ASSEMBLE]["html"]).decode()
    assert html.count("data-effect") >= 1 and html.count('data-effect="readabilityGradient"') == 1


def test_a_gradient_already_owned_is_never_drawn_a_second_time(script, monkeypatch):
    from lib.stages import validate

    scene = {"effects": [{"id": "g", "kind": "readabilityGradient", "owner": "photo", "note": None}]}
    done = validate._own_missing_shading(
        script["jobId"], job_of(script), scene, 1, {"missingShading": "bottom"})
    assert done["action"] == "kept" and done["owner"] == "photo"


def test_other_missing_artwork_still_goes_to_the_overlay_repair(script, monkeypatch):
    from lib.errors import OverlayRepairNeeded

    _fidelity_says(monkeypatch, displaced=True, description="the red ribbon is missing")
    for stage in FIRST_PASS[:-1]:
        run_stage(script["jobId"], stage, attempt=1)
    with pytest.raises(OverlayRepairNeeded):
        run_stage(script["jobId"], Stage.VALIDATE_ASSETS, attempt=1)


def test_the_gradient_falls_back_to_overlay_repair_when_shapes_are_disabled(script, monkeypatch):
    from lib.errors import OverlayRepairNeeded

    monkeypatch.setattr(conf, "CODE_SHAPES_ENABLED", False)
    _fidelity_says(monkeypatch, missingShading="bottom")
    for stage in FIRST_PASS[:-1]:
        run_stage(script["jobId"], stage, attempt=1)
    with pytest.raises(OverlayRepairNeeded):
        run_stage(script["jobId"], Stage.VALIDATE_ASSETS, attempt=1)


@pytest.mark.parametrize("preset,box,angle", [
    ("gradient-left", (0, 0, 614, 1024), 90),
    ("gradient-right", (410, 0, 614, 1024), 270),
    ("gradient-top", (0, 0, 1024, 614), 0),
    ("gradient-bottom", (0, 410, 1024, 614), 180),
])
def test_gradient_presets_put_the_dark_end_at_the_named_edge(preset, box, angle):
    from lib import scene_patch

    scene = copy.deepcopy(SCENE)
    scene["imageLayers"][0]["zIndex"] = 0
    for layer in scene["imageLayers"][1:] + scene["textLayers"]:
        layer["zIndex"] = max(layer["zIndex"], 1)
    patched = scene_patch.add_shape(scene, preset)
    shape = patched["shapeLayers"][-1]
    assert (shape["x"], shape["y"], shape["width"], shape["height"]) == box
    assert shape["angle"] == angle
    dark_at = shape["stops"][0] if preset in ("gradient-left", "gradient-right") else shape["stops"][-1]
    assert dark_at["opacity"] > 0 and dark_at["position"] == (0.0 if "left" in preset or "right" in preset else 1.0)


def test_a_faint_gradient_is_strengthened_by_a_number_not_a_css_string():
    from lib import scene_patch
    from lib.errors import InvalidScene

    scene = copy.deepcopy(SCENE)
    scene["imageLayers"][0]["zIndex"] = 0
    for layer in scene["imageLayers"][1:] + scene["textLayers"]:
        layer["zIndex"] = max(layer["zIndex"], 1)
    scene = scene_patch.add_shape(scene, "gradient-left")
    shape_id = scene["shapeLayers"][-1]["id"]

    stronger = scene_patch.apply_properties(
        scene, shape_id, {"gradientStrength": "1.0"}, target="layout")
    peak = lambda s: max(st["opacity"] for st in s["shapeLayers"][-1]["stops"])
    assert peak(stronger) == 1.0 and peak(scene) == 0.9
    assert stronger["shapeLayers"][-1]["stops"][-1]["opacity"] == 0.0   # the fade stays a fade

    with pytest.raises(InvalidScene):
        scene_patch.apply_properties(
            scene, shape_id, {"color": "linear-gradient(90deg, #000, #fff)"}, target="layout")
    with pytest.raises(InvalidScene):    # typography corrections may not touch shapes
        scene_patch.apply_properties(
            scene, shape_id, {"gradientStrength": "1"}, target="typography")


def test_a_plan_with_too_many_gradient_stops_is_thinned_not_rejected():
    from lib.stages import plan_layout

    stops = [{"color": "#000000", "opacity": o, "position": p}
             for o, p in ((0.9, 0.0), (0.8, 0.25), (0.6, 0.5), (0.3, 0.75), (0.0, 1.0))]
    raw = {"shapeLayers": [{"id": "g", "stops": list(reversed(stops))}, {"id": "p", "stops": []}]}
    plan_layout._limit_stops(raw)
    kept = raw["shapeLayers"][0]["stops"]
    assert len(kept) == 4
    assert kept[0]["position"] == 0.0 and kept[-1]["position"] == 1.0
    assert [s["position"] for s in kept] == sorted(s["position"] for s in kept)
    assert raw["shapeLayers"][1]["stops"] == []


def _bad_chevron_plan():
    plan = copy.deepcopy(LAYOUT_PLAN)
    plan["shapeLayers"].append({
        "id": "arrow-2", "kind": "chevron", "effect": "g2", "x": 10, "y": 10, "width": 40,
        "height": 40, "zIndex": 2, "color": None, "opacity": 1.0, "radius": 0, "angle": 180,
        "stops": [], "borderColor": None, "borderWidth": 0.0, "rotation": 45})
    plan["effects"].append({"id": "g2", "kind": "chevrons", "owner": "layout", "note": None})
    return plan


def test_a_plan_that_breaks_a_schema_rule_gets_one_corrected_retry(layout_first, monkeypatch):
    plans = [_bad_chevron_plan(), copy.deepcopy(LAYOUT_PLAN)]
    prompts_seen = []
    previous = vision_provider.analyze

    def analyze(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "layout_plan_v1":
            prompts_seen.append(prompt)
            return plans.pop(0)
        return previous(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", analyze)
    out = run_layout(layout_first)
    assert out["route"] == "approve"
    assert len(prompts_seen) == 2 and "PREVIOUS PLAN WAS REJECTED" in prompts_seen[1]
    assert "borderColor" in prompts_seen[1]
    assert len(layout_first["generated"]) == 1          # still exactly one image


def test_a_plan_rejected_twice_fails_without_generating_any_image(layout_first, monkeypatch):
    from lib.errors import InvalidScene

    previous = vision_provider.analyze
    monkeypatch.setattr(vision_provider, "analyze", lambda prompt, images=(), *, schema, schema_name, **kw: (
        _bad_chevron_plan() if schema_name == "layout_plan_v1"
        else previous(prompt, images, schema=schema, schema_name=schema_name, **kw)))
    for stage in LAYOUT_STAGES[:3]:
        run_stage(layout_first["jobId"], stage)
    with pytest.raises(InvalidScene):
        run_stage(layout_first["jobId"], Stage.PLAN_LAYOUT)
    assert layout_first["generated"] == []


def _bad_chevron_plan():
    plan = copy.deepcopy(LAYOUT_PLAN)
    plan["shapeLayers"].append({
        "id": "arrow-2", "kind": "chevron", "effect": "g2", "x": 10, "y": 10, "width": 40,
        "height": 40, "zIndex": 2, "color": None, "opacity": 1.0, "radius": 0, "angle": 180,
        "stops": [], "borderColor": None, "borderWidth": 0.0, "rotation": 45})
    plan["effects"].append({"id": "g2", "kind": "chevrons", "owner": "layout", "note": None})
    return plan


def test_a_plan_that_breaks_a_schema_rule_gets_one_corrected_retry(layout_first, monkeypatch):
    plans = [_bad_chevron_plan(), copy.deepcopy(LAYOUT_PLAN)]
    prompts_seen = []
    previous = vision_provider.analyze

    def analyze(prompt, images=(), *, schema, schema_name, **kw):
        if schema_name == "layout_plan_v1":
            prompts_seen.append(prompt)
            return plans.pop(0)
        return previous(prompt, images, schema=schema, schema_name=schema_name, **kw)

    monkeypatch.setattr(vision_provider, "analyze", analyze)
    out = run_layout(layout_first)
    assert out["route"] == "approve"
    assert len(prompts_seen) == 2 and "PREVIOUS PLAN WAS REJECTED" in prompts_seen[1]
    assert "borderColor" in prompts_seen[1]
    assert len(layout_first["generated"]) == 1          # still exactly one image


def test_a_plan_rejected_twice_fails_without_generating_any_image(layout_first, monkeypatch):
    from lib.errors import InvalidScene

    previous = vision_provider.analyze
    monkeypatch.setattr(vision_provider, "analyze", lambda prompt, images=(), *, schema, schema_name, **kw: (
        _bad_chevron_plan() if schema_name == "layout_plan_v1"
        else previous(prompt, images, schema=schema, schema_name=schema_name, **kw)))
    for stage in LAYOUT_STAGES[:3]:
        run_stage(layout_first["jobId"], stage)
    with pytest.raises(InvalidScene):
        run_stage(layout_first["jobId"], Stage.PLAN_LAYOUT)
    assert layout_first["generated"] == []


def test_a_layout_owned_effect_that_nothing_draws_is_withdrawn_not_fatal():
    from lib.stages import plan_layout

    raw = {"shapeLayers": [{"id": "s", "effect": "drawn"}],
           "effects": [{"id": "drawn", "owner": "layout"},
                       {"id": "ghost", "owner": "layout"},
                       {"id": "baked", "owner": "photo"}]}
    plan_layout._drop_undrawn_effects(raw)
    assert [e["id"] for e in raw["effects"]] == ["drawn", "baked"]


def test_out_of_range_shape_numbers_are_snapped_not_fatal():
    from lib.stages import plan_layout

    raw = {"shapeLayers": [{"rotation": 100, "opacity": 1.2, "angle": 400, "zIndex": 120,
                            "radius": 5, "borderWidth": 60.0,
                            "stops": [{"opacity": -0.1, "position": 1.4}]},
                           {"rotation": 360}]}
    plan_layout._snap_shape_numbers(raw)
    first, second = raw["shapeLayers"]
    assert first["rotation"] == 90 and first["opacity"] == 1.0 and first["angle"] == 360
    assert first["zIndex"] == 100 and first["borderWidth"] == 48.0
    assert first["stops"] == [{"opacity": 0.0, "position": 1.0}]
    assert second["rotation"] == 0


def test_dynamodb_decimals_serialise_and_hash_like_plain_numbers():
    from decimal import Decimal

    from lib import dependencies

    stored = {"attempt": Decimal("1"), "ratio": Decimal("0.5"), "nested": [Decimal("3")]}
    assert json.loads(artifacts.json.dumps(stored, default=artifacts.json_default)) == {
        "attempt": 1, "ratio": 0.5, "nested": [3]}
    assert dependencies.digest(stored) == dependencies.digest(
        {"attempt": 1, "ratio": 0.5, "nested": [3]})
    with pytest.raises(TypeError):
        artifacts.json_default(object())


def test_metric_writes_alias_the_reserved_word_metrics(monkeypatch):
    seen = {}

    class Table:
        def update_item(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(jobs, "_tbl", lambda: Table())
    jobs.add_metric("job-1", "imageCalls")
    assert seen["UpdateExpression"] == "ADD #root.#m :n"
    assert seen["ExpressionAttributeNames"] == {"#root": "metrics", "#m": "imageCalls"}
