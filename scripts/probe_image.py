#!/usr/bin/env python3
"""Phase 1 gate: prove the configured image model returns a real alpha channel.

Mirrors the real pipeline rather than testing transparency in the abstract:

  1. generate an OPAQUE concept          -> what CONCEPT does
  2. edit that concept demanding alpha   -> what DECORATION_OVERLAY does
  3. generate directly with alpha        -> transparent asset creation

Step 2 is the one that matters. DECORATION_OVERLAY edits an opaque concept and
must get genuine RGBA back; if that fails the service cannot be built as specified.

Usage:  python scripts/probe_image.py
Cost:   three low-quality 1024x1024 images. Do not loop this.
"""

from __future__ import annotations

import base64
import datetime
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import conf  # noqa: E402
from scripts._probe_common import alpha_report, client, save  # noqa: E402

SIZE = "1024x1024"
QUALITY = "low"

CONCEPT_PROMPT = (
    "A premium marketing poster composition for a winter travel offer to Italy. "
    "Photographic Tuscan hillside at golden hour, with decorative graphic ribbon "
    "panels and a rounded badge shape overlaying the photograph. Rich, polished, "
    "magazine quality."
)

OVERLAY_PROMPT = (
    "Keep ONLY the graphic artwork: the decorative ribbon panels, badge shapes and "
    "ornamental flourishes, aligned exactly where they are. Remove the entire "
    "photographic scene - sky, hills, buildings, landscape - and remove all words "
    "and logos. The areas where the photograph showed through must be fully "
    "transparent, not white and not a drawn checkerboard. Keep the graphic panels "
    "opaque and keep soft edges partially transparent."
)

TRANSPARENT_ASSET_PROMPT = (
    "A single decorative golden ribbon banner on a fully transparent background. "
    "Clean alpha edges, generous padding, no backdrop, no scenery, no checkerboard, "
    "no watermark."
)


def _png(result):
    """Extract PNG bytes and the provider request identity from a response."""
    datum = result.data[0]
    if not getattr(datum, "b64_json", None):
        raise RuntimeError("Response carried no b64_json payload.")
    return base64.b64decode(datum.b64_json)


def run():
    oai = client()
    findings = {
        "model": conf.IMAGE_MODEL,
        "size": SIZE,
        "quality": QUALITY,
        "ran_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "steps": {},
    }

    # 1. Opaque concept, as CONCEPT would produce.
    print(f"[1/3] generate opaque concept  ({conf.IMAGE_MODEL}) ...", flush=True)
    concept = _png(
        oai.images.generate(
            model=conf.IMAGE_MODEL,
            prompt=CONCEPT_PROMPT,
            size=SIZE,
            quality=QUALITY,
            output_format="png",
            background="opaque",
        )
    )
    save("concept.png", concept)
    findings["steps"]["generate_opaque_concept"] = alpha_report(concept)
    print("      ok:", findings["steps"]["generate_opaque_concept"]["mode"], flush=True)

    # 2. The critical one: edit that concept and demand genuine transparency.
    #    output_compression is deliberately omitted - it is invalid for PNG.
    print("[2/3] edit concept -> transparent overlay  (the critical case) ...", flush=True)
    overlay = _png(
        oai.images.edit(
            model=conf.IMAGE_MODEL,
            image=("concept.png", concept, "image/png"),
            prompt=OVERLAY_PROMPT,
            size=SIZE,
            quality=QUALITY,
            output_format="png",
            background="transparent",
        )
    )
    save("overlay.png", overlay)
    findings["steps"]["edit_to_transparent_overlay"] = alpha_report(overlay)
    print("      ok:", findings["steps"]["edit_to_transparent_overlay"]["mode"], flush=True)

    # 3. Direct transparent generation.
    print("[3/3] generate transparent asset ...", flush=True)
    asset = _png(
        oai.images.generate(
            model=conf.IMAGE_MODEL,
            prompt=TRANSPARENT_ASSET_PROMPT,
            size=SIZE,
            quality=QUALITY,
            output_format="png",
            background="transparent",
        )
    )
    save("asset.png", asset)
    findings["steps"]["generate_transparent_asset"] = alpha_report(asset)
    print("      ok:", findings["steps"]["generate_transparent_asset"]["mode"], flush=True)

    (save("probe_image.json", json.dumps(findings, indent=2).encode()))

    overlay_ok = findings["steps"]["edit_to_transparent_overlay"]["real_alpha"]
    asset_ok = findings["steps"]["generate_transparent_asset"]["real_alpha"]

    print("\n" + json.dumps(findings, indent=2))
    if not overlay_ok:
        print(
            "\nFAIL: the edit path did not return a usable alpha channel. "
            "DECORATION_OVERLAY cannot be implemented. Stop and report; do not key "
            "out white and do not fall back to gpt-image-2.",
            file=sys.stderr,
        )
        return 1
    if not asset_ok:
        print("\nWARN: direct transparent generation looks weak.", file=sys.stderr)
    print("\nPASS: genuine RGBA on the edit path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
