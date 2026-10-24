"""Regression for job e700e56e0acd46c2832efb224dc2f50a (designFreedom 0).

What actually happened, established from the stored artifacts:

* The FIRST overlay was correct. It passed all 13 alpha sample regions, and
  stacked on the clean photograph it reproduced the concept's artwork.
* The vision composite inspection nevertheless rejected it, reporting the
  grainy black background panel as "photographic scratched texture". That panel
  is intended artwork: ANALYZE_REFERENCE recorded it as the `graphicPanel`
  anchor "textured-black-background" and REVIEW_CONCEPT approved it.
* The repair was then told to remove that "texture", so repair 1 inverted the
  alpha (panel transparent, photo window opaque) and repair 2 erased the panel
  and hazed everything else. Both were correctly rejected by the alpha checks;
  the alpha validator was never the faulty part.

So the fix is in the inspection (it now sees the approved concept), and these
tests pin down both halves: the deterministic alpha geometry the job
exposed, and the contract of the vision inspection that failed.

The sample regions below are the 13 from that job's scene.json, verbatim, on the
same 1080 canvas, against images generated at 1024 like the provider emits.
"""

import io

import pytest
from PIL import Image, ImageDraw, ImageFilter

from lib import artifacts, jobs, validate_assets as va
from lib.errors import InvalidTransparency, OverlayRepairNeeded, Stage
from lib.providers import vision as vision_provider
from lib.stages import validate as validate_stage
from tests.test_failure_paths import _repair, through
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    fake_overlay,
    pipeline,
    png_bytes,
    run_stage,
    skip_external_probes,
)

CANVAS = {"width": 1080, "height": 1080}
GENERATED = 1024  # what the image provider returns for a square canvas

MODEL_OPAQUE = 254

SAMPLES = [
    {"kind": "transparent", "x": 780, "y": 170, "width": 250, "height": 80},
    {"kind": "transparent", "x": 880, "y": 285, "width": 150, "height": 150},
    {"kind": "transparent", "x": 735, "y": 480, "width": 150, "height": 140},
    {"kind": "transparent", "x": 650, "y": 685, "width": 160, "height": 110},
    {"kind": "transparent", "x": 860, "y": 850, "width": 170, "height": 170},
    {"kind": "transparent", "x": 575, "y": 1010, "width": 160, "height": 50},
    {"kind": "opaque", "x": 440, "y": 20, "width": 580, "height": 45},
    {"kind": "opaque", "x": 175, "y": 105, "width": 360, "height": 30},
    {"kind": "opaque", "x": 610, "y": 230, "width": 40, "height": 130},
    {"kind": "opaque", "x": 50, "y": 650, "width": 450, "height": 35},
    {"kind": "opaque", "x": 15, "y": 715, "width": 40, "height": 60},
    {"kind": "opaque", "x": 160, "y": 955, "width": 280, "height": 60},
    {"kind": "opaque", "x": 35, "y": 1045, "width": 380, "height": 25},
]
SCENE = {"overlaySamples": SAMPLES}

#: The photo window of the concept, in 1024-space: a slanted left edge running
#: from near the top down to the bottom, filling the right of the canvas.
WINDOW = [(705, 140), (1024, 140), (1024, 1024), (515, 1024)]


def window_overlay(*, size=GENERATED, shift=0, haze=None, drop=None):
    """The layout of the job's concept: opaque panel, transparent photo window.

    `shift` slides the window sideways, `haze` fills the panel with a faint
    translucent alpha instead of solid, `drop` removes a box of artwork.
    """
    scale = size / GENERATED
    mask = Image.new("L", (size, size), haze if haze is not None else MODEL_OPAQUE)
    draw = ImageDraw.Draw(mask)
    draw.polygon(
        [((x + shift) * scale, y * scale) for x, y in WINDOW], fill=0
    )
    if drop:
        draw.rectangle([v * scale for v in drop], fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(3 * scale))
    if haze is None:
        # The blur pulls the interior just under 254; clamp it back like the
        # model's real output.
        mask = mask.point(lambda a: MODEL_OPAQUE if a >= 250 else a)
    img = Image.new("RGBA", (size, size), (12, 12, 12, 0))
    img.putalpha(mask)
    return img


def inverted_overlay():
    """Repair 1 of the job: panel gone, photo window filled in, orange kept."""
    img = window_overlay()
    alpha = img.getchannel("A").point(lambda a: 254 - a)
    draw = ImageDraw.Draw(alpha)
    for index in (6, 10, 12):  # the orange strips survived in the real repair
        s = SAMPLES[index]
        k = GENERATED / CANVAS["width"]
        draw.rectangle(
            [s["x"] * k - 2, s["y"] * k - 2,
             (s["x"] + s["width"]) * k + 2, (s["y"] + s["height"]) * k + 2],
            fill=MODEL_OPAQUE,
        )
    img.putalpha(alpha)
    return img


def failing_regions(img):
    rgba, _ = va.load_rgba(png_bytes(img))
    rgba = va.resample_to_canvas(va.normalize_alpha(rgba), CANVAS)
    failures = va.check_sample_regions(rgba, SCENE, CANVAS)
    return sorted(int(f.split()[1]) for f in failures)


# --- the alpha validator, against the job's real geometry -------------------


def test_a_valid_overlay_passes_all_thirteen_regions_at_a_mismatched_size():
    """1024 px image, 1080 canvas: regions are in canvas pixels, so the image
    must be resampled to the canvas before they are cropped."""
    img = window_overlay()
    assert img.size != (CANVAS["width"], CANVAS["height"])
    assert failing_regions(img) == []
    stats = va.validate_overlay(png_bytes(img), SCENE, CANVAS)
    assert stats["transparent"] > 0.2 and stats["opaque"] > 0.4


def test_the_same_artwork_gives_the_same_verdict_at_either_resolution():
    """The validator maps both sizes onto the canvas the same way."""
    at_generated = failing_regions(window_overlay(size=1024))
    at_canvas = failing_regions(window_overlay(size=1080))
    assert at_generated == at_canvas == []


def test_artwork_that_is_misaligned_is_rejected_not_excused():
    """A window slid 150 px left leaves a strip of panel along the right edge
    where the photograph must show through."""
    failed = failing_regions(window_overlay(shift=-150))
    assert {0, 1, 4} <= set(failed), failed
    # Regions far from the moved edge are unaffected, so this is alignment being
    # measured rather than everything failing.
    assert 6 not in failed and 12 not in failed


def test_the_inverted_repair_from_the_job_is_rejected():
    """Repair 1: every transparent region opaque, panel regions transparent."""
    failed = failing_regions(inverted_overlay())
    assert failed == [0, 1, 2, 3, 4, 5, 7, 8, 9, 11]


def test_a_hazed_panel_is_rejected_as_not_opaque():
    """Repair 2: the panel survives only as a faint haze (alpha ~16)."""
    failed = failing_regions(window_overlay(haze=16))
    assert {7, 8, 9, 11} <= set(failed)


def test_required_artwork_missing_is_rejected():
    """The top orange strip (region 6) is part of the design; dropping it fails
    that region and no other."""
    failed = failing_regions(window_overlay(drop=(360, 0, 1024, 90)))
    assert failed == [6]


@pytest.mark.parametrize(
    "alpha, counts_as",
    [(15, "transparent"), (16, None), (249, None), (250, "opaque")],
)
def test_the_thresholds_the_failure_messages_are_measured_with(alpha, counts_as):
    """'N% transparent' counts alpha < 16; 'N% opaque' counts alpha >= 250, over
    every pixel of the region (denominator: region area after resampling)."""
    img = Image.new("RGBA", (1080, 1080), (0, 0, 0, alpha))
    box = {"x": 100, "y": 100, "width": 50, "height": 50}
    region = va._clip(box, CANVAS)
    histogram = img.crop(region).getchannel("A").histogram()
    total = sum(histogram)
    transparent = sum(histogram[: va.TRANSPARENT_THRESHOLD]) / total
    opaque = sum(histogram[va.OPAQUE_THRESHOLD :]) / total
    assert (transparent == 1.0) == (counts_as == "transparent")
    assert (opaque == 1.0) == (counts_as == "opaque")
    assert total == 50 * 50


# --- the vision inspection that actually failed the job ---------------------


def _capture_inspection(monkeypatch, verdict):
    seen = {}

    def analyze(prompt, images=(), *, schema_name, **kwargs):
        if schema_name != "overlay_inspection_v1":
            seen["other"] = schema_name
            return {"displaced": False, "description": ""}
        seen["prompt"] = prompt
        seen["images"] = list(images)
        return verdict

    monkeypatch.setattr(vision_provider, "analyze", analyze)
    return seen


CLEAN = {"photographicLeftovers": False, "textLeftovers": False,
         "artworkPresent": True, "notes": "", "leftoverDescription": ""}


def test_the_inspection_is_given_the_approved_concept_first(pipeline, monkeypatch):
    """Without the concept the model cannot tell a grainy graphic panel from a
    photograph - which is exactly how the job's correct overlay was rejected."""
    through(pipeline, Stage.CLEAN_PHOTO)
    seen = _capture_inspection(monkeypatch, CLEAN)
    # Only the composite inspection is under test here; let fidelity pass too.
    run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)

    job = jobs.get_job_unscoped(pipeline["jobId"])
    concept = artifacts.get_bytes(pipeline["jobId"], jobs.approved_concept(job)["key"])
    assert len(seen["images"]) == 4
    assert seen["images"][0] == validate_stage._thumbnail_png(concept)
    # The other three are the composites, not the concept again.
    assert len(set(seen["images"])) == 4


def test_the_inspection_prompt_treats_textured_graphic_panels_as_artwork(
    pipeline, monkeypatch
):
    through(pipeline, Stage.CLEAN_PHOTO)
    seen = _capture_inspection(monkeypatch, CLEAN)
    run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)

    prompt = seen["prompt"].lower()
    assert "image 1" in prompt and "approved concept" in prompt
    # The rule that was missing: texture on a graphic surface is not photography.
    assert "grain" in prompt and "scratch" in prompt
    assert "not a photographic scene" in prompt
    # ...and the repair must never be told to remove a panel that image 1 shows.
    assert "never list a graphic panel" in prompt


def test_an_overlay_with_unwanted_photography_is_rejected(pipeline, monkeypatch):
    through(pipeline, Stage.CLEAN_PHOTO)
    _capture_inspection(
        monkeypatch,
        {**CLEAN, "photographicLeftovers": True,
         "leftoverDescription": "a rectangular street photograph inside the panel"},
    )
    with pytest.raises(OverlayRepairNeeded, match="photographic scene remains"):
        run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)
    # What the repair is told to remove is what the inspection actually saw.
    assert "street photograph" in jobs.get_job_unscoped(pipeline["jobId"])[
        "overlayDefect"
    ]


def test_an_overlay_with_no_artwork_is_rejected(pipeline, monkeypatch):
    through(pipeline, Stage.CLEAN_PHOTO)
    _capture_inspection(monkeypatch, {**CLEAN, "artworkPresent": False})
    with pytest.raises(OverlayRepairNeeded, match="no decorative artwork"):
        run_stage(pipeline["jobId"], Stage.VALIDATE_ASSETS)


# --- validation reads the latest repair, not the overlay it replaced --------


def test_validation_reads_the_latest_repair_not_the_original(pipeline, monkeypatch):
    """The original passes every alpha check. The repair is bad. If validation
    were reading the original or a cached copy it would pass; it must fail on the
    repair's bytes, then pass once a good repair replaces it."""
    from lib.providers import image as image_provider

    job_id = pipeline["jobId"]
    through(pipeline, Stage.CLEAN_PHOTO)

    flagged = {"on": True}

    def analyze(prompt, images=(), *, schema_name, **kwargs):
        if schema_name == "overlay_inspection_v1":
            on = flagged["on"]
            flagged["on"] = False
            return {**CLEAN, "textLeftovers": on,
                    "leftoverDescription": "a word in the banner" if on else ""}
        return {"displaced": False, "description": ""}

    monkeypatch.setattr(vision_provider, "analyze", analyze)

    # 1. The first overlay is rejected by inspection, which requests a repair.
    with pytest.raises(OverlayRepairNeeded):
        run_stage(job_id, Stage.VALIDATE_ASSETS)
    original_key = jobs.get_job_unscoped(job_id)["artifacts"][
        Stage.DECORATION_OVERLAY
    ]["decoration"]

    # 2. The repair comes back wrong: the panel is gone (transparent).
    def bad_repair(prompt, data, width, height, **kwargs):
        empty_panel = Image.new("RGBA", (1024, 1024), (11, 37, 69, 0))
        return {"bytes": png_bytes(empty_panel), "mimeType": "image/png",
                "requestId": "repair-1", "model": "m"}

    monkeypatch.setattr(image_provider, "edit", bad_repair)
    repaired = _repair(job_id)
    assert repaired["decoration"] != original_key
    assert jobs.get_job_unscoped(job_id)["artifacts"][Stage.DECORATION_OVERLAY][
        "decoration"
    ] == repaired["decoration"]

    # Fails on the repair's content, not on anything from the original.
    with pytest.raises(InvalidTransparency) as exc:
        run_stage(job_id, Stage.VALIDATE_ASSETS)
    assert "effectively" in exc.value.message or "region" in exc.value.message
    # The rejected original is still readable (artifacts are append-only).
    assert artifacts.get_bytes(job_id, original_key)

    # 3. A good second repair replaces it and the next validation reads that one.
    def good_repair(prompt, data, width, height, **kwargs):
        return {"bytes": fake_overlay(), "mimeType": "image/png",
                "requestId": "repair-2", "model": "m"}

    monkeypatch.setattr(image_provider, "edit", good_repair)
    second = _repair(job_id)
    assert second["decoration"] not in (original_key, repaired["decoration"])
    run_stage(job_id, Stage.VALIDATE_ASSETS)
