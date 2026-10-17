"""Alpha validation, calibrated against the measured probe histogram.

The synthetic overlays here deliberately top out at alpha 254 rather than 255,
because that is what the image model actually produces (docs/PROBE_RESULTS.md).
A test built on 255 would pass while the real pipeline rejected every asset.
"""

import io

import pytest
from PIL import Image

from lib import validate_assets as va
from lib.errors import AssetInvalid, InvalidTransparency

CANVAS = {"width": 200, "height": 200}

#: What the provider really emits for "fully opaque".
MODEL_OPAQUE = 254


def png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def overlay(*, opaque_box=(20, 20, 120, 120), soft_edge=True, size=200):
    """An overlay: transparent canvas with one opaque panel and a soft edge."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    left, top, right, bottom = opaque_box
    for x in range(left, right):
        for y in range(top, bottom):
            img.putpixel((x, y), (200, 30, 60, MODEL_OPAQUE))
    if soft_edge:
        for x in range(left, right):
            for offset, alpha in ((0, 128), (1, 64)):
                y = bottom + offset
                if y < size:
                    img.putpixel((x, y), (200, 30, 60, alpha))
    return img


def photo():
    """A background with enough colour variation to read as photographic."""
    img = Image.new("RGB", (200, 200))
    for x in range(200):
        for y in range(200):
            img.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    return img


def scene(samples):
    return {"overlaySamples": samples}


GOOD_SAMPLES = [
    {"kind": "opaque", "x": 40, "y": 40, "width": 40, "height": 40},
    {"kind": "transparent", "x": 150, "y": 150, "width": 40, "height": 40},
]


# --- the calibration itself ------------------------------------------------


def test_model_opaque_alpha_is_treated_as_opaque():
    """The whole module hinges on this: 254 must count as opaque.

    If this ever regresses to an == 255 test, every real overlay is rejected.
    """
    rgba, _ = va.load_rgba(png(overlay()))
    stats = va.alpha_stats(rgba)
    assert stats["opaque"] > 0.2
    assert stats["alphaMax"] == MODEL_OPAQUE


def test_normalization_lifts_the_ceiling_to_255():
    rgba, _ = va.load_rgba(png(overlay()))
    normalized = va.normalize_alpha(rgba)
    assert va.alpha_stats(normalized)["alphaMax"] == 255


def test_normalization_preserves_real_translucency():
    """A soft edge at alpha 128 must stay at 128, not get rounded to opaque."""
    rgba, _ = va.load_rgba(png(overlay()))
    normalized = va.normalize_alpha(rgba)
    assert normalized.getchannel("A").histogram()[128] > 0


# --- overlay validation ----------------------------------------------------


def test_valid_overlay_passes():
    stats = va.validate_overlay(png(overlay()), scene(GOOD_SAMPLES), CANVAS)
    assert stats["transparent"] > 0.5
    assert stats["opaque"] > 0.2


def test_flat_image_without_alpha_rejected():
    with pytest.raises(InvalidTransparency, match="no alpha channel"):
        va.validate_overlay(png(photo()), scene(GOOD_SAMPLES), CANVAS)


def test_fully_opaque_rgba_overlay_rejected():
    """The common failure: the model returns the artwork on a white rectangle."""
    img = Image.new("RGBA", (200, 200), (255, 255, 255, MODEL_OPAQUE))
    with pytest.raises(InvalidTransparency, match="effectively opaque"):
        va.validate_overlay(png(img), scene(GOOD_SAMPLES), CANVAS)


def test_empty_overlay_rejected():
    img = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    with pytest.raises(InvalidTransparency, match="effectively empty"):
        va.validate_overlay(png(img), scene(GOOD_SAMPLES), CANVAS)


def test_hard_keyed_overlay_rejected():
    with pytest.raises(InvalidTransparency, match="no soft edges"):
        va.validate_overlay(
            png(overlay(soft_edge=False)), scene(GOOD_SAMPLES), CANVAS
        )


def test_scene_without_sample_regions_rejected():
    with pytest.raises(InvalidTransparency, match="no overlay sample regions"):
        va.validate_overlay(png(overlay()), scene([]), CANVAS)


# --- sample regions --------------------------------------------------------


def test_region_expected_opaque_but_transparent_is_reported():
    samples = [{"kind": "opaque", "x": 150, "y": 150, "width": 40, "height": 40}]
    with pytest.raises(InvalidTransparency, match="should be solid artwork"):
        va.validate_overlay(png(overlay()), scene(samples), CANVAS)


def test_region_expected_transparent_but_opaque_is_reported():
    samples = [
        {"kind": "transparent", "x": 40, "y": 40, "width": 40, "height": 40},
        {"kind": "opaque", "x": 40, "y": 40, "width": 40, "height": 40},
    ]
    with pytest.raises(InvalidTransparency, match="show the photograph through"):
        va.validate_overlay(png(overlay()), scene(samples), CANVAS)


def test_region_outside_the_canvas_is_skipped_not_fatal():
    """A model-reported box can run past the edge; clamp rather than fail."""
    samples = GOOD_SAMPLES + [
        {"kind": "opaque", "x": 199, "y": 199, "width": 50, "height": 50}
    ]
    va.validate_overlay(png(overlay()), scene(samples), CANVAS)


def test_regions_are_checked_against_the_resampled_canvas():
    """Coordinates are canvas pixels, so a differently sized asset must be
    resampled before sampling or every region lands in the wrong place."""
    big = overlay(size=400, opaque_box=(40, 40, 240, 240))
    va.validate_overlay(png(big), scene(GOOD_SAMPLES), CANVAS)


# --- background validation -------------------------------------------------


def test_valid_background_passes():
    result = va.validate_background(png(photo()), CANVAS)
    assert result == {"width": 200, "height": 200}


def test_background_with_holes_rejected():
    """Panel-shaped gaps mean the scene behind the graphics was not rebuilt."""
    img = photo().convert("RGBA")
    for x in range(20, 120):
        for y in range(20, 120):
            img.putpixel((x, y), (0, 0, 0, 0))
    with pytest.raises(AssetInvalid, match="not reconstructed"):
        va.validate_background(png(img), CANVAS)


def test_solid_colour_background_rejected():
    img = Image.new("RGB", (200, 200), (120, 140, 160))
    with pytest.raises(AssetInvalid, match="near-solid colour"):
        va.validate_background(png(img), CANVAS)


def test_background_is_resampled_to_the_canvas():
    result = va.validate_background(png(photo().resize((512, 512))), CANVAS)
    assert result == {"width": 200, "height": 200}


def test_undecodable_bytes_rejected():
    with pytest.raises(AssetInvalid, match="not a decodable image"):
        va.load_rgba(b"this is not a png")
