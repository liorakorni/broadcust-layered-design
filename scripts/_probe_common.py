"""Shared helpers for the Phase 1 capability probes.

Probes hit the paid API. Keep calls few, small and low quality.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "scripts" / "probe_out"


def load_api_key():
    """Read the OpenAI key from the environment or the gitignored stage config."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        return key
    for stage in ("dev", "live"):
        path = REPO / f"config.{stage}.json"
        if path.exists():
            value = json.loads(path.read_text()).get("openai_api_key", "")
            if value and value != "YOUR_OPENAI_API_KEY":
                return value
    print(
        "No OpenAI API key. Set OPENAI_API_KEY or fill config.dev.json.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def client():
    from openai import OpenAI

    return OpenAI(api_key=load_api_key(), timeout=240.0)


def alpha_report(png_bytes):
    """Measure the alpha channel of a PNG.

    lib/validate_assets.py is calibrated from these numbers, so this reports the
    actual distribution rather than a pass/fail on a guessed threshold. In
    particular it checks whether "opaque" pixels really reach 255, since GPT Image
    has been reported to return 252-254 with a grey halo at the alpha edge.
    """
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes))
    info = {
        "mode": img.mode,
        "size": list(img.size),
        "has_alpha": img.mode in ("RGBA", "LA") or "transparency" in img.info,
    }
    if img.mode != "RGBA":
        img = img.convert("RGBA")
        info["converted_to_rgba"] = True

    alpha = img.getchannel("A")
    hist = alpha.histogram()
    total = sum(hist)
    fully_transparent = sum(hist[0:16])
    fully_opaque = hist[255]
    near_opaque = sum(hist[250:256])
    partial = total - fully_transparent - near_opaque

    present = [value for value, count in enumerate(hist) if count]
    info.update(
        {
            "total_pixels": total,
            "alpha_min": present[0] if present else None,
            "alpha_max": present[-1] if present else None,
            "distinct_alpha_values": len(present),
            "pct_transparent_lt16": round(100.0 * fully_transparent / total, 3),
            "pct_opaque_eq255": round(100.0 * fully_opaque / total, 3),
            "pct_opaque_gte250": round(100.0 * near_opaque / total, 3),
            "pct_partial": round(100.0 * partial / total, 3),
            "top_alpha_values": sorted(
                ((v, hist[v]) for v in present), key=lambda p: -p[1]
            )[:8],
        }
    )
    info["real_alpha"] = (
        info["pct_transparent_lt16"] > 0.5 and info["pct_opaque_gte250"] > 0.5
    )
    return info


def save(name, data):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    path.write_bytes(data)
    return path
