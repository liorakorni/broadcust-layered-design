"""Readability gradients are measured from the concept, not assumed.

Regression for job cbf8474e: the cleaned photograph came back brighter than the
concept behind the copy, the shading was declared "owned by the photo", and the
first repair then added a fixed 648px band that happened to be neither the right
width nor the right strength.
"""

import copy
import io
import json
import pathlib

import pytest
from PIL import Image

from lib import artifacts, readability, scene_patch, schemas
from lib.errors import Stage
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    pipeline, run_stage, skip_external_probes,
)
from tests.test_repair_pipeline import (  # noqa: F401
    FIRST_PASS, image_calls, job_of, review_pass, script,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "job_cbf8474e"
SIZE = 128


def png(pixel, mode="L"):
    """An image from a function (x, y) -> grey level 0..255 (or an RGBA tuple)."""
    image = Image.new(mode, (SIZE, SIZE))
    for y in range(SIZE):
        for x in range(SIZE):
            image.putpixel((x, y), pixel(x, y))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def flat(level):
    return png(lambda x, y: level)


def scene_with_text(x, width, *, color="#FFFFFF", canvas=1000):
    return {
        "canvas": {"width": canvas, "height": canvas},
        "textLayers": [{"id": "t", "x": x, "y": 200, "width": width, "height": 500,
                        "color": color, "contentKey": "k", "text": "x"}],
        "shapeLayers": [],
        "imageLayers": [{"id": "bg", "type": "background", "zIndex": 0, "x": 0, "y": 0,
                         "width": canvas, "height": canvas}],
        "effects": [],
    }


def dark_field(edge, extent):
    """A concept with a black field fading out `extent` of the way in from `edge`."""
    def level(x, y):
        distance = {"left": x, "right": SIZE - 1 - x, "top": y, "bottom": SIZE - 1 - y}[edge]
        fraction = distance / (SIZE * extent)
        return int(30 + 120 * min(1.0, max(0.0, fraction)))
    return png(level)


# --- the fit follows the pixels ------------------------------------------------------


def test_missing_shading_is_fitted_to_the_edge_the_concept_darkens():
    scene = scene_with_text(60, 400)
    fit = readability.fit(dark_field("left", 0.6), flat(150), scene)

    assert fit["action"] == "add" and fit["edge"] == "left"
    assert fit["angle"] == 90 and fit["color"] == "#000000"
    first, *_, last = fit["stops"]
    assert first["opacity"] >= 0.7 and last["opacity"] == 0.0
    assert [s["position"] for s in fit["stops"]] == sorted(s["position"] for s in fit["stops"])


@pytest.mark.parametrize("edge,angle", [("left", 90), ("right", 270), ("top", 180), ("bottom", 0)])
def test_the_gradient_starts_at_the_edge_that_needs_it(edge, angle):
    horizontal = edge in ("left", "right")
    text = {"left": (60, 400), "right": (540, 400)}.get(edge)
    scene = scene_with_text(*(text or (200, 600)))
    if not horizontal:
        scene["textLayers"][0].update(
            y=60 if edge == "top" else 540, height=400, x=100, width=800)
    fit = readability.fit(dark_field(edge, 0.6), flat(150), scene)

    assert fit["edge"] == edge and fit["angle"] == angle
    # CSS: the first stop is the one nearest the edge, where the field is darkest.
    assert fit["stops"][0]["opacity"] > fit["stops"][-1]["opacity"]


def test_the_band_follows_the_concepts_fade_not_a_fixed_share_of_the_canvas():
    scene = scene_with_text(40, 400)
    short = readability.fit(dark_field("left", 0.3), flat(150), scene)
    long = readability.fit(dark_field("left", 0.7), flat(150), scene)

    assert short["box"]["width"] < long["box"]["width"]
    assert 600 not in (short["box"]["width"], long["box"]["width"])


def test_a_fade_never_runs_far_beyond_the_copy_it_serves():
    narrow = readability.fit(dark_field("left", 0.9), flat(150), scene_with_text(40, 150))

    reach = readability.band_for(scene_with_text(40, 150), "left")
    assert narrow["box"]["width"] <= reach + 1000 / readability.GRID   # one grid cell of rounding
    assert reach < 0.4 * 1000                   # 40 + 150 plus a margin, not 60%


def test_shading_the_photograph_still_carries_is_not_added_a_second_time():
    scene = scene_with_text(60, 400)
    concept = dark_field("left", 0.6)

    fit = readability.fit(concept, concept, scene)

    assert fit["action"] == "none"
    assert "already carries" in fit["reason"]


def test_half_lost_shading_is_only_half_replaced():
    """The difference is what is added, so a partially retained field does not
    become a doubled one."""
    scene = scene_with_text(60, 400)
    concept = dark_field("left", 0.6)
    half = png(lambda x, y: int((30 + 120 * min(1.0, x / (SIZE * 0.6)) + 150) / 2))
    full = readability.fit(concept, flat(150), scene)
    partial = readability.fit(concept, half, scene)

    assert partial["action"] in ("add", "none")
    if partial["action"] == "add":
        assert partial["stops"][0]["opacity"] < full["stops"][0]["opacity"]


def test_dark_copy_on_a_light_field_gets_a_light_gradient():
    scene = scene_with_text(60, 400, color="#111111")
    # Lighter than the photograph near the left edge, fading to the photograph.
    concept = png(lambda x, y: 120 + int(135 * max(0.0, 1 - x / (SIZE * 0.6))))
    fit = readability.fit(concept, flat(120), scene)

    assert fit["action"] == "add" and fit["color"] == "#FFFFFF"
    assert all(s["color"] == "#FFFFFF" for s in fit["stops"])


def test_artwork_the_overlay_already_paints_is_not_read_as_missing_shading():
    scene = scene_with_text(60, 400)
    overlay = png(lambda x, y: (0, 0, 0, 255 if x < SIZE * 0.7 else 0), mode="RGBA")

    fit = readability.fit(dark_field("left", 0.6), flat(150), scene, overlay)

    assert fit["action"] == "none"


def test_copy_over_a_button_does_not_pull_the_region_off_the_photograph():
    scene = scene_with_text(60, 400)
    scene["shapeLayers"] = [{"id": "cta", "kind": "panel", "x": 600, "y": 800, "width": 300,
                             "height": 100}]
    scene["textLayers"].append({"id": "label", "x": 620, "y": 810, "width": 260, "height": 80,
                                "color": "#000000", "contentKey": "c", "text": "y"})

    region = readability.text_region(scene)

    assert region["x"] + region["width"] == 460        # the label is not part of it


# --- the real failing job --------------------------------------------------------------


def test_the_failing_job_gets_its_gradient_at_the_first_reconstruction():
    concept = (FIXTURES / "concept.png").read_bytes()
    photo = (FIXTURES / "photo.png").read_bytes()
    overlay = (FIXTURES / "overlay.png").read_bytes()
    scene = json.loads((FIXTURES / "scene-initial.json").read_text())

    fit = readability.fit(concept, photo, scene, overlay)

    # What the concept showed: near-black behind the copy at the left, fading into
    # the photograph; not a fixed 648px preset.
    assert fit["action"] == "add" and fit["edge"] == "left"
    assert fit["stops"][0]["opacity"] >= 0.8 and fit["stops"][-1]["opacity"] == 0.0
    region = readability.text_region(scene)
    assert fit["box"]["width"] >= 0.9 * (region["x"] + region["width"])
    assert fit["box"]["height"] == scene["canvas"]["height"]
    assert fit["measured"]["residual"] < 0.1       # the fit reproduces the concept

    patched = scene_patch.add_shape(scene, "gradient-left", take_over=True, fitted=fit)
    shape = next(s for s in patched["shapeLayers"] if s["kind"] == "gradient")
    # The effect the model called photo-owned is now drawn once, by the layout,
    # under the same id; nothing else was renumbered.
    assert shape["effect"] == "effect-photo-darkening"
    owners = [e["owner"] for e in patched["effects"] if e["kind"] == "readabilityGradient"]
    assert owners == ["layout"]
    assert [s["id"] for s in patched["shapeLayers"][:-1]] == \
        [s["id"] for s in scene["shapeLayers"]]
    schemas.parse_scene(
        patched, scene["canvas"]["width"], scene["canvas"]["height"],
        allowed_content_keys={t["contentKey"] for t in patched["textLayers"]},
        allowed_fonts={t["fontFamily"] for t in patched["textLayers"]},
    )


def test_a_fitted_gradient_is_drawn_from_the_stops_that_were_measured():
    scene = json.loads((FIXTURES / "scene-initial.json").read_text())
    fit = {
        "edge": "left", "angle": 90, "box": {"x": 0, "y": 0, "width": 700, "height": 1080},
        "stops": [{"color": "#000000", "opacity": 0.9, "position": 0.0},
                  {"color": "#000000", "opacity": 0.4, "position": 0.5},
                  {"color": "#000000", "opacity": 0.0, "position": 1.0}],
        "measured": {"edgeAlpha": 0.9},
    }

    patched = scene_patch.add_shape(copy.deepcopy(scene), "gradient-left",
                                    take_over=True, fitted=fit)
    shape = patched["shapeLayers"][-1]

    assert (shape["width"], shape["angle"]) == (700, 90)
    assert [s["opacity"] for s in shape["stops"]] == [0.9, 0.4, 0.0]


# --- it is in the first reconstruction ------------------------------------------------------


def test_the_first_assembled_html_already_has_the_fitted_gradient(script, monkeypatch):
    fit = {
        "action": "add", "edge": "left", "angle": 90, "color": "#000000",
        "box": {"x": 0, "y": 0, "width": 700, "height": 1024},
        "stops": [{"color": "#000000", "opacity": 0.9, "position": 0.0},
                  {"color": "#000000", "opacity": 0.5, "position": 0.5},
                  {"color": "#000000", "opacity": 0.0, "position": 1.0}],
        "measured": {"edgeAlpha": 0.9, "residual": 0.03},
    }
    monkeypatch.setattr(readability, "fit", lambda *a, **k: copy.deepcopy(fit))

    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)
    paid = image_calls(script)
    review_pass(script, 1)

    job = job_of(script)
    validation = artifacts.get_json(
        script["jobId"], job["artifacts"][Stage.VALIDATE_ASSETS]["validation"])
    assert validation["codeOwned"]["action"] == "added"
    assert validation["codeOwned"]["imageGenerationCalls"] == 0
    html = artifacts.get_bytes(
        script["jobId"], job["artifacts"][Stage.ASSEMBLE]["html"]).decode()
    assert html.count('data-effect="readabilityGradient"') == 1
    scene = artifacts.get_json(script["jobId"], job["artifacts"][Stage.ASSEMBLE]["scene"])
    shape = next(s for s in scene["shapeLayers"] if s["kind"] == "gradient")
    assert (shape["width"], shape["angle"]) == (700, 90)
    assert [e["owner"] for e in scene["effects"] if e["kind"] == "readabilityGradient"] == ["layout"]
    assert image_calls(script) == paid          # fitting, assembling and rendering cost no image
    assert (job.get("metrics") or {}).get("fittedGradients") == 1


def test_a_fit_that_finds_nothing_missing_leaves_the_scene_alone(script, monkeypatch):
    monkeypatch.setattr(readability, "fit", lambda *a, **k: {
        "action": "none", "reason": "the cleaned photograph already carries the shading",
        "measured": {"edgeAlpha": 0.03}})

    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)

    job = job_of(script)
    validation = artifacts.get_json(
        script["jobId"], job["artifacts"][Stage.VALIDATE_ASSETS]["validation"])
    assert validation["codeOwned"] is None
    assert validation["readability"]["action"] == "not-needed"
    assert (job.get("metrics") or {}).get("fittedGradients") is None


def test_a_fit_that_crashes_never_fails_the_job(script, monkeypatch):
    def crash(*a, **k):
        raise ValueError("unreadable image")

    monkeypatch.setattr(readability, "fit", crash)
    for stage in FIRST_PASS:
        run_stage(script["jobId"], stage, attempt=1)

    job = job_of(script)
    validation = artifacts.get_json(
        script["jobId"], job["artifacts"][Stage.VALIDATE_ASSETS]["validation"])
    assert validation["readability"]["action"] == "failed"


# --- a dark photograph (measured live) ----------------------------------------------------


def textured(level_of_shadow, highlight):
    """A photograph with deep shadows and bright detail, in a fine checker."""
    return lambda x, y: highlight if (x // 2 + y // 2) % 2 else level_of_shadow


def test_a_dark_photograph_with_bright_detail_still_needs_the_concept_s_black_field():
    """Measured on a rebuilt design: the concept's field behind the copy was black, the
    cleaned photograph's shadows were nearly as dark, and a low-percentile comparison read
    that as 'almost no shading' - a 0.4 fade where 0.9 was needed, and a reviewer who could
    still see the gym through the copy. Means over the pixels that are not copy see the
    highlights the concept removed."""
    photograph = png(textured(8, 70))
    concept = png(lambda x, y: 0)
    scene = scene_with_text(0, 600)

    fit = readability.fit(concept, photograph, scene)

    assert fit["action"] == "add"
    assert fit["measured"]["edgeAlpha"] >= 0.85
    assert fit["measured"]["textMeanAlpha"] >= 0.85


def test_copy_pixels_in_the_concept_are_not_read_as_a_brighter_background():
    """White text on the concept's black field: the text must not cancel the shading."""
    def with_text(x, y):
        return 255 if 20 <= x < 100 and (y // 8) % 2 == 0 else 0

    fit = readability.fit(png(with_text), png(textured(10, 80)), scene_with_text(0, 600))

    assert fit["action"] == "add" and fit["measured"]["edgeAlpha"] >= 0.85


def test_a_photograph_that_already_carries_the_shading_still_gets_none():
    same = png(textured(5, 40))
    assert readability.fit(same, same, scene_with_text(0, 600))["action"] == "none"


def test_a_column_of_copy_on_the_right_gets_its_gradient_from_the_right_not_the_top():
    """Measured live: the concept's black field also darkened its top rows as much as its
    right ones, the strongest edge won the tie, and the design got a top-down gradient
    behind a right-hand column - which the reviewer called misdirected."""
    def concept_level(x, y):
        return 0 if (x > SIZE * 0.45 or y < SIZE * 0.25) else 150

    scene = scene_with_text(560, 380)          # a tall column on the right
    fit = readability.fit(png(concept_level), flat(150), scene)

    assert fit["action"] == "add" and fit["edge"] == "right" and fit["angle"] == 270


def test_the_strongest_edge_still_wins_when_the_preferred_one_is_clearly_weaker():
    def concept_level(x, y):
        return 0 if y < SIZE * 0.4 else 150    # only the top is dark

    scene = scene_with_text(560, 380)
    fit = readability.fit(png(concept_level), flat(150), scene)

    assert fit["action"] == "none" or fit["edge"] == "top"
