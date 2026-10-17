"""HTML assembly: frozen structure and injection resistance.

The golden file is the contract. If a change here requires updating
tests/golden/design.html, that is a signal to check docs/CONTRACTS.md section 5
first - downstream editors parse this structure.
"""

import pathlib

import pytest

from lib.assemble_html import assemble
from lib.errors import AssemblyFailed

GOLDEN = pathlib.Path(__file__).parent / "golden" / "design.html"

CANVAS = {"width": 1080, "height": 1080}

ASSETS = {
    "background": "https://res.cloudinary.com/demo/photo.png",
    "decoration": "https://res.cloudinary.com/demo/overlay.png",
    "inset_1": "https://res.cloudinary.com/demo/inset.png",
}


def scene(**overrides):
    base = {
        "canvas": CANVAS,
        "imageLayers": [
            {
                "id": "bg",
                "type": "background",
                "assetId": None,
                "x": 0,
                "y": 0,
                "width": 1080,
                "height": 1080,
                "zIndex": 0,
                "objectFit": "cover",
            },
            {
                "id": "deco",
                "type": "decoration",
                "assetId": None,
                "x": 0,
                "y": 0,
                "width": 1080,
                "height": 1080,
                "zIndex": 1,
                "objectFit": "contain",
            },
            {
                "id": "inset",
                "type": "image",
                "assetId": "inset_1",
                "x": 700,
                "y": 760,
                "width": 280,
                "height": 240,
                "zIndex": 5,
                "objectFit": "contain",
            },
        ],
        "textLayers": [
            text_layer(),
            {
                **text_layer(),
                "id": "phone",
                "role": "userInformation",
                "contentKey": "contact.phone",
                "text": "03-1234567",
                "x": 80,
                "y": 960,
                "width": 300,
                "height": 48,
                "zIndex": 11,
                "fontSize": 28,
                "fontWeight": 400,
                "align": "left",
                "direction": "ltr",
            },
        ],
        "overlaySamples": [],
    }
    base.update(overrides)
    return base


def text_layer(**overrides):
    layer = {
        "id": "title",
        "type": "text",
        "role": "title",
        "contentKey": "title.primary",
        "text": "חופשת חורף באיטליה",
        "x": 540,
        "y": 80,
        "width": 480,
        "height": 180,
        "zIndex": 10,
        "fontFamily": "Heebo",
        "fontSize": 72,
        "fontWeight": 800,
        "color": "#FFFFFF",
        "align": "right",
        "direction": "rtl",
        "lineHeight": 1.12,
    }
    layer.update(overrides)
    return layer


# --- frozen structure ------------------------------------------------------


def test_matches_the_golden_file():
    """Regenerate with: python scripts/regen_golden.py - then review the diff."""
    assert assemble(scene(), ASSETS, CANVAS) == GOLDEN.read_text(encoding="utf-8")


def test_output_is_deterministic():
    """FINALIZE verifies the delivered HTML by hash, so this must not vary."""
    assert assemble(scene(), ASSETS, CANVAS) == assemble(scene(), ASSETS, CANVAS)


def test_nesting_is_exactly_container_content_box():
    html = assemble(scene(), ASSETS, CANVAS)
    assert html.index('class="image container"') < html.index('class="content"')
    assert html.index('class="content"') < html.index('class="box"')


def test_layer_elements_are_direct_children_of_box():
    html = assemble(scene(), ASSETS, CANVAS)
    after_box = html.split('class="box"', 1)[1]
    # Six spaces of indentation is the direct-child level in the frozen layout.
    assert '\n      <img class="background"' in after_box
    assert '\n      <div class="badges b1"' in after_box
    assert '\n      <div class="words d1001' in after_box


def test_ids_and_classes_are_unique():
    html = assemble(scene(), ASSETS, CANVAS)
    import re

    ids = re.findall(r'id="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert 'class="words d1001 fxs"' in html
    assert 'class="words d1002 fxs"' in html


def test_badges_group_numbering_continues():
    layers = scene()["imageLayers"]
    html = assemble(scene(imageLayers=layers), ASSETS, CANVAS)
    assert 'id="decorations-1"' in html
    assert 'id="decorations-2"' in html  # the inset is the second image group


def test_line_height_is_emitted_unitless():
    """A px line-height would collapse the text into a single pixel row."""
    html = assemble(scene(), ASSETS, CANVAS)
    assert "line-height:1.12;" in html
    assert "line-height:1.12px" not in html


def test_text_sits_in_a_plain_inner_div():
    html = assemble(scene(), ASSETS, CANVAS)
    assert '<div class="text">חופשת חורף באיטליה</div>' in html


def test_rtl_and_ltr_layers_keep_their_own_direction():
    html = assemble(scene(), ASSETS, CANVAS)
    assert "direction:rtl" in html
    assert "direction:ltr" in html


def test_no_scripts_svg_or_css_background_image():
    html = assemble(scene(), ASSETS, CANVAS)
    for forbidden in ("<script", "<svg", "background-image", "<style"):
        assert forbidden not in html


# --- injection resistance --------------------------------------------------


def test_text_is_html_escaped():
    layers = [text_layer(text='<script>alert(1)</script> & "quoted"')]
    html = assemble(scene(textLayers=layers), ASSETS, CANVAS)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


def test_style_breaking_font_family_rejected():
    layers = [text_layer(fontFamily='Heebo;} </style><script>alert(1)</script>')]
    with pytest.raises(AssemblyFailed, match="unsupported characters"):
        assemble(scene(textLayers=layers), ASSETS, CANVAS)


def test_style_breaking_colour_rejected():
    layers = [text_layer(color="#fff;position:fixed;top:0")]
    with pytest.raises(AssemblyFailed, match="#RGB or #RRGGBB"):
        assemble(scene(textLayers=layers), ASSETS, CANVAS)


def test_non_https_asset_url_rejected():
    assets = dict(ASSETS, background="http://insecure.example/photo.png")
    with pytest.raises(AssemblyFailed, match="not https"):
        assemble(scene(), assets, CANVAS)


def test_javascript_url_rejected():
    assets = dict(ASSETS, background="javascript:alert(1)")
    with pytest.raises(AssemblyFailed, match="not https"):
        assemble(scene(), assets, CANVAS)


def test_url_with_quote_rejected():
    assets = dict(ASSETS, background='https://res.cloudinary.com/a".png')
    with pytest.raises(AssemblyFailed, match="cannot be embedded"):
        assemble(scene(), assets, CANVAS)


def test_missing_asset_url_rejected():
    assets = {k: v for k, v in ASSETS.items() if k != "decoration"}
    with pytest.raises(AssemblyFailed, match="no delivered URL"):
        assemble(scene(), assets, CANVAS)


def test_unknown_role_rejected():
    layers = [text_layer(role="marquee")]
    with pytest.raises(AssemblyFailed, match="not an allowed role"):
        assemble(scene(textLayers=layers), ASSETS, CANVAS)


def test_pixel_line_height_rejected():
    """24 reads as a pixel value that leaked into a multiplier field."""
    layers = [text_layer(lineHeight=24)]
    with pytest.raises(AssemblyFailed, match="multiplier"):
        assemble(scene(textLayers=layers), ASSETS, CANVAS)


def test_out_of_range_z_index_rejected():
    layers = [text_layer(zIndex=9999)]
    with pytest.raises(AssemblyFailed, match="outside 0..100"):
        assemble(scene(textLayers=layers), ASSETS, CANVAS)


def test_unknown_object_fit_rejected():
    layers = scene()["imageLayers"]
    layers[0] = dict(layers[0], objectFit="fill")
    with pytest.raises(AssemblyFailed, match="not one of"):
        assemble(scene(imageLayers=layers), ASSETS, CANVAS)


def test_the_scene_schema_constrains_roles_to_the_validated_allowlist():
    """The model must not be able to emit a role that is rejected downstream.

    An open-ended role string is only caught by the validator, after the image
    generation and the vision call have both been paid for.
    """
    from lib.schemas import ALLOWED_ROLES
    from lib.stages.extract_scene import SCENE_SCHEMA

    role = SCENE_SCHEMA["properties"]["textLayers"]["items"]["properties"]["role"]
    assert role.get("enum") == list(ALLOWED_ROLES)


def test_assembly_and_validation_share_one_role_allowlist():
    from lib import assemble_html, schemas

    assert assemble_html.ALLOWED_ROLES is schemas.ALLOWED_ROLES
