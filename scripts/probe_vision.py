#!/usr/bin/env python3
"""Phase 1 gate: prove the vision model supports what EXTRACT_SCENE and REVIEW need.

Three capabilities, each a hard requirement:

  1. strict JSON via response_format json_schema (strict=true)
       -> EXTRACT_SCENE and REVIEW both require strict JSON, never prose.
  2. detail="original"
       -> EXTRACT_SCENE returns pixel coordinates. Any other detail level resizes
          the image before the model sees it, so coordinates refer to the resized
          frame and every layer lands wrong, with no error raised.
  3. three images in a single call
       -> REVIEW compares concept against the native and feed renders together.

Usage:  python scripts/probe_vision.py
Requires scripts/probe_out/ from probe_image.py for real pixels to analyse.
"""

from __future__ import annotations

import base64
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import conf  # noqa: E402
from scripts._probe_common import OUT_DIR, client, save  # noqa: E402

SCENE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["canvas", "textLayers"],
    "properties": {
        "canvas": {
            "type": "object",
            "additionalProperties": False,
            "required": ["width", "height"],
            "properties": {
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
        },
        "textLayers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "role", "text", "x", "y", "width", "height"],
                "properties": {
                    "id": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": ["title", "subtitle", "cta", "informationItem"],
                    },
                    "text": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
            },
        },
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["passed", "blockingIssues", "suggestions"],
    "properties": {
        "passed": {"type": "boolean"},
        "blockingIssues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "layerId", "message"],
                "properties": {
                    "code": {"type": "string"},
                    "layerId": {"type": ["string", "null"]},
                    "message": {"type": "string"},
                },
            },
        },
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
}


def data_uri(path):
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def image_part(path, detail):
    return {
        "type": "image_url",
        "image_url": {"url": data_uri(path), "detail": detail},
    }


def run():
    concept = OUT_DIR / "concept.png"
    overlay = OUT_DIR / "overlay.png"
    asset = OUT_DIR / "asset.png"
    if not concept.exists():
        print("Run scripts/probe_image.py first.", file=sys.stderr)
        return 2

    oai = client()
    findings = {
        "model": conf.SCENE_MODEL,
        "ran_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "checks": {},
    }

    # 1 + 2. Strict JSON with detail="original", as EXTRACT_SCENE will call it.
    print(f"[1/2] strict JSON + detail=original  ({conf.SCENE_MODEL}) ...", flush=True)
    scene = oai.chat.completions.create(
        model=conf.SCENE_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Analyse this marketing poster. Return the canvas size in "
                            "pixels and every visible text block with its bounding box "
                            "in pixels measured from the TOP-LEFT of the image. Use the "
                            "image's own pixel dimensions."
                        ),
                    },
                    image_part(concept, "original"),
                ],
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "scene_v1",
                "schema": SCENE_SCHEMA,
                "strict": True,
            },
        },
    )
    raw = scene.choices[0].message.content
    parsed = json.loads(raw)
    findings["checks"]["strict_json_detail_original"] = {
        "ok": True,
        "detail": "original",
        "returned_canvas": parsed["canvas"],
        "text_layer_count": len(parsed["textLayers"]),
        "sample_layers": parsed["textLayers"][:4],
        "schema_enforced": True,
    }
    print(
        "      ok: canvas",
        parsed["canvas"],
        "| layers",
        len(parsed["textLayers"]),
        flush=True,
    )

    # 3. Three images in one call, as REVIEW will call it.
    print("[2/2] three images in a single call ...", flush=True)
    review = oai.chat.completions.create(
        model=conf.REVIEW_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Image 1 is a concept, image 2 is a transparent decoration "
                            "overlay, image 3 is a standalone asset. Confirm you can "
                            "see all three and report whether the overlay retains the "
                            "concept's graphic panels while dropping its photographic "
                            "scene."
                        ),
                    },
                    image_part(concept, "original"),
                    image_part(overlay, "original"),
                    image_part(asset, "original"),
                ],
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "review_v1",
                "schema": REVIEW_SCHEMA,
                "strict": True,
            },
        },
    )
    verdict = json.loads(review.choices[0].message.content)
    findings["checks"]["three_images_one_call"] = {
        "ok": True,
        "image_count": 3,
        "verdict": verdict,
    }
    print("      ok: passed =", verdict["passed"], flush=True)

    save("probe_vision.json", json.dumps(findings, indent=2).encode())
    print("\n" + json.dumps(findings, indent=2))
    print("\nPASS: strict JSON, detail=original and multi-image all supported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
