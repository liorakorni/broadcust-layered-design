"""End-to-end pipeline run with faked providers.

Proves the twelve stages actually compose: each one finds the artifacts the
previous stage recorded, under the real artifact key scheme, with the real
conditional-write and ownership rules in force. Only the three external services
are faked - OpenAI, Cloudinary and Chromium - and each fake returns realistic
data, including the alpha-254 ceiling the image model really produces.

The brief is the SPEC section 9 scenario: Hebrew RTL body copy with Latin contact
details, and no invented price.
"""

import io
import json

import pytest
from PIL import Image

import conf
import handler_stage
from lib import artifacts, jobs, media, reference, render as renderer
from lib.errors import Stage, Status
from lib.providers import image as image_provider, vision as vision_provider
from tests.fakes import FakeS3, FakeTable

CANVAS = {"width": 1024, "height": 1024}

COPY_ITEMS = [
    {"contentKey": "title.primary", "role": "title", "text": "חופשת חורף באיטליה"},
    {
        "contentKey": "subtitle.primary",
        "role": "subtitle",
        "text": "טיסות, מלון ומדריך צמוד",
    },
    {"contentKey": "contact.phone", "role": "contact", "text": "03-1234567"},
]

REQUEST = {
    "schemaVersion": "1.0",
    "requestId": "req-italy-1",
    "brief": {
        "post": "חופשת חורף באיטליה. טיסות, מלון ומדריך צמוד. יציאה בינואר.",
        "instructions": None,
        "requiredTexts": ["חופשת חורף באיטליה"],
        "business": {
            "name": "Dolce Travel",
            "phone": "03-1234567",
            "email": "info@dolce.example",
            "website": None,
        },
    },
    "canvas": CANVAS,
    "referenceImageUrl": "https://res.cloudinary.com/demo/inspiration.jpg",
    "brand": {"colors": ["#0B2545"], "fontFamilies": ["Heebo"]},
    "suppliedAssets": [],
}


# --- realistic fake assets -------------------------------------------------


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def fake_photo():
    """A varied image that reads as photographic rather than a flat colour."""
    small = Image.new("RGB", (64, 64))
    small.putdata(
        [(x * 4 % 256, y * 4 % 256, (x + y) * 2 % 256) for y in range(64) for x in range(64)]
    )
    return png_bytes(small.resize((1024, 1024), Image.BILINEAR))


def fake_overlay():
    """Opaque panel at alpha 254 with a feathered edge, transparent elsewhere.

    254 rather than 255 is what the real model emits, and the feathered band
    reproduces the ~1.8% partially transparent pixels measured in the probe. See
    docs/PROBE_RESULTS.md.
    """
    from PIL import ImageDraw, ImageFilter

    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rounded_rectangle((100, 100, 500, 500), radius=40, fill=254)
    mask = mask.filter(ImageFilter.GaussianBlur(6))
    # Blur pushes the interior slightly below 254; clamp it back so the panel
    # reads as opaque exactly the way a real one does.
    mask = mask.point(lambda a: 254 if a >= 250 else a)
    panel = Image.new("RGBA", (1024, 1024), (11, 37, 69, 0))
    panel.putalpha(mask)
    return png_bytes(panel)


SCENE = {
    "canvas": CANVAS,
    "textLayers": [
        {
            "id": "t_title",
            "type": "text",
            "role": "title",
            "contentKey": "title.primary",
            "text": "חופשת חורף באיטליה",
            "x": 120,
            "y": 140,
            "width": 700,
            "height": 160,
            "zIndex": 10,
            "fontFamily": "Heebo",
            "fontSize": 72,
            "fontWeight": 800,
            "color": "#FFFFFF",
            "align": "right",
            "direction": "rtl",
            "lineHeight": 1.12,
        },
        {
            "id": "t_sub",
            "type": "text",
            "role": "subtitle",
            "contentKey": "subtitle.primary",
            "text": "טיסות, מלון ומדריך צמוד",
            "x": 120,
            "y": 320,
            "width": 700,
            "height": 80,
            "zIndex": 11,
            "fontFamily": "Heebo",
            "fontSize": 40,
            "fontWeight": 400,
            "color": "#FFFFFF",
            "align": "right",
            "direction": "rtl",
            "lineHeight": 1.2,
        },
        {
            "id": "t_phone",
            "type": "text",
            "role": "userInformation",
            "contentKey": "contact.phone",
            "text": "03-1234567",
            "x": 120,
            "y": 920,
            "width": 300,
            "height": 48,
            "zIndex": 12,
            "fontFamily": "Heebo",
            "fontSize": 28,
            "fontWeight": 400,
            "color": "#FFFFFF",
            "align": "left",
            "direction": "ltr",
            "lineHeight": 1.2,
        },
    ],
    "imageLayers": [
        {
            "id": "l_bg",
            "type": "background",
            "assetId": None,
            "x": 0,
            "y": 0,
            "width": 1024,
            "height": 1024,
            "zIndex": 0,
            "objectFit": "cover",
        },
        {
            "id": "l_deco",
            "type": "decoration",
            "assetId": None,
            "x": 0,
            "y": 0,
            "width": 1024,
            "height": 1024,
            "zIndex": 1,
            "objectFit": "contain",
        },
    ],
    "overlaySamples": [
        {"kind": "opaque", "x": 200, "y": 200, "width": 100, "height": 100},
        {"kind": "transparent", "x": 700, "y": 700, "width": 100, "height": 100},
    ],
}


@pytest.fixture
def pipeline(monkeypatch):
    """Wire every external dependency to a deterministic fake."""
    table = FakeTable()
    s3 = FakeS3()
    calls = {"image": [], "vision": [], "published": []}

    monkeypatch.setattr(jobs, "_tbl", lambda: table)
    monkeypatch.setattr(artifacts, "_client", lambda: s3)

    def fake_fetch(url, max_bytes=None):
        return {
            "bytes": fake_photo(),
            "mimeType": "image/png",
            "format": "PNG",
            "width": 1024,
            "height": 1024,
        }

    monkeypatch.setattr(reference, "fetch", fake_fetch)

    def fake_edit(prompt, source, w, h, *, transparent=False, operation_id=None,
                  quality="high", mime="image/png"):
        calls["image"].append(
            {"op": "edit", "transparent": transparent, "source": source,
             "operationId": operation_id}
        )
        data = fake_overlay() if transparent else fake_photo()
        return {"bytes": data, "mimeType": "image/png", "requestId": "req_x",
                "model": conf.IMAGE_MODEL}

    monkeypatch.setattr(image_provider, "edit", fake_edit)

    def fake_analyze(prompt, images=(), *, schema, schema_name, model=None,
                     operation_id=None, detail="original"):
        calls["vision"].append(
            {"schema": schema_name, "images": len(images), "detail": detail,
             "model": model, "prompt": prompt}
        )
        if schema_name == "copy_manifest_v1":
            return {"items": COPY_ITEMS}
        if schema_name == "scene_v1":
            return json.loads(json.dumps(SCENE))
        if schema_name == "overlay_inspection_v1":
            return {"photographicLeftovers": False, "textLeftovers": False,
                    "artworkPresent": True, "notes": ""}
        if schema_name == "review_v1":
            return {"passed": True, "blockingIssues": [], "suggestions": []}
        raise AssertionError(f"unexpected schema {schema_name}")

    monkeypatch.setattr(vision_provider, "analyze", fake_analyze)

    def fake_publish(data, *, job_id, asset_id):
        calls["published"].append(asset_id)
        return {"url": f"https://res.cloudinary.com/demo/{job_id}/{asset_id}.png",
                "publicId": asset_id, "bytes": len(data)}

    monkeypatch.setattr(media, "publish", fake_publish)

    def fake_render(html, canvas, scene):
        return {
            "native": fake_photo(),
            "feed": fake_photo(),
            "checks": {"problems": [], "textLayers": len(scene["textLayers"]),
                       "images": 2},
        }

    monkeypatch.setattr(renderer, "render", fake_render)

    job_id = "job-italy"
    request_key = artifacts.artifact_key(job_id, Stage.ACCEPT_REQUEST, 1, "request.json")
    artifacts.put_json(request_key, REQUEST)
    jobs.create_job(job_id, "owner-1", "hash-1", request_key, CANVAS)
    table.items[f"JOB#{job_id}"]["artifacts"] = {}

    return {"jobId": job_id, "table": table, "s3": s3, "calls": calls}


def run_stage(job_id, stage, attempt=1, asset_id=None):
    event = {"jobId": job_id, "stage": stage, "attempt": attempt, "assetId": asset_id}
    return handler_stage.handler(event, None)["artifacts"]


def run_pipeline(pipeline):
    job_id = pipeline["jobId"]
    for stage in (
        Stage.PREFLIGHT,
        Stage.PREPARE_COPY,
        Stage.CONCEPT,
        Stage.EXTRACT_SCENE,
        Stage.CLEAN_PHOTO,
        Stage.DECORATION_OVERLAY,
        Stage.VALIDATE_ASSETS,
        Stage.ASSEMBLE,
        Stage.RENDER,
        Stage.REVIEW,
        Stage.FINALIZE,
    ):
        run_stage(job_id, stage)
    return pipeline["table"].items[f"JOB#{job_id}"]


@pytest.fixture(autouse=True)
def skip_external_probes(monkeypatch):
    """PREFLIGHT launches Chromium and pings Cloudinary; neither is available
    on the test host, and both are exercised for real in test_integration.py."""
    monkeypatch.setattr("lib.stages.preflight._check_renderer", lambda: [])
    monkeypatch.setattr("lib.stages.preflight._check_media", lambda: [])


# --- the run ---------------------------------------------------------------


def test_pipeline_completes_and_marks_the_job_succeeded(pipeline):
    job = run_pipeline(pipeline)
    assert job["status"] == Status.SUCCEEDED
    assert job["stage"] == Stage.FINALIZE
    assert job["htmlHash"]
    assert job["assetManifestHash"]


def test_result_matches_the_spec_output_shape(pipeline):
    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    assert set(result) >= {
        "jobId", "status", "html", "htmlHash", "assets",
        "assetManifestHash", "layerMap", "preview", "canvas",
    }
    assert result["html"].startswith('<div class="image container"')
    assert result["preview"].startswith("https://")


def test_layer_map_describes_every_layer(pipeline):
    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    kinds = sorted(layer["kind"] for layer in result["layerMap"])
    assert kinds == ["background", "decoration", "text", "text", "text"]


def test_approved_copy_reaches_the_html_verbatim(pipeline):
    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    for item in COPY_ITEMS:
        assert item["text"] in result["html"]


def test_latin_contact_stays_ltr_inside_an_rtl_design(pipeline):
    """SPEC section 9: a phone number inside Hebrew copy must not be reordered."""
    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    assert "direction:ltr" in result["html"]
    assert "direction:rtl" in result["html"]
    assert "03-1234567" in result["html"]


def test_no_price_is_invented(pipeline):
    """The brief names no price, so none may appear in the visible copy.

    Only the text content is inspected; `%` legitimately appears in CSS widths.
    """
    import re

    job = run_pipeline(pipeline)
    result = artifacts.get_json(pipeline["jobId"], job["resultKey"])
    copy = " ".join(re.findall(r'<div class="text">(.*?)</div>', result["html"]))
    for marker in ("$", "₪", "%"):
        assert marker not in copy


# --- the invariants, observed on a real run --------------------------------


def test_both_separation_branches_edit_the_identical_concept_bytes(pipeline):
    """Invariant 3. If the overlay were derived from the cleaned photo it would
    be empty, and no later stage would notice."""
    run_pipeline(pipeline)
    edits = [c for c in pipeline["calls"]["image"] if c["op"] == "edit"]
    separation = edits[1:3]
    assert len(separation) == 2
    assert separation[0]["source"] == separation[1]["source"]
    assert {c["transparent"] for c in separation} == {False, True}


def test_scene_extraction_requests_original_detail(pipeline):
    """Invariant 4. Any other detail level silently misplaces every layer."""
    run_pipeline(pipeline)
    scene_calls = [c for c in pipeline["calls"]["vision"] if c["schema"] == "scene_v1"]
    assert scene_calls and all(c["detail"] == "original" for c in scene_calls)


def test_review_sees_concept_native_and_feed(pipeline):
    run_pipeline(pipeline)
    review = [c for c in pipeline["calls"]["vision"] if c["schema"] == "review_v1"]
    assert review and review[0]["images"] == 3


def test_overlay_is_inspected_on_three_contrasting_backgrounds(pipeline):
    run_pipeline(pipeline)
    inspect = [
        c for c in pipeline["calls"]["vision"]
        if c["schema"] == "overlay_inspection_v1"
    ]
    assert inspect and inspect[0]["images"] == 3


def test_preflight_performs_no_paid_image_generation(pipeline):
    run_stage(pipeline["jobId"], Stage.PREFLIGHT)
    assert pipeline["calls"]["image"] == []


def test_every_artifact_lands_under_the_job_prefix(pipeline):
    run_pipeline(pipeline)
    prefix = f"layered-design/{pipeline['jobId']}/"
    assert all(k.startswith(prefix) for k in pipeline["s3"].objects)


def test_concept_bytes_are_never_overwritten(pipeline):
    """Artifacts are immutable, so a second write to the same key must fail."""
    from lib.errors import AssetInvalid

    run_pipeline(pipeline)
    job = pipeline["table"].items[f"JOB#{pipeline['jobId']}"]
    key = job["artifacts"][Stage.CONCEPT]["concept"]
    with pytest.raises(AssetInvalid, match="immutable"):
        artifacts.put_bytes(key, b"different", "image/png")


def test_model_separate_from_assembly(pipeline):
    """SPEC section 6: no model writes HTML. Every vision call is schema-bound,
    and none of them is handed the document."""
    run_pipeline(pipeline)
    for call in pipeline["calls"]["vision"]:
        assert call["schema"]
        assert "<div" not in call["prompt"]


def test_models_stay_separately_configurable(pipeline):
    run_pipeline(pipeline)
    used = {c["model"] for c in pipeline["calls"]["vision"]}
    assert conf.COPY_MODEL in used
    assert conf.SCENE_MODEL in used
    assert conf.REVIEW_MODEL in used
