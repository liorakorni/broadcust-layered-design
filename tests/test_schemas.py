"""Validation tests for the three documents that cross a trust boundary."""

import copy

import pytest

from lib.errors import InvalidModelJson, InvalidRequest, InvalidScene
from lib.schemas import (
    canonical_request_hash,
    parse_review,
    parse_scene,
    parse_start_request,
)

CLOUDINARY = "https://res.cloudinary.com/broadcust/image/upload"


# --- start request ---------------------------------------------------------


def test_accepts_the_acceptance_brief(start_payload):
    req = parse_start_request(start_payload)
    assert req.requestId == "req-italy-001"
    assert req.canvas.width == 1080
    assert req.brief.business.phone == "0501234567"
    assert req.suppliedAssets[0].kind == "logo"


def test_canvas_defaults_to_1080_square(start_payload):
    del start_payload["canvas"]
    req = parse_start_request(start_payload)
    assert (req.canvas.width, req.canvas.height) == (1080, 1080)


def test_schema_version_defaults(start_payload):
    del start_payload["schemaVersion"]
    assert parse_start_request(start_payload).schemaVersion == "1.0"


@pytest.mark.parametrize("field", ["requestId", "brief", "referenceImageUrl"])
def test_required_fields_rejected_when_missing(start_payload, field):
    del start_payload[field]
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_blank_post_rejected(start_payload):
    start_payload["brief"]["post"] = "   "
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_overlong_post_rejected(start_payload):
    start_payload["brief"]["post"] = "x" * 12001
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


@pytest.mark.parametrize("size", [255, 2049, 0, -1])
def test_canvas_out_of_range_rejected(start_payload, size):
    start_payload["canvas"] = {"width": size, "height": size}
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://res.cloudinary.com/a.png",            # not https
        "https://evil.com/a.png",                      # unauthorized origin
        "https://res.cloudinary.com.evil.com/a.png",   # suffix spoofing
        "ftp://res.cloudinary.com/a.png",
    ],
)
def test_unauthorized_reference_origins_rejected(start_payload, url):
    start_payload["referenceImageUrl"] = url
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_unknown_structural_field_rejected(start_payload):
    # Silently ignoring an unknown field lets a caller think it took effect.
    start_payload["renderMode"] = "whatever"
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_duplicate_asset_ids_rejected(start_payload):
    start_payload["suppliedAssets"].append(
        {"id": "business-logo", "kind": "photo", "url": f"{CLOUDINARY}/other.png"}
    )
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_more_than_ten_supplied_assets_rejected(start_payload):
    start_payload["suppliedAssets"] = [
        {"id": f"a{i}", "kind": "photo", "url": f"{CLOUDINARY}/{i}.png"}
        for i in range(11)
    ]
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_invalid_brand_colour_rejected(start_payload):
    start_payload["brand"]["colors"] = ["red"]
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


def test_oversized_request_rejected(start_payload):
    start_payload["brief"]["instructions"] = "x" * 70_000
    with pytest.raises(InvalidRequest):
        parse_start_request(start_payload)


# --- idempotency hashing ---------------------------------------------------


def test_same_content_hashes_equal_regardless_of_key_order(start_payload):
    reordered = dict(reversed(list(start_payload.items())))
    a = canonical_request_hash(parse_start_request(start_payload))
    b = canonical_request_hash(parse_start_request(reordered))
    assert a == b


def test_changed_content_changes_hash(start_payload):
    a = canonical_request_hash(parse_start_request(start_payload))
    changed = copy.deepcopy(start_payload)
    changed["brief"]["post"] += " Extra."
    b = canonical_request_hash(parse_start_request(changed))
    assert a != b


def test_omitted_default_hashes_same_as_explicit_default(start_payload):
    explicit = canonical_request_hash(parse_start_request(start_payload))
    implicit_payload = copy.deepcopy(start_payload)
    del implicit_payload["canvas"]
    implicit = canonical_request_hash(parse_start_request(implicit_payload))
    assert explicit == implicit


# --- scene -----------------------------------------------------------------


def test_valid_scene_accepted(scene_payload):
    scene = parse_scene(scene_payload, 1080, 1080)
    assert len(scene.textLayers) == 1
    assert scene.textLayers[0].lineHeight == 1.12


def test_scene_canvas_must_match_request(scene_payload):
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1200, 1200)


def test_layer_outside_canvas_rejected(scene_payload):
    scene_payload["textLayers"][0]["x"] = 1000
    scene_payload["textLayers"][0]["width"] = 500
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080)


def test_duplicate_layer_ids_rejected(scene_payload):
    dup = copy.deepcopy(scene_payload["textLayers"][0])
    dup["contentKey"] = "title.secondary"
    scene_payload["textLayers"].append(dup)
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080)


def test_duplicate_content_keys_rejected(scene_payload):
    dup = copy.deepcopy(scene_payload["textLayers"][0])
    dup["id"] = "text-1002"
    scene_payload["textLayers"].append(dup)
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080)


@pytest.mark.parametrize(
    "key",
    [
        "title.main",
        "service.personal_support",
        "service.custom_itinerary",
        "cta.book_now",
        "contact.phone",
        "title",
    ],
)
def test_every_key_the_copy_stage_accepts_is_accepted_in_a_scene(scene_payload, key):
    # The copy manifest and the scene are validated by separate patterns. A key
    # approved by one and refused by the other fails a paid job midway.
    from lib.stages.prepare_copy import CONTENT_KEY as copy_pattern

    assert copy_pattern.match(key)
    scene_payload["textLayers"][0]["contentKey"] = key
    scene = parse_scene(scene_payload, 1080, 1080)
    assert scene.textLayers[0].contentKey == key


def test_content_key_outside_approved_copy_rejected(scene_payload):
    # The model inventing a content key is how invented copy would enter.
    with pytest.raises(InvalidScene):
        parse_scene(
            scene_payload, 1080, 1080, allowed_content_keys={"subtitle.primary"}
        )


def test_uninstalled_font_rejected(scene_payload):
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080, allowed_fonts={"Arial"})


def test_line_height_in_pixels_rejected(scene_payload):
    # 72 means someone sent pixels where a multiplier belongs.
    scene_payload["textLayers"][0]["lineHeight"] = 72
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080)


@pytest.mark.parametrize("z", [-1, 101])
def test_zindex_out_of_range_rejected(scene_payload, z):
    scene_payload["textLayers"][0]["zIndex"] = z
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080)


def test_unknown_role_rejected(scene_payload):
    scene_payload["textLayers"][0]["role"] = "disclaimer"
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080)


def test_non_positive_size_rejected(scene_payload):
    scene_payload["textLayers"][0]["height"] = 0
    with pytest.raises(InvalidScene):
        parse_scene(scene_payload, 1080, 1080)


# --- review ----------------------------------------------------------------


def test_review_passed_with_no_issues_is_approved():
    v = parse_review({"passed": True, "blockingIssues": [], "suggestions": []})
    assert v.approved is True


def test_model_claiming_passed_while_reporting_blockers_is_not_approved():
    # SPEC section REVIEW: passed:true does not override blocking issues.
    v = parse_review(
        {
            "passed": True,
            "blockingIssues": [
                {"code": "TEXT_OVERFLOW", "layerId": "text-1001", "message": "clipped"}
            ],
            "suggestions": [],
        }
    )
    assert v.passed is True
    assert v.approved is False


def test_review_missing_required_field_rejected():
    with pytest.raises(InvalidModelJson):
        parse_review({"passed": True})


def test_review_null_optional_ids_allowed():
    v = parse_review(
        {
            "passed": False,
            "blockingIssues": [
                {"code": "X", "layerId": None, "assetId": None, "message": "m"}
            ],
            "suggestions": [],
        }
    )
    assert v.approved is False
