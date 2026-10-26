"""Hiding each component finds the ones that exist twice."""

import io
import json
import pathlib
import re

from PIL import Image, ImageDraw

from lib import assemble_html, component_probe

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "job_cbf8474e"
CANVAS = {"width": 1080, "height": 1080}


def scene():
    return json.loads((FIXTURES / "scene-initial.json").read_text())


def fake_renderer(doc, baked=()):
    """A stand-in browser: paints every component's box unless it is hidden.

    `baked` names layers that are ALSO painted into the photograph, so hiding the
    component leaves its pixels on the page - the defect being probed for.
    """
    ids = {**assemble_html.element_ids(doc), **assemble_html.shape_element_ids(doc),
           **assemble_html.image_element_ids(doc)}
    # Stacking order: shapes, then the copy on them, then the pictograms.
    paint = [(l["id"], l, (250, 210, 40), False) for l in doc["shapeLayers"]]
    paint += [(l["id"], l, (255, 255, 255), True) for l in doc["textLayers"]]
    paint += [(l["id"], l, (10, 10, 10), False)
              for l in doc["imageLayers"] if l["type"] == "image"]

    def render(html):
        hidden = set(re.findall(r"#([\w-]+)\{visibility:hidden", html))
        image = Image.new("RGB", (CANVAS["width"], CANVAS["height"]), (40, 40, 40))
        draw = ImageDraw.Draw(image)
        for layer_id, layer, color, sparse in paint:
            if ids[layer_id] in hidden and layer_id not in baked:
                continue
            x, y, w, h = layer["x"], layer["y"], layer["width"], layer["height"]
            if sparse:                       # text inks a few strokes, not the whole box
                for stripe in range(x, x + w, 12):
                    draw.rectangle((stripe, y + h // 4, stripe + 3, y + 3 * h // 4), fill=color)
            else:
                draw.rectangle((x, y, x + w - 1, y + h - 1), fill=color)
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    return render


def test_every_editable_component_is_the_only_copy_in_a_clean_design():
    doc = scene()

    records = component_probe.probe(fake_renderer(doc), "<div></div>", doc, CANVAS)

    assert {r["kind"] for r in records} >= {"text", "shape"}
    assert component_probe.baked_duplicates(records) == []
    assert all(r["changed"] > 0.2 for r in records if r["kind"] == "shape")


def test_a_component_also_painted_into_the_photograph_is_found():
    doc = scene()

    records = component_probe.probe(
        fake_renderer(doc, baked={"cta-pill", "phone-circle"}), "<div></div>", doc, CANVAS)

    assert sorted(r["layerId"] for r in component_probe.baked_duplicates(records)) == \
        ["cta-pill", "phone-circle"]


def test_hiding_keeps_the_element_in_the_layout():
    html = component_probe.hide("<div id='text-1001'>x</div>", "text-1001")
    assert "visibility:hidden" in html and "display:none" not in html


def test_only_the_named_components_are_probed():
    doc = scene()
    records = component_probe.probe(
        fake_renderer(doc), "<div></div>", doc, CANVAS, only={"cta-pill"})
    assert [r["layerId"] for r in records] == ["cta-pill"]


def test_a_hairline_outline_is_judged_by_its_own_ink_not_a_fill_s():
    """Measured live: a 1px chevron outline changed 3.6% of its box when hidden - the
    whole of its ink - and was reported as a baked-in duplicate."""
    from lib import component_probe

    scene = {
        "canvas": {"width": 100, "height": 100}, "textLayers": [], "imageLayers": [],
        "shapeLayers": [{"id": "chev", "kind": "chevron", "x": 10, "y": 10, "width": 60,
                         "height": 60, "color": None, "borderColor": "#FFD526", "zIndex": 5}],
    }
    from PIL import Image

    def render(html):
        image = Image.new("RGB", (100, 100), (0, 0, 0))
        if "visibility:hidden" not in html:
            for i in range(12, 68):          # a diagonal hairline: ~56 of ~4,100 pixels
                image.putpixel((i, i), (255, 213, 38))
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    records = component_probe.probe(render, "<div id='chev'></div>", scene, scene["canvas"])
    assert records[0]["changed"] < component_probe.MIN_CHANGED
    assert records[0]["onlyCopy"] is True
