"""CLEAN_PHOTO is repainted under a mask, and overlay repairs start from the concept.

Both come from job e700e56e0acd46c2832efb224dc2f50a (see
tests/test_overlay_validation_regression.py). Measured against the stored concept:

* Unmasked, CLEAN_PHOTO came back zoomed 1.09-1.25x and shifted 90-270 px in all
  four samples, so the stacked layers no longer reproduced the concept and the
  fidelity check rejected them. Two stricter prompts did not help (0 of 6).
* Under a mask the same stage returned scale 1.00 and a 0.1 px shift.
* The overlay repair edited the rejected overlay without seeing the concept and
  was told to "remove" a panel that belonged, so both repairs made it worse.
"""

import io

import pytest
from PIL import Image

from lib import artifacts, edit_mask, jobs, validate_assets as va
from lib.errors import AssetInvalid, InvalidTransparency, OverlayRepairNeeded, Stage
from lib.providers import image as image_provider
from lib.providers import vision as vision_provider
from lib.stages import separation
from tests.test_failure_paths import _repair, through
from tests.test_overlay_validation_regression import (
    CANVAS,
    GENERATED,
    SAMPLES,
    SCENE as REAL_SAMPLES,
    inverted_overlay,
    window_overlay,
)
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    fake_overlay,
    pipeline,
    png_bytes,
    run_stage,
    skip_external_probes,
)

SIZE = (GENERATED, GENERATED)

#: Layers of the job's scene that sit on the photograph: a title in the panel,
#: a contact line, and (invented here) an independent inset inside the window.
SCENE = {
    "canvas": CANVAS,
    "overlaySamples": REAL_SAMPLES["overlaySamples"],
    "textLayers": [
        {"x": 55, "y": 147, "width": 530, "height": 257},
        {"x": 141, "y": 832, "width": 220, "height": 37},
    ],
    "imageLayers": [
        {"type": "background", "x": 0, "y": 0, "width": 1080, "height": 1080},
        {"type": "decoration", "x": 0, "y": 0, "width": 1080, "height": 1080},
        {"type": "image", "x": 940, "y": 600, "width": 80, "height": 80},
    ],
}


def mask_alpha(scene=SCENE, overlay=None):
    overlay = overlay or window_overlay()
    data, fraction = edit_mask.build_clean_photo_mask(SIZE, png_bytes(overlay), scene)
    mask = Image.open(io.BytesIO(data))
    return mask, mask.getchannel("A"), fraction


def to_generated(box):
    """A 1080-canvas box as a 1024-image box."""
    k = GENERATED / CANVAS["width"]
    return tuple(round(v * k) for v in box)


def region_alpha(alpha, canvas_box):
    left, top, right, bottom = to_generated(canvas_box)
    return alpha.crop((left, top, right, bottom)).getextrema()


# --- the mask --------------------------------------------------------------


def test_the_mask_matches_the_concept_size_and_uses_the_provider_convention():
    mask, alpha, _ = mask_alpha()
    assert mask.size == SIZE
    assert mask.mode == "RGBA"
    # alpha 0 = repaint, 255 = keep: both must be present.
    assert alpha.getextrema() == (0, 255)


def test_the_visible_photograph_is_locked():
    """Window interior (the transparent sample regions) must be kept as is."""
    _, alpha, _ = mask_alpha()
    for index in (1, 2, 4):  # well inside the window, away from text and insets
        s = SAMPLES[index]
        box = (s["x"], s["y"], s["x"] + s["width"], s["y"] + s["height"])
        low, high = region_alpha(alpha, box)
        assert low == 255, f"region {index} would be repainted"


def test_everything_the_overlay_draws_is_repainted():
    """Panel pixels are not the photograph and must not survive into it."""
    _, alpha, _ = mask_alpha()
    for index in (6, 7, 9, 10, 11, 12):  # the opaque sample regions
        s = SAMPLES[index]
        box = (s["x"], s["y"], s["x"] + s["width"], s["y"] + s["height"])
        assert region_alpha(alpha, box) == (0, 0), f"region {index} would be kept"


def test_text_boxes_and_independent_insets_are_repainted_even_over_the_photo():
    """The overlay has no text, so only the scene says where text sat."""
    scene = {**SCENE, "textLayers": [{"x": 800, "y": 600, "width": 150, "height": 60}]}
    _, alpha, _ = mask_alpha(scene)
    assert region_alpha(alpha, (810, 610, 940, 650)) == (0, 0)
    # ...and the inset layer is repainted too.
    _, alpha, _ = mask_alpha()
    assert region_alpha(alpha, (950, 610, 1010, 670)) == (0, 0)


def test_artwork_is_grown_so_a_soft_edge_is_not_left_behind():
    """A pixel just outside the artwork's alpha cut-off is still repainted."""
    mask, alpha, _ = mask_alpha()
    overlay = window_overlay().convert("RGBA")
    # Find an artwork pixel on the window's left edge, then step outward into the
    # photo window by a couple of pixels.
    y = 600
    row = overlay.getchannel("A").crop((0, y, GENERATED, y + 1))
    edge = max(x for x in range(GENERATED) if row.getpixel((x, 0)) >= 16 and
               all(row.getpixel((xx, 0)) >= 16 for xx in range(x + 1)))
    assert alpha.getpixel((edge + 3, y)) == 0
    assert alpha.getpixel((min(GENERATED - 1, edge + 60), y)) == 255


def test_a_mask_for_a_different_sized_overlay_is_resampled_not_misaligned():
    """The overlay may be generated at a different size than the concept."""
    at_canvas = window_overlay(size=1080)
    mask, alpha, _ = mask_alpha(overlay=at_canvas)
    assert mask.size == SIZE
    low, _ = region_alpha(alpha, (880, 285, 1030, 435))
    assert low == 255


def test_the_overlays_pixels_are_not_part_of_the_mask():
    """Invariant 3: only the SHAPE of the overlay's alpha is used."""
    a = window_overlay()
    solid = lambda v: Image.new("L", a.size, v)  # noqa: E731
    b = Image.merge("RGBA", (solid(255), solid(0), solid(0), a.getchannel("A")))
    first, _, _ = mask_alpha(overlay=a)
    second, _, _ = mask_alpha(overlay=b)
    assert first.tobytes() == second.tobytes()


# --- the stage -------------------------------------------------------------


def test_clean_photo_without_an_overlay_fails_clearly_instead_of_running_unmasked(
    pipeline,
):
    through(pipeline, Stage.EXTRACT_SCENE)
    with pytest.raises(AssetInvalid, match="needs the decoration overlay"):
        run_stage(pipeline["jobId"], Stage.CLEAN_PHOTO)
    assert pipeline["calls"]["image"][-1]["operationId"].find("CLEAN_PHOTO") == -1


def test_clean_photo_edits_the_concept_never_the_overlay(pipeline):
    through(pipeline, Stage.EXTRACT_SCENE)
    run_stage(pipeline["jobId"], Stage.DECORATION_OVERLAY)
    run_stage(pipeline["jobId"], Stage.CLEAN_PHOTO)

    job = jobs.get_job_unscoped(pipeline["jobId"])
    concept = artifacts.get_bytes(pipeline["jobId"], jobs.approved_concept(job)["key"])
    overlay_bytes = artifacts.get_bytes(
        pipeline["jobId"], job["artifacts"][Stage.DECORATION_OVERLAY]["decoration"]
    )
    photo_call = pipeline["calls"]["image"][-1]
    assert photo_call["source"] == concept
    assert photo_call["source"] != overlay_bytes
    assert photo_call["mask"]
    assert "unmasked pixels" in photo_call["prompt"].lower()


# --- repairs ---------------------------------------------------------------


def _rebuild(job_id, attempt=1):
    import handler_stage

    event = {"jobId": job_id, "stage": Stage.CLEAN_PHOTO, "attempt": attempt,
             "assetId": None, "operation": "REBUILD_PHOTO"}
    return handler_stage.handler(event, None)["artifacts"]


def test_an_alpha_failure_records_where_and_what_for_the_repair(pipeline, monkeypatch):
    """Repair 2 of the job was given the previous round's stale vision text,
    because region failures were never recorded."""
    through(pipeline, Stage.CLEAN_PHOTO)
    job_id = pipeline["jobId"]

    # The fake pipeline's samples sit at 1024; make the overlay violate them.
    bad = png_bytes(Image.new("RGBA", (1024, 1024), (11, 37, 69, 254)))
    bad_key = artifacts.artifact_key(job_id, Stage.DECORATION_OVERLAY, 1, "bad.png")
    stored = artifacts.put_bytes(bad_key, bad, "image/png")
    jobs.record_artifacts(job_id, Stage.DECORATION_OVERLAY, 1,
                          {"decoration": stored["key"], "sha256": stored["sha256"]})

    with pytest.raises((OverlayRepairNeeded, InvalidTransparency)):
        run_stage(job_id, Stage.VALIDATE_ASSETS)
    defect = jobs.get_job_unscoped(job_id)["overlayDefect"]
    assert defect and "effectively opaque" in defect


def test_region_failures_are_described_with_location_and_intent():
    img = inverted_overlay()
    with pytest.raises(InvalidTransparency) as exc:
        va.validate_overlay(png_bytes(img), REAL_SAMPLES, CANVAS)
    defect = exc.value.defect
    assert "x=780, y=170 (250x80 px" in defect
    assert "fully transparent so the photograph shows through" in defect
    assert "solid graphic panel and must be fully opaque" in defect
    assert "region 0" not in defect  # numbers mean nothing to an image model


def test_the_repair_starts_from_the_concept_and_carries_the_full_rules(
    pipeline, monkeypatch
):
    through(pipeline, Stage.CLEAN_PHOTO)
    job_id = pipeline["jobId"]
    jobs.record_overlay_defect(job_id, "the two white contact badges are missing")

    seen = {}

    def capture(prompt, data, width, height, **kwargs):
        seen.update(prompt=prompt, data=data, kwargs=kwargs)
        return {"bytes": fake_overlay(), "mimeType": "image/png",
                "requestId": "r", "model": "m"}

    monkeypatch.setattr(image_provider, "edit", capture)
    jobs.bump_counter(job_id, "overlayRepairs", 2)
    _repair(job_id)

    job = jobs.get_job_unscoped(job_id)
    concept = artifacts.get_bytes(job_id, jobs.approved_concept(job)["key"])
    assert seen["data"] == concept, "a repair must not edit the rejected overlay"
    assert seen["kwargs"]["transparent"] is True
    # Every rule about what to keep still applies, plus what went wrong.
    assert "Icon badges" in seen["prompt"]
    assert "Solid, dark or textured background surfaces" in seen["prompt"]
    assert "the two white contact badges are missing" in seen["prompt"]
    assert "CORRECTION" in seen["prompt"]
    # The prompt is never told to remove what the finding says is missing.
    assert "REMOVE completely, leaving genuine transparency" not in seen["prompt"]


def test_a_repair_rebuilds_the_photo_around_the_repaired_overlay(
    pipeline, monkeypatch
):
    through(pipeline, Stage.CLEAN_PHOTO)
    job_id = pipeline["jobId"]
    jobs.record_overlay_defect(job_id, "a dark rectangle behind the ornaments")
    jobs.bump_counter(job_id, "overlayRepairs", 2)
    _repair(job_id)

    before = jobs.get_job_unscoped(job_id)["artifacts"][Stage.CLEAN_PHOTO][
        "background"
    ]
    rebuilt = _rebuild(job_id)

    assert rebuilt["background"] != before
    assert rebuilt["background"].endswith("photo-after-repair-1.png")
    # The first photo stays readable; the next validation reads the rebuilt one.
    assert artifacts.get_bytes(job_id, before)
    assert jobs.get_job_unscoped(job_id)["artifacts"][Stage.CLEAN_PHOTO][
        "background"
    ] == rebuilt["background"]
    photo_call = pipeline["calls"]["image"][-1]
    assert photo_call["mask"], "the rebuilt photo is masked too"
    run_stage(job_id, Stage.VALIDATE_ASSETS)


def test_the_first_overlay_prompt_keeps_badges_and_non_photographic_panels(pipeline):
    """Measured on the stored concept: without these two rules the overlay kept
    both contact badges 1 time in 4; with them 4 of 4 kept badges and the panel.
    A prompt that says only "icons" lets the model drop a white disc as 'text'."""
    through(pipeline, Stage.EXTRACT_SCENE)
    run_stage(pipeline["jobId"], Stage.DECORATION_OVERLAY)
    prompt = pipeline["calls"]["image"][-1]["prompt"]
    assert "Icon badges" in prompt and "never the badge" in prompt
    assert "Solid, dark or textured background surfaces" in prompt
    assert "fully opaque, edge to edge" in prompt
