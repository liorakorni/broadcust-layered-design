"""Contact pictograms become independent transparent images.

Per the agreed approach: one transparent PNG per pictogram, placed through the
existing `.badges` structure, so each contact row is three independent layers -
a CSS circle, the pictogram, and the value as text. The pictogram is cut out of
the approved concept arithmetically, which keeps the icon the customer approved
and costs no image call; one bounded generation is the fallback; failing both is
a blocking failure, because an icon that cannot be separated is missing
editability and must not be left inside the artwork.

Editing the glyph's internal strokes is out of scope here: that needs the
editor's native icon mechanism, which is deferred.
"""

import copy
import io

import pytest
from PIL import Image, ImageDraw

from lib import artifacts, assemble_html, icon_extract, jobs
from lib.errors import ImpossibleContentRequirement, Stage
from lib.providers import image as image_provider
from tests import test_pipeline as base
from tests.test_clean_photo_mask import decompose
from tests.test_component_plan import decomposed
from tests.test_failure_paths import through
from tests.test_pipeline import (  # noqa: F401  (fixtures are used by pytest)
    CANVAS,
    pipeline,
    png_bytes,
    run_stage,
    skip_external_probes,
)

CIRCLE = {"x": 80, "y": 900, "width": 64, "height": 64}
GLYPH = {"x": 96, "y": 916, "width": 32, "height": 32}


def concept(circle="#FFD43B", glyph="#102030", textured=False, empty=False):
    """A synthetic concept: noisy photograph, flat badge, dark pictogram."""
    image = Image.new("RGB", (1024, 1024))
    for x in range(0, 1024, 4):                      # cheap photographic noise
        for y in range(0, 1024, 4):
            image.paste((x % 256, y % 256, (x + y) % 256), (x, y, x + 4, y + 4))
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (CIRCLE["x"], CIRCLE["y"], CIRCLE["x"] + CIRCLE["width"],
         CIRCLE["y"] + CIRCLE["height"]),
        fill=circle,
    )
    if textured:
        # A circle that is not flat: every pixel a different colour, so no
        # background colour exists to subtract.
        for x in range(CIRCLE["x"], CIRCLE["x"] + CIRCLE["width"]):
            for y in range(CIRCLE["y"], CIRCLE["y"] + CIRCLE["height"]):
                image.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, (x * y) % 256))
    if not empty:
        draw.rectangle(
            (GLYPH["x"] + 6, GLYPH["y"] + 2, GLYPH["x"] + 14, GLYPH["y"] + 30),
            fill=glyph,
        )
        draw.rectangle(
            (GLYPH["x"] + 6, GLYPH["y"] + 22, GLYPH["x"] + 26, GLYPH["y"] + 30),
            fill=glyph,
        )
    return png_bytes(image)


# --- the extraction itself ----------------------------------------------------


def test_a_flat_circle_icon_is_extracted_without_a_model_call():
    data, fraction = icon_extract.extract(concept(), GLYPH, CANVAS, "#FFD43B")

    assert icon_extract.looks_like_a_glyph(fraction)
    with Image.open(io.BytesIO(data)) as out:
        assert out.mode == "RGBA"
        alpha = out.getchannel("A")
        # The glyph survived, the circle behind it did not.
        assert alpha.getextrema() == (0, 255)
        corners = [alpha.getpixel(p) for p in
                   ((0, 0), (out.width - 1, 0), (0, out.height - 1))]
        assert corners == [0, 0, 0]
        # Sharp enough for the canvas: at least twice the size it renders at.
        assert min(out.size) >= GLYPH["width"] * icon_extract.MIN_SCALE


def test_the_circle_colour_is_found_without_being_told():
    """A scene may pair no shape with the icon; the crop still has a background."""
    told, _ = icon_extract.extract(concept(), GLYPH, CANVAS, "#FFD43B")
    guessed, fraction = icon_extract.extract(concept(), GLYPH, CANVAS, None)
    assert icon_extract.looks_like_a_glyph(fraction)
    assert guessed == told


def test_an_empty_crop_does_not_look_like_a_glyph():
    _, fraction = icon_extract.extract(concept(empty=True), GLYPH, CANVAS, "#FFD43B")
    assert not icon_extract.looks_like_a_glyph(fraction)


def test_a_textured_badge_does_not_look_like_a_glyph():
    """Nothing is subtractable, so almost everything survives: not a pictogram."""
    _, fraction = icon_extract.extract(concept(textured=True), GLYPH, CANVAS, "#FFD43B")
    assert fraction > icon_extract.MAX_GLYPH_FRACTION
    assert not icon_extract.looks_like_a_glyph(fraction)


def test_a_solid_png_is_never_mistaken_for_an_isolated_glyph():
    solid = png_bytes(Image.new("RGB", (64, 64), "#FFD43B"))
    assert icon_extract.glyph_fraction(solid) == 1.0
    assert not icon_extract.looks_like_a_glyph(icon_extract.glyph_fraction(solid))


# --- the stage ----------------------------------------------------------------


def icon_scene():
    """The decomposed fixture scene, with the phone icon where the concept has it."""
    doc = decomposed()
    circle = next(s for s in doc["shapeLayers"] if s["id"] == "s_phone_circle")
    circle.update(CIRCLE)
    icon = next(l for l in doc["imageLayers"] if l["id"] == "i_phone")
    icon.update(GLYPH)
    return doc


def prepared(pipeline, monkeypatch, **concept_kwargs):
    monkeypatch.setattr(base, "fake_photo", lambda: concept(**concept_kwargs))
    through(pipeline, Stage.EXTRACT_SCENE)
    decompose(pipeline["jobId"], icon_scene())
    return pipeline["jobId"]


def test_the_stage_extracts_the_icon_with_no_image_generation(pipeline, monkeypatch):
    job_id = prepared(pipeline, monkeypatch)
    before = len(pipeline["calls"]["image"])

    out = run_stage(job_id, Stage.INDEPENDENT_ASSETS, asset_id="icon-phone")

    assert out["source"] == "extracted"
    assert len(pipeline["calls"]["image"]) == before, "no paid call for an icon"
    assert jobs.get_job_unscoped(job_id)["metrics"].get("imageCalls", 0) == before
    stored = artifacts.get_bytes(job_id, out["key"])
    assert icon_extract.looks_like_a_glyph(icon_extract.glyph_fraction(stored))


def test_a_textured_circle_falls_back_to_one_generation(pipeline, monkeypatch):
    job_id = prepared(pipeline, monkeypatch, textured=True)
    before = len(pipeline["calls"]["image"])
    # The fallback returns a real isolated glyph.
    glyph = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(glyph).rectangle((20, 10, 44, 54), fill=(16, 32, 48, 255))
    monkeypatch.setattr(
        image_provider, "edit",
        lambda prompt, source, w, h, **kw: pipeline["calls"]["image"].append(
            {"op": "edit", "prompt": prompt, "transparent": kw.get("transparent"),
             "source": source}) or {
            "bytes": png_bytes(glyph), "mimeType": "image/png",
            "requestId": "req_icon", "model": "m"},
    )

    out = run_stage(job_id, Stage.INDEPENDENT_ASSETS, asset_id="icon-phone")

    assert out["source"] == "generated"
    assert len(pipeline["calls"]["image"]) == before + 1, "exactly one bounded call"
    call = pipeline["calls"]["image"][-1]
    assert call["transparent"] is True
    assert "Remove the coloured circle" in call["prompt"]
    # It was given the crop of the concept, not a text description.
    with Image.open(io.BytesIO(call["source"])) as crop:
        assert crop.size[0] < 1024


def test_an_icon_that_cannot_be_separated_fails_with_its_name(pipeline, monkeypatch):
    job_id = prepared(pipeline, monkeypatch, textured=True)
    # The generation comes back as a solid tile: still not an isolated pictogram.
    monkeypatch.setattr(
        image_provider, "edit",
        lambda prompt, source, w, h, **kw: {
            "bytes": png_bytes(Image.new("RGB", (64, 64), "#FFD43B")),
            "mimeType": "image/png", "requestId": "r", "model": "m"},
    )

    with pytest.raises(ImpossibleContentRequirement) as raised:
        run_stage(job_id, Stage.INDEPENDENT_ASSETS, asset_id="icon-phone")

    assert "icon-phone" in raised.value.message
    assert "independent transparent image" in raised.value.message


def test_a_contact_row_is_three_independent_layers(pipeline, monkeypatch):
    """The circle, the pictogram and the value: each selectable on its own."""
    job_id = prepared(pipeline, monkeypatch)
    run_stage(job_id, Stage.INDEPENDENT_ASSETS, asset_id="icon-phone")
    scene = artifacts.get_json(
        job_id, jobs.get_job_unscoped(job_id)["artifacts"][Stage.EXTRACT_SCENE]["scene"])
    phone = next(l for l in scene["textLayers"] if l["contentKey"] == "contact.phone")

    html = assemble_html.assemble(
        scene,
        {"background": "https://x/b.png", "decoration": "https://x/d.png",
         "icon-phone": "https://x/i.png"},
        CANVAS,
    )

    circle_id = assemble_html.shape_element_ids(scene)["s_phone_circle"]
    icon_id = assemble_html.image_element_ids(scene)["i_phone"]
    text_id = assemble_html.element_ids(scene)[phone["id"]]
    assert len({circle_id, icon_id, text_id}) == 3
    # Three separate elements, all direct children of .box, none wrapping another.
    body = html.split('class="box"', 1)[1]
    for element_id in (circle_id, icon_id, text_id):
        assert f'\n      <div class="' in body
        assert f'id="{element_id}"' in body
    assert f'<div class="badges b2" id="{icon_id}"' in html
    assert f'id="{circle_id}" data-role="decoration"' in html
    assert f'<div class="text">{phone["text"]}</div>' in html
    # The pictogram is an image layer; the circle carries no text of its own.
    assert html.index(f'id="{circle_id}"') < html.index(f'id="{text_id}"')


def test_the_icon_asset_is_planned_as_an_independent_asset(pipeline, monkeypatch):
    from lib.stages import independent

    job_id = prepared(pipeline, monkeypatch)
    scene = icon_scene()
    assert independent.plan_from_scene(scene, job_id) == ["icon-phone"]


# --- every produced asset reaches the assembler -------------------------------


def two_icon_scene():
    """Two contact pictograms, as a design with a phone and an email line has."""
    doc = icon_scene()
    phone_icon = next(l for l in doc["imageLayers"] if l["id"] == "i_phone")
    doc["imageLayers"].append({**copy.deepcopy(phone_icon), "id": "i_email",
                               "assetId": "icon-email"})
    doc["components"].append(
        {"id": "c_icon_email", "kind": "rasterIcon", "layerId": "i_email",
         "pairsWith": next(c["pairsWith"] for c in doc["components"]
                           if c["id"] == "c_icon"), "note": None})
    return doc


def test_both_icons_are_recorded_and_both_reach_the_assembler(pipeline, monkeypatch):
    """Measured on a live run: the Map produced both pictograms and ASSEMBLE saw
    one, because the stage's whole pointer was rewritten per asset. Each asset is
    recorded under its own key, so a sibling cannot drop it."""
    monkeypatch.setattr(base, "fake_photo", lambda: concept())
    through(pipeline, Stage.CLEAN_PHOTO)
    job_id = pipeline["jobId"]
    decompose(job_id, two_icon_scene())

    for asset_id in ("icon-phone", "icon-email"):
        run_stage(job_id, Stage.INDEPENDENT_ASSETS, asset_id=asset_id)

    job = jobs.get_job_unscoped(job_id)
    assert [e["assetId"] for e in jobs.asset_items(job)] == ["icon-email", "icon-phone"]

    out = run_stage(job_id, Stage.ASSEMBLE)
    delivered = {e["assetId"] for e in out["assets"]}
    assert {"icon-phone", "icon-email"} <= delivered
    html = artifacts.get_bytes(job_id, out["html"]).decode("utf-8")
    for element_id in assemble_html.image_element_ids(
            artifacts.get_json(job_id, out["scene"])).values():
        assert f'id="{element_id}"' in html


def test_a_measured_fraction_does_not_fail_the_record(pipeline, monkeypatch):
    """DynamoDB has no float type: the measured alpha fraction reached it as one
    and failed the stage after the icon had already been produced."""
    from decimal import Decimal

    job_id = prepared(pipeline, monkeypatch)
    run_stage(job_id, Stage.INDEPENDENT_ASSETS, asset_id="icon-phone")

    stored = pipeline["table"].items[f"JOB#{job_id}"]["assets"]["icon-phone"]
    assert isinstance(stored["alphaFraction"], Decimal)
    assert not isinstance(stored["alphaFraction"], float)
    assert jobs.storable({"a": [0.5, {"b": 1.25}], "c": 3}) == {
        "a": [Decimal("0.5"), {"b": Decimal("1.25")}], "c": 3}


def test_a_measured_fraction_does_not_fail_the_record(pipeline, monkeypatch):
    """DynamoDB has no float type: the measured alpha fraction reached it as one
    and failed the stage after the icon had already been produced."""
    from decimal import Decimal

    job_id = prepared(pipeline, monkeypatch)
    run_stage(job_id, Stage.INDEPENDENT_ASSETS, asset_id="icon-phone")

    stored = pipeline["table"].items[f"JOB#{job_id}"]["assets"]["icon-phone"]
    assert isinstance(stored["alphaFraction"], Decimal)
    assert not isinstance(stored["alphaFraction"], float)
    assert jobs.storable({"a": [0.5, {"b": 1.25}], "c": 3}) == {
        "a": [Decimal("0.5"), {"b": Decimal("1.25")}], "c": 3}
