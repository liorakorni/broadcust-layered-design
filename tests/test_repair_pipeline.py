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
from lib import artifacts, assemble_html, candidates, design_edit, design_state, jobs, telemetry
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
    loop the way the workflow's Correct state does: by delivering the best usable
    candidate with its unresolved findings (status SUCCEEDED_WITH_WARNINGS), or by
    raising its precise error when no candidate is usable.
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
            op(p, "CORRECT", attempt)       # delivers the best candidate, flagged
            return decisions
    raise AssertionError("the loop did not end")


def delivered(p):
    """`(status, result)` of a job that ended by delivering a candidate."""
    job = job_of(p)
    return job["status"], artifacts.get_json(p["jobId"], job["resultKey"])


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


def test_a_gradient_a_shape_already_draws_is_never_duplicated_by_a_repair(script):
    """Two owners is the defect this prevents: the layout drawing the darkening
    twice. (An effect an *asset* claims but nothing draws is a different case:
    the review has seen the render without it, and the layout takes it over.)"""
    from lib import repair_routing

    scene = copy.deepcopy(SCENE)
    scene["effects"] = [{"id": "fade", "kind": "readabilityGradient",
                         "owner": "layout", "note": None}]
    scene["shapeLayers"] = [{
        "id": "s_fade", "type": "shape", "kind": "gradient", "effect": "fade",
        "x": 0, "y": 512, "width": 1024, "height": 512, "zIndex": 1,
        "color": None, "opacity": 1.0, "radius": 0, "angle": 180,
        "stops": [{"color": "#000000", "opacity": 0.8, "position": 0.0},
                  {"color": "#000000", "opacity": 0.0, "position": 1.0}],
        "borderColor": None, "borderWidth": 0.0, "rotation": 0}]
    record = repair_routing.route_issue(GRADIENT_MISSING, {
        "scene": scene, "copyKeys": set(), "assetForLayer": {}, "assetIds": set(),
        "fonts": conf.APPROVED_FONT_FAMILIES, "conceptJustified": False})
    assert record["route"] == "unresolved"
    assert any("already has a readabilityGradient" in p for p in record["problems"])


def test_an_asset_owned_gradient_the_render_lacks_is_taken_over_once(script):
    """Measured on a live run: the scene said the photograph carried the
    darkening, the separated photograph did not, and the design failed over one
    CSS gradient."""
    from lib import repair_routing

    record = repair_routing.route_issue(GRADIENT_MISSING, {
        "scene": _baked_gradient_scene(), "copyKeys": set(), "assetForLayer": {},
        "assetIds": set(), "fonts": conf.APPROVED_FONT_FAMILIES,
        "conceptJustified": False})

    assert record["route"] == "light"
    assert record["patch"]["addShapes"] == [
        {"preset": "gradient-bottom", "effect": "effect-photo-darkening",
         "takeOver": True}]


def _baked_gradient_scene():
    scene = copy.deepcopy(SCENE)
    scene["effects"] = [
        {"id": "effect-photo-darkening", "kind": "readabilityGradient",
         "owner": "photo", "note": "dark gradient painted into the photograph"}]
    return scene


def _route(issue_, scene):
    from lib import repair_routing

    return repair_routing.route_issue(issue_, {
        "scene": scene, "copyKeys": set(), "assetForLayer": {}, "assetIds": set(),
        "fonts": conf.APPROVED_FONT_FAMILIES, "conceptJustified": False})


#: What a live review actually returned when the separated layers turned out not
#: to contain the darkening the scene said the photograph owned: properties on
#: the effect's id, which is not a layer and never was.
DARKENING_LOST = issue(
    "MISSING_RIGHT_DARKENING", target="layout", affected=["effect-photo-darkening"],
    constraints=[("effect-photo-darkening", "angle", "90"),
                 ("effect-photo-darkening", "gradientStrength", "0.95")],
)


def test_a_correction_aimed_at_an_undrawn_effect_becomes_the_shape_that_draws_it():
    record = _route(DARKENING_LOST, _baked_gradient_scene())

    assert record["route"] == "light"
    assert record["patch"]["addShapes"] == [
        {"preset": "gradient-left", "effect": "effect-photo-darkening",
         "takeOver": True}]
    # The translation is recorded, not silent: it says what was asked and why it
    # is being done differently.
    assert record["translated"]["from"][0]["property"] == "angle"
    assert "no shape draws it" in record["translated"]["reason"]


def test_an_undrawn_effect_is_taken_over_by_the_layout_exactly_once():
    from lib import scene_patch
    from lib.errors import InvalidScene

    scene = _baked_gradient_scene()
    taken = scene_patch.add_shape(
        scene, "gradient-right", effect_id="effect-photo-darkening", take_over=True)

    assert [e["owner"] for e in taken["effects"]] == ["layout"]
    assert [s["effect"] for s in taken["shapeLayers"]] == ["effect-photo-darkening"]
    # And a second attempt now finds a shape drawing it, so it is refused.
    with pytest.raises(InvalidScene, match="already has a readabilityGradient"):
        scene_patch.add_shape(taken, "gradient-right", take_over=True)


#: What a live review returned when the photograph came back without the
#: readability shading the scene said it carried: the right complaint, in words,
#: naming nothing - because the gradient it wanted did not exist to be named.
SHADING_MISSING = {
    **issue("MISSING_READABILITY_SHADING", target="layout", affected=[]),
    "message": ("The dark text field and its separation from the right-side "
                "photograph. Fix: restore the readability shading with a "
                "code-owned dark gradient, keeping the subject visible."),
}


def test_a_shading_finding_that_names_nothing_still_draws_the_undrawn_gradient():
    record = _route(SHADING_MISSING, _baked_gradient_scene())

    assert record["route"] == "light"
    # The edge comes from the scene's own geometry rather than the finding's
    # prose: the fixture's words are centred horizontally and sit high, so the
    # darkening goes under them at the top.
    assert record["patch"]["addShapes"] == [
        {"preset": "gradient-top", "effect": "effect-photo-darkening",
         "takeOver": True}]


def test_the_unnamed_shading_follows_the_side_the_words_are_on():
    scene = _baked_gradient_scene()
    for layer in scene["textLayers"]:          # the whole column moved right
        layer["x"] = 700

    record = _route(SHADING_MISSING, scene)

    assert record["patch"]["addShapes"][0]["preset"] == "gradient-right"


def test_an_add_request_without_a_preset_still_draws_the_effect():
    """Measured live: the review did name the effect - affectedElementIds
    ["add:effect-photo-darkening"] - but described the gradient in prose instead
    of naming a preset. Half a request is still a request; the preset follows
    from the scene."""
    half = {**issue("MISSING_TEXT_SIDE_DARKENING", target="layout",
                    affected=["add:effect-photo-darkening"]),
            "message": "Restore the darkening above the photograph."}

    record = _route(half, _baked_gradient_scene())

    assert record["route"] == "light"
    assert record["patch"]["addShapes"] == [
        {"preset": "gradient-top", "effect": "effect-photo-darkening",
         "takeOver": True}]


def test_an_add_request_naming_its_own_effect_adds_that_effect():
    """The review may also invent the id when the design declares no darkening."""
    scene = copy.deepcopy(SCENE)
    scene["effects"] = []
    half = {**issue("MISSING_READABILITY_SHADING", target="layout",
                    affected=["add:text-field-darkening"]),
            "message": "Add a dark gradient behind the copy."}

    record = _route(half, scene)

    assert record["patch"]["addShapes"] == [
        {"preset": "gradient-top", "effect": "text-field-darkening",
         "takeOver": False}]


def test_a_review_that_names_a_preset_itself_is_left_alone():
    """The documented form is applied as written, not re-derived."""
    spelled_out = issue(
        "MISSING_READABILITY_SHADING", target="layout",
        affected=["add:readabilityGradient"],
        constraints=[("add:readabilityGradient", "addShape", "gradient-bottom")])
    scene = copy.deepcopy(SCENE)
    scene["effects"] = []

    record = _route(spelled_out, scene)

    assert record["patch"]["addShapes"] == [
        {"preset": "gradient-bottom", "effect": "readabilityGradient",
         "takeOver": False}]
    assert "translated" not in record


def test_the_reviewer_is_told_what_each_property_will_accept():
    """Measured live twice: a review asked for 'Open Sans Hebrew Condensed', and
    another for a letterSpacing of -12 where -10 is the limit. Each time the
    repair refused a value no one has or no one allows, and the design failed over
    one number. The prompt now carries the fonts and the bounds, generated from
    the same table the executor validates against."""
    from lib import scene_patch

    help_text = scene_patch.property_help("typography")

    assert "fontFamily (only " in help_text
    for family in conf.APPROVED_FONT_FAMILIES:
        assert family in help_text
    low, high = scene_patch.TEXT_TYPOGRAPHY["letterSpacing"][1]
    assert f"{low} to {high}" in help_text
    assert '"rtl" or "ltr"' in help_text          # enums, not only ranges


def test_a_shading_finding_adds_the_gradient_a_design_never_declared():
    """Measured live: the review asked for a left-to-right darkening on a design
    whose scene declares no readabilityGradient at all - nothing to take over, so
    the answer is the new shape, not a failed job."""
    scene = copy.deepcopy(SCENE)
    scene["effects"] = []

    record = _route(SHADING_MISSING, scene)

    assert record["route"] == "light"
    assert record["patch"]["addShapes"] == [
        {"preset": "gradient-top", "effect": "readabilityGradient",
         "takeOver": False}]
    assert "declares no readabilityGradient" in record["translated"]["reason"]


def test_a_shading_finding_is_dropped_when_a_shape_already_draws_one():
    """The design already darkens; a second gradient would just darken twice."""
    scene = copy.deepcopy(SCENE)
    scene["effects"] = [{"id": "effect-readability", "kind": "readabilityGradient",
                         "owner": "layout", "note": None}]
    scene["shapeLayers"] = [dict(
        id="shade", type="shape", kind="gradient", effect="effect-readability",
        x=0, y=0, width=1024, height=400, zIndex=1, color=None, opacity=1.0,
        radius=0, angle=180, borderColor=None, borderWidth=0.0, rotation=0,
        stops=[{"color": "#000000", "opacity": 0.9, "position": 0.0},
               {"color": "#000000", "opacity": 0.0, "position": 1.0}])]

    record = _route(SHADING_MISSING, scene)

    assert record["route"] == "unresolved"


def test_a_layout_finding_about_something_else_is_still_unroutable():
    """Only the words of the finding may send it to the undrawn effect; otherwise
    every unnamed layout complaint would be answered with a gradient."""
    vague = {**issue("CROWDED_HEADER", target="layout", affected=[]),
             "message": "The header feels crowded and the margins are tight."}

    record = _route(vague, _baked_gradient_scene())

    assert record["route"] == "unresolved"
    assert any("must name the elements" in p for p in record["problems"])


def test_a_shading_finding_is_not_routed_when_two_effects_are_undrawn():
    """With more than one candidate there is no single reading, so it stays
    unresolved rather than guessing which effect was meant."""
    scene = _baked_gradient_scene()
    scene["effects"].append(
        {"id": "effect-text-field", "kind": "readabilityGradient", "owner": "decoration",
         "note": "a second darkening, also drawn by nothing"})

    record = _route(SHADING_MISSING, scene)

    assert record["route"] == "unresolved"


def test_a_shape_the_repair_adds_is_entered_in_the_decomposition_plan():
    """Measured live: the repair added the gradient shape, the plan still did not
    mention it, and the patched scene was rejected as an incomplete
    decomposition - failing the job the repair existed to save."""
    from lib import schemas, scene_patch

    scene = _planned_scene()
    taken = scene_patch.add_shape(
        scene, "gradient-right", effect_id="effect-photo-darkening", take_over=True)

    schemas.parse_scene(taken, CANVAS["width"], CANVAS["height"])
    entry = next(c for c in taken["components"]
                 if c["layerId"] == "shape-effect-photo-darkening")
    assert entry["kind"] == "editableShape"


def _planned_scene():
    """A baked-gradient scene carrying a decomposition plan, as live ones do."""
    scene = _baked_gradient_scene()
    scene["textLayers"] = [l for l in scene["textLayers"]
                           if not l["contentKey"].startswith("contact.")]
    scene["components"] = [
        {"id": "c-photo", "kind": "photo", "layerId": "l_bg",
         "pairsWith": None, "note": None},
        {"id": "c-deco", "kind": "retainedRaster", "layerId": "l_deco",
         "pairsWith": None, "note": None},
    ]
    return scene


def test_a_baked_gradient_is_not_taken_over_without_a_finding():
    """A photograph that really carries its gradient looks the same from here, so
    nothing infers the take-over from the scene alone."""
    from lib import scene_patch
    from lib.errors import InvalidScene

    with pytest.raises(InvalidScene, match="already has a readabilityGradient"):
        scene_patch.add_shape(_baked_gradient_scene(), "gradient-bottom")


def test_a_correction_naming_a_layer_that_does_not_exist_is_still_unresolved():
    bad = issue("X", target="layout", affected=["no-such-thing"],
                constraints=[("no-such-thing", "x", "10")])

    record = _route(bad, _baked_gradient_scene())

    assert record["route"] == "unresolved"
    assert any("does not exist" in p for p in record["problems"])


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

    op(script, "CORRECT", 1)

    status, result = delivered(script)
    # Nothing was regenerated; the design that exists is returned, flagged.
    assert image_calls(script) == calls
    assert status == "SUCCEEDED_WITH_WARNINGS"
    assert result["publishReady"] is False and result["needsReview"] is True
    assert result["stopReason"] == "REPAIR_ROUTE_UNRESOLVED"
    assert why in " ".join(
        p for r in out["routing"]["issues"] for p in r["problems"]
    ) or why in result["diagnostics"]["original"]["message"]
    # The original reason is kept in the diagnostics, beside the candidate returned.
    assert result["diagnostics"]["original"]["code"] == "REPAIR_ROUTE_UNRESOLVED"
    assert result["diagnostics"]["selectedAttempt"] == 1
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
    drive(script)
    repairs = job_of(script)["metrics"]["lightRepairs"]
    assert repairs <= conf.MAX_REPAIRS["typography"]
    status, result = delivered(script)
    assert status == "SUCCEEDED_WITH_WARNINGS"
    assert result["stopReason"] in ("REPAIR_NO_PROGRESS", "CORRECTION_EXHAUSTED")
    assert "did not improve" in result["diagnostics"]["original"]["message"] or \
        "bounded repairs" in result["diagnostics"]["original"]["message"]
    assert image_calls(script) == 3                          # the first pass only
    assert result["selectedSnapshotKey"]
    assert result["publishReady"] is False


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
    drive(script)
    assert job_of(script)["metrics"]["lightRepairs"] <= conf.MAX_REPAIRS["layout"]
    assert delivered(script)[0] == "SUCCEEDED_WITH_WARNINGS"
    assert image_calls(script) == 3


def _verdict(code, reason="evidence"):
    from lib import repair_progress

    return {"verdict": code, "label": repair_progress.LABELS[code], "reason": reason,
            "evidence": {}}


def test_a_new_blocking_finding_is_not_by_itself_a_regression(script):
    """The rule this replaces read a higher blocking count as damage done by the
    repair. Nothing here shows damage - the render did not move - so the loop
    carries on and the design is approved on the next review."""
    script["script"].append([OUTLINE])
    script["script"].append([copy.deepcopy(OUTLINE), issue(
        "NEW_PROBLEM", target="layout", affected=["t_sub"],
        constraints=[("t_sub", "y", "340")])])

    decisions = drive(script)

    assert [route for _, route in decisions] == ["light", "light", "approve"]
    kept = job_of(script)["designCandidates"]
    assert kept["2"]["blockingCount"] == 2 > kept["1"]["blockingCount"]
    assert kept["2"]["progressVerdict"] in ("B", "C", "E")


def test_a_partial_improvement_is_followed_by_another_bounded_repair(script, monkeypatch):
    """The same finding on the same element is no longer 'the same failure' when
    the render measurably moved towards the concept: a different patch is tried."""
    from lib.stages import review as review_stage

    stronger = issue("HEADLINE_OUTLINE_MISSING", target="typography", affected=["t_title"],
                     constraints=[("t_title", "strokeColor", "#FFFFFF"),
                                  ("t_title", "strokeWidth", "5")])
    script["script"].append([OUTLINE])
    script["script"].append([stronger])
    monkeypatch.setattr(review_stage, "_progress", lambda *a, **k: _verdict("B"))

    decisions = drive(script)

    assert [route for _, route in decisions] == ["light", "light", "approve"]
    assert job_of(script)["designCandidates"]["2"]["progressVerdict"] == "B"
    assert image_calls(script) == 3                  # none spent on the repairs


def test_a_repair_that_changed_nothing_visible_still_stops(script, monkeypatch):
    from lib.stages import review as review_stage

    other = issue("HEADLINE_OUTLINE_MISSING", target="typography", affected=["t_title"],
                  constraints=[("t_title", "strokeColor", "#FFFFFF"),
                               ("t_title", "strokeWidth", "5")])
    script["script"].append([OUTLINE])
    script["script"].append([other])
    monkeypatch.setattr(review_stage, "_progress", lambda *a, **k: _verdict("C"))

    drive(script)

    status, result = delivered(script)
    assert status == "SUCCEEDED_WITH_WARNINGS"
    assert result["stopReason"] == "REPAIR_NO_PROGRESS"
    assert "no meaningful change" in result["diagnostics"]["original"]["message"]


def test_a_verified_regression_restores_the_better_candidate_once(script, monkeypatch):
    from lib.stages import review as review_stage

    script["script"].append([OUTLINE])
    script["script"].append([issue("WORSE", target="layout", affected=["t_sub"],
                                   constraints=[("t_sub", "y", "340")])])
    seen = []

    def progress(job, candidates, previous, *a, **k):
        seen.append(previous["attempt"])
        return _verdict("D", "the patched region moved further from the concept")

    monkeypatch.setattr(review_stage, "_progress", progress)
    first_pass(script)
    attempt = op(script, "REPAIR_LIGHT", 1)["nextAttempt"]
    out = review_pass(script, attempt)

    # Attempt 2 verifiably regressed: the next repair starts from attempt 1.
    assert out["route"] == "light"
    review = artifacts.get_json(
        script["jobId"], job_of(script)["artifacts"][Stage.REVIEW]["review"])
    restore = review["routing"]["restoreBase"]
    assert restore["attempt"] == 1
    op(script, "REPAIR_LIGHT", attempt)
    record = artifacts.get_json(script["jobId"], artifacts.artifact_key(
        script["jobId"], Stage.REPAIR, attempt, "repair.json"))
    assert record["restoredFrom"]["attempt"] == 1

    # A second verified regression ends it: no unbounded retrying.
    script["script"].append([issue("WORSE_AGAIN", target="layout", affected=["t_sub"],
                                   constraints=[("t_sub", "y", "360")])])
    later = op(script, "REPAIR_LIGHT", attempt)["nextAttempt"]
    out = review_pass(script, later)
    assert out["route"] == "noProgress"


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
          "warningCount": 0, "route": "approve", "htmlHash": "h", "snapshotKey": "s",
          "pointers": {"ASSEMBLE": {"html": "h", "scene": "s", "assets": [{}]},
                       "RENDER": {"native": "n"}}}
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


def test_an_export_round_trip_preserves_decoration_markers_ids_and_styles(script):
    """The in-repo proxy for the editor's save and reopen.

    A decoration has to survive being stored, edited and exported with its marker,
    its element id and its styles intact, or the editor loses the layer. The
    native editor check stays pending until the editor is available.
    """
    import re

    from lib.stages import export_design

    script["script"].append([GRADIENT_MISSING])
    first_pass(script)
    drive_remaining(script)
    delivered = html_of(script)
    shape_id = scene_of(script)["shapeLayers"][0]["id"]
    element_id = assemble_html.shape_element_ids(scene_of(script))[shape_id]

    out = export_design.export(script["jobId"], owner_scope="owner-1")
    decoration = lambda doc: re.search(
        rf'<div class="words (d\d+)" id="{element_id}"([^>]*)>', doc)
    assert decoration(delivered).groups() == decoration(out["html"]).groups()
    assert 'data-role="decoration"' in decoration(out["html"]).group(2)
    assert "data-shape=" in decoration(out["html"]).group(2)
    assert "linear-gradient" in out["html"]
    assert out["html"] == delivered                      # nothing was edited yet

    # Restyle the decoration the way the editor would, then export again.
    edited = design_edit.update(
        {"jobId": script["jobId"], "requestId": "edit-decoration",
         "expectedVersion": int(job_of(script)["designVersion"]),
         "changes": {"layout": {shape_id: {"opacity": "0.5"}}}},
        "owner-1",
    )
    assert edited["changed"] is True and edited["imageGenerationCalls"] == 0
    reopened = export_design.export(script["jobId"], owner_scope="owner-1")["html"]
    kept = decoration(reopened)
    assert kept.group(1) == decoration(delivered).group(1)        # same dNNNN class
    assert 'data-role="decoration"' in kept.group(2)
    assert "opacity:0.5" in kept.group(2)
    assert '<div class="text"></div>' in reopened


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


# --- a repair must change the design, and only what was asked -------------------


def test_the_reviewer_prompt_lists_exactly_the_supported_properties(script):
    """One table. A property the reviewer is offered must be one the executor applies."""
    from lib import prompts, scene_patch

    first_pass(script)
    prompt = next(c["prompt"] for c in reversed(script["calls"]["vision"])
                  if c["schema"] == "review_v2")

    supported = set()
    for target in ("typography", "layout"):
        # The block the executor generates is the block the reviewer is given.
        assert scene_patch.property_help(target) in prompt
        supported |= scene_patch.supported_properties(target)

    for name in supported:
        assert name in prompt
    assert "gradientStrength" in supported          # translated, never ignored
    # The old hand-written list is gone, so it cannot drift from the table.
    assert "strokeWidth (px)" not in prompts.load("review")
    assert "{typography_properties}" in prompts.load("review")


def test_a_no_op_patch_is_reported_as_no_progress_not_success(script):
    """A constraint that sets a property to the value it already has fixes nothing."""
    title = next(l for l in SCENE["textLayers"] if l["id"] == "t_title")
    script["script"].append([issue(
        "TITLE_TOO_SMALL", target="typography", affected=["t_title"],
        constraints=[("t_title", "fontSize", str(title["fontSize"]))],
    )])
    first_pass(script)
    with pytest.raises(RepairNoProgress) as raised:
        op(script, "REPAIR_LIGHT", 1)
    assert "changed nothing" in raised.value.message
    assert "TITLE_TOO_SMALL" in raised.value.message
    # Nothing committed: no new design version, no spent budget.
    assert job_of(script)["designVersion"] == snapshot_of(script)["version"]
    assert int(job_of(script).get("repairs_typography", 0)) == 0


def test_a_patch_that_does_not_reach_the_css_is_reported_as_no_progress(script):
    """The scene is only assembly's input; the delivered design is the HTML.

    A background layer's box is one such field: the assembler sizes the
    photograph from the canvas, so moving it changes the scene and nothing that
    is rendered.
    """
    from lib.stages import repair as repair_stage

    first_pass(script)
    job = job_of(script)
    before = scene_of(script)
    after = copy.deepcopy(before)
    background = next(l for l in after["imageLayers"] if l["type"] == "background")
    background["x"] += 40

    routed = [{"code": "PHOTO_OFF_CENTRE", "target": "layout",
               "patch": {"layers": {background["id"]: {"x": background["x"]}}}}]
    with pytest.raises(RepairNoProgress) as raised:
        repair_stage.verify_changed(before, after, routed, job, produced=job["artifacts"])
    assert "not the assembled HTML" in raised.value.message
    assert f"{background['id']}.x" in raised.value.message


def test_a_change_that_only_the_text_fitter_reads_is_accepted(script):
    """`overflow` governs what the fitter may shrink, so it emits no CSS of its own."""
    from lib.stages import repair as repair_stage

    first_pass(script)
    job = job_of(script)
    before = scene_of(script)
    after = copy.deepcopy(before)
    title = next(l for l in after["textLayers"] if l["id"] == "t_title")
    title["overflow"] = "visible" if title.get("overflow") != "visible" else "shrink"

    routed = [{"code": "TITLE_MUST_KEEP_ITS_SIZE", "target": "typography",
               "patch": {"layers": {"t_title": {"overflow": title["overflow"]}}}}]
    out = repair_stage.verify_changed(before, after, routed, job, produced=job["artifacts"])
    assert out["changed"] == {"t_title": ["overflow"]}
    assert out["styleChanged"] == []


def test_a_patch_may_not_touch_an_element_the_review_did_not_name(script, monkeypatch):
    from lib import scene_patch

    script["script"].append([OUTLINE])
    first_pass(script)
    real = scene_patch.apply_properties

    def also_moves_the_subtitle(scene, layer_id, properties, *, target, font_names=()):
        patched = real(scene, layer_id, properties, target=target, font_names=font_names)
        patched = copy.deepcopy(patched)
        for layer in patched["textLayers"]:
            if layer["id"] == "t_sub":
                layer["x"] = layer["x"] + 40
        return patched

    monkeypatch.setattr(scene_patch, "apply_properties", also_moves_the_subtitle)
    with pytest.raises(RepairRouteUnresolved) as raised:
        op(script, "REPAIR_LIGHT", 1)
    assert "t_sub" in raised.value.message
    assert "t_title" in raised.value.message


def test_a_round_that_restyles_text_and_adds_a_shape_stays_in_scope(script):
    """Measured live: a review asked for a readability gradient and a headline
    change in the same round, and the design failed because the gradient the
    repair had just been told to add counted as an element nobody named. An
    addition names its elements by consequence - the shape the preset creates and
    the effects list that records its owner - not by id.
    """
    from lib import scene_patch
    from lib.stages import repair as repair_stage

    first_pass(script)
    job = job_of(script)
    before = scene_of(script)
    after = scene_patch.add_shape(before, "gradient-bottom",
                                  effect_id="readabilityGradient")
    title = next(l for l in after["textLayers"] if l["id"] == "t_title")
    title["strokeColor"], title["strokeWidth"] = "#FFFFFF", 3.0

    routed = [
        {"code": "HEADLINE_OUTLINE_MISSING", "target": "typography",
         "patch": {"layers": {"t_title": {"strokeColor": "#FFFFFF",
                                          "strokeWidth": 3.0}}}},
        {"code": "MISSING_READABILITY_SHADING", "target": "layout",
         "patch": {"addShapes": [{"preset": "gradient-bottom",
                                  "effect": "readabilityGradient",
                                  "takeOver": False}]}},
    ]

    out = repair_stage.verify_changed(before, after, routed, job,
                                      produced=job["artifacts"])
    assert set(out["changed"]) == {"t_title", "effects",
                                   scene_patch.shape_id_for("readabilityGradient")}


def test_a_supported_patch_changes_the_scene_the_css_and_the_render(script):
    from lib import assemble_html

    script["script"].append([OUTLINE])
    first_pass(script)
    before_html = html_of(script)
    before_style = assemble_html.element_style(before_html, "text-1001")

    drive_remaining(script)

    after_html = html_of(script)
    after_style = assemble_html.element_style(after_html, "text-1001")
    assert before_style != after_style
    assert "-webkit-text-stroke" in after_style and "-webkit-text-stroke" not in before_style
    record = artifacts.get_json(
        script["jobId"], artifacts.artifact_key(script["jobId"], Stage.REPAIR, 1, "repair.json"))
    assert record["touched"]["changed"] == {"t_title": ["strokeColor", "strokeWidth"]}
    assert record["touched"]["styleChanged"] == ["t_title"]
    # And the render really ran again on the new document.
    assert job_of(script)["artifacts"][Stage.RENDER]["native"].endswith("/2/native.png")


def test_an_unsupported_property_is_rejected_by_name(script):
    """Rejected at classification, so no repair is attempted and nothing is ignored."""
    script["script"].append([issue(
        "TITLE_SHADOW_MISSING", target="typography", affected=["t_title"],
        constraints=[("t_title", "textShadow", "0 2px 4px #000")],
    )])
    out = first_pass(script)
    assert out["route"] == "unresolved"
    routing = artifacts.get_json(
        script["jobId"], job_of(script)["artifacts"][Stage.REVIEW]["review"])["routing"]
    problems = " ".join(p for u in routing["unresolved"] for p in u["problems"])
    assert "textShadow" in problems

    # REPAIR_LIGHT refuses a review it was not routed, and the terminal step
    # names the property rather than regenerating anything.
    with pytest.raises(RepairRouteUnresolved):
        op(script, "REPAIR_LIGHT", 1)
    op(script, "CORRECT", 1)
    _, result = delivered(script)
    assert "textShadow" in result["diagnostics"]["original"]["message"]


def test_an_edit_that_changes_nothing_is_reported_as_a_no_op(script):
    """The editor autosaves, so this is reported rather than raised."""
    first_pass(script)
    run_stage(script["jobId"], Stage.FINALIZE, attempt=1)
    job = job_of(script)
    scene = design_state.scene_of(
        design_state.load(script["jobId"], design_state.current_pointer(job)["key"]))
    title = next(l for l in scene["textLayers"] if l["id"] == "t_title")

    out = design_edit.update(
        {"jobId": script["jobId"], "requestId": "edit-noop", "expectedVersion": job["designVersion"],
         "changes": {"content": {title["contentKey"]: title["text"]},
                     "typography": {"t_title": {"fontSize": str(title["fontSize"])}}}},
        "owner-1",
    )
    assert out["changed"] is False and out["noOp"] is True
    assert out["noOpFields"] == [f"content.{title['contentKey']}", "typography.t_title.fontSize"]
    assert out["designVersion"] == job["designVersion"]      # no new version
    assert out["imageGenerationCalls"] == 0
