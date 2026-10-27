"""templateFlexibility profiles and the structural template check (addendum sections 3-4).

The fixture is job 9281413450b748119609d627b61670f0, reduced to 256px: the approved
concept (yellow torn borders at top-right and bottom-left) and the three overlays the
provider returned for it. What these tests prove: the check measures, in code and
without a model call, that the first and third overlays carry artwork the concept does
not have, and that the same finding is major in every flexibility profile. What they do
not prove: that a provider will produce a correct template (that needs a real call), or
that the second overlay - a dark panel and nothing else - is wrong (the check does not
see it; the composite review does).
"""

import io
import json
import pathlib

import pytest
from PIL import Image, ImageChops

from lib import template_check, template_policy

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "job_9281413450b7"


def _read(name):
    return (FIXTURE / name).read_bytes()


@pytest.fixture(scope="module")
def scene():
    return json.loads(_read("scene.json"))


@pytest.fixture(scope="module")
def concept():
    return _read("concept-256.png")


def _png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


# --- the dial -------------------------------------------------------------------------


@pytest.mark.parametrize("value, name", [
    (0, "strict"), (1, "small"), (20, "small"), (21, "moderate"), (40, "moderate"),
    (41, "broad"), (100, "broad"),
])
def test_the_bands_select_the_documented_profiles(value, name):
    assert template_policy.profile_for(value)["name"] == name
    assert template_policy.resolve(value)["profile"] == name


def test_an_omitted_value_resolves_to_the_default_of_twenty():
    resolved = template_policy.resolve(None)
    assert resolved["value"] == 20 and resolved["profile"] == "small"
    assert resolved["policyVersion"] == template_policy.POLICY_VERSION


@pytest.mark.parametrize("bad", [-1, 101, 20.5, "20", True, None])
def test_values_outside_zero_to_one_hundred_are_refused(bad):
    with pytest.raises(ValueError):
        template_policy.validate(bad)


def test_tolerances_loosen_with_flexibility_but_the_invariants_never_do():
    resolved = [template_policy.resolve(v) for v in (0, 20, 40, 100)]
    for key in ("contourShift", "textureDelta", "featherWidth"):
        values = [r["tolerances"][key] for r in resolved]
        assert values == sorted(values) and values[0] < values[-1]
    assert len({json.dumps(r["invariants"], sort_keys=True) for r in resolved}) == 1


def test_the_persisted_document_carries_numbers_not_just_a_name():
    resolved = template_policy.resolve(35)
    assert resolved["tolerances"]["contourShift"] == 0.02
    assert "Moderate" in template_policy.prompt_clause(resolved)
    assert template_policy.prompt_clause(None) == ""


# --- the check, on the recorded fixture ------------------------------------------------


@pytest.mark.parametrize("flexibility", [0, 20, 40, 100])
def test_the_added_vertical_border_is_major_in_every_profile(scene, concept, flexibility):
    """Local contour variation may be allowed; a border the concept never had may not."""
    result = template_check.compare(
        concept, _read("overlay-first-256.png"), scene, template_policy.resolve(flexibility))

    assert result["major"] is True
    major = [f for f in result["findings"] if f["severity"] == "major"]
    assert [f["code"] for f in major] == [template_check.CODE_ADDED]
    # Yellow, on the right-hand side, running most of the height - not the concept's
    # own top-right or bottom-left ornaments.
    assert major[0]["color"].startswith("#D") or major[0]["color"].startswith("#E")
    assert major[0]["region"]["x"] > 0.6 and major[0]["span"] > 0.3


def test_the_third_overlay_still_carries_added_artwork(scene, concept):
    result = template_check.compare(concept, _read("overlay-repair-2-256.png"), scene)
    assert result["major"] is True
    assert result["findings"][0]["region"]["x"] > 0.8


def test_a_dark_panel_alone_is_not_something_this_check_can_see(scene, concept):
    """Overlay 2 of the job is a black panel with no added ornament. Dark pixels match the
    concept's dark right-hand side, so this check reports no addition: judging whether a
    dark field is a panel or shading over the photograph is the composite review's job."""
    result = template_check.compare(concept, _read("overlay-repair-1-256.png"), scene)
    assert result["major"] is False
    assert result["regions"] == []


def _concept_ornaments(concept_bytes):
    """A template that is exactly the concept's own yellow ornaments (a negative control)."""
    image = Image.open(io.BytesIO(concept_bytes)).convert("RGB")
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    pixels, target = image.load(), out.load()
    for y in range(image.height):
        for x in range(image.width):
            r, g, b = pixels[x, y]
            if r > 150 and g > 120 and b < 110 and (y < 60 or y > 150):
                target[x, y] = (r, g, b, 255)
    return out


@pytest.mark.parametrize("flexibility", [0, 20, 100])
def test_a_template_that_matches_the_concept_is_not_flagged(scene, concept, flexibility):
    template = _concept_ornaments(concept)
    result = template_check.compare(
        concept, _png(template), scene, template_policy.resolve(flexibility))
    assert result["major"] is False
    assert result["findings"] == []


def test_a_slightly_rougher_contour_is_tolerated_and_only_strict_notices_a_large_one(
        scene, concept):
    template = ImageChops.offset(_concept_ornaments(concept), 2, 2)
    for flexibility in (0, 20, 100):
        result = template_check.compare(
            concept, _png(template), scene, template_policy.resolve(flexibility))
        assert result["major"] is False

    shifted = ImageChops.offset(_concept_ornaments(concept), 4, 4)
    strict = template_check.compare(
        concept, _png(shifted), scene, template_policy.resolve(0))
    relaxed = template_check.compare(
        concept, _png(shifted), scene, template_policy.resolve(20))
    assert not strict["major"] and not relaxed["major"]
    assert [f["code"] for f in strict["findings"]] == [template_check.CODE_CONTOUR]
    assert relaxed["findings"] == []


def test_an_added_line_is_major_even_when_everything_else_matches(scene, concept):
    template = _concept_ornaments(concept)
    pixels = template.load()
    for y in range(20, 230):             # a vertical border the concept does not have
        for x in (214, 215, 216, 217):
            pixels[x, y] = (230, 200, 20, 255)
    result = template_check.compare(
        concept, _png(template), scene, template_policy.resolve(100))
    assert result["major"] is True
    assert result["findings"][0]["span"] > 0.6


def test_the_check_works_at_any_input_size(scene, concept):
    """Concept and template are brought to one working size, whatever they arrive as."""
    big = Image.open(_read_path("overlay-first-256.png")).resize((1024, 1024))
    result = template_check.compare(concept, _png(big), scene)
    assert result["measured"]["workingSize"] == template_check.WORK
    assert result["major"] is True


def _read_path(name):
    return FIXTURE / name
