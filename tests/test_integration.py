"""Live integration tests against the real providers.

Skipped unless RUN_INTEGRATION=1. These make paid OpenAI calls and upload to
Cloudinary, so they never run by default.

    RUN_INTEGRATION=1 scripts/test.sh tests/test_integration.py -v

This is where SPEC section 4's "explicit integration tests establish actual
provider capabilities" lands: PREFLIGHT deliberately does not generate images, so
proof that the model really returns alpha lives here and in scripts/probe_image.py.
"""

import os

import pytest

import conf
from lib.providers import image as image_provider, vision as vision_provider
from lib.validate_assets import OPAQUE_THRESHOLD, alpha_stats, load_rgba

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="live provider tests cost money; set RUN_INTEGRATION=1",
)


@pytest.fixture(scope="module")
def concept():
    """One paid generation, shared by the tests below."""
    result = image_provider.generate(
        "A premium winter travel poster scene: a Tuscan hill town under snow at "
        "golden hour, with a decorative rounded panel in the lower third.",
        1024,
        1024,
        transparent=False,
        quality="low",
        operation_id="integration:concept",
    )
    return result["bytes"]


def test_image_model_returns_real_alpha_on_the_edit_path(concept):
    """The capability DECORATION_OVERLAY depends on entirely."""
    result = image_provider.edit(
        "Keep only the decorative panel artwork. Remove the entire photographic "
        "scene and all text. The removed areas must be fully transparent.",
        concept,
        1024,
        1024,
        transparent=True,
        quality="low",
        operation_id="integration:overlay",
    )
    rgba, had_alpha = load_rgba(result["bytes"])
    assert had_alpha, "the edit path returned no alpha channel"

    stats = alpha_stats(rgba)
    assert stats["transparent"] > 0.05, f"almost nothing is transparent: {stats}"
    assert stats["opaque"] > 0.01, f"almost nothing is opaque: {stats}"


def test_opaque_pixels_still_top_out_below_255(concept):
    """Regression guard on the calibration in docs/PROBE_RESULTS.md.

    If the provider ever starts emitting a true 255, this test tells us the
    OPAQUE_THRESHOLD of 250 can be revisited. Until then it must stay.
    """
    result = image_provider.generate(
        "A single isolated gold laurel wreath ornament on a transparent "
        "background. No text.",
        1024,
        1024,
        transparent=True,
        quality="low",
        operation_id="integration:alpha-ceiling",
    )
    rgba, _ = load_rgba(result["bytes"])
    stats = alpha_stats(rgba)
    assert stats["alphaMax"] >= OPAQUE_THRESHOLD
    if stats["alphaMax"] == 255:
        pytest.skip(
            "the provider now emits alpha 255; revisit OPAQUE_THRESHOLD and "
            "docs/PROBE_RESULTS.md"
        )


def test_vision_model_enforces_strict_json():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["colour", "count"],
        "properties": {
            "colour": {"type": "string"},
            "count": {"type": "integer"},
        },
    }
    result = vision_provider.analyze(
        "Reply with colour set to 'blue' and count set to 3.",
        (),
        schema=schema,
        schema_name="integration_v1",
        model=conf.SCENE_MODEL,
    )
    assert set(result) == {"colour", "count"}
    assert isinstance(result["count"], int)


def test_vision_model_accepts_three_images_in_one_call(concept):
    """REVIEW sends concept, native render and feed render together."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["imagesSeen"],
        "properties": {"imagesSeen": {"type": "integer"}},
    }
    result = vision_provider.analyze(
        "How many images were provided? Reply with imagesSeen.",
        (concept, concept, concept),
        schema=schema,
        schema_name="integration_count_v1",
        model=conf.REVIEW_MODEL,
    )
    assert result["imagesSeen"] == 3


def test_configured_models_are_all_reachable():
    for model in (conf.SCENE_MODEL, conf.REVIEW_MODEL, conf.COPY_MODEL):
        assert vision_provider.capability_check(model), model


@pytest.mark.skipif(
    # conftest sets dummy CLOUDINARY_* values for the unit tests, so checking
    # those would never skip. Real credentials arrive either as a Secrets
    # Manager ARN or as a cloud name that is not the test placeholder.
    not os.environ.get("CLOUDINARY_SECRET_ARN")
    and os.environ.get("CLOUDINARY_CLOUD_NAME", "") in ("", "test-cloud"),
    reason="stable media credentials are not configured",
)
def test_stable_media_publishing_is_reachable():
    """Exercises whichever credential source is configured.

    With CLOUDINARY_SECRET_ARN set this also proves the AWS role can read the
    secret, which is the part that only fails once deployed.
    """
    from lib import media

    media.reset()
    assert media.check_available()
