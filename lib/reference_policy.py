"""Translate `designFreedom` into an explicit reference-adherence policy.

SPEC section 2.7. `designFreedom` is a creative-control setting, not a measured
pixel difference, similarity score or provider parameter. This module turns the
number into constraints stated in plain English, which the concept prompt and the
concept review both consume.

Pure: no I/O, no configuration, no provider calls.
"""

from __future__ import annotations

POLICY_VERSION = "1.0"

STRICT = "strict-adherence"
CLOSE = "close-adaptation"
GUIDED = "guided-adaptation"
BROAD = "broad-inspiration"
FREE = "free-composition"

MODES = (STRICT, CLOSE, GUIDED, BROAD, FREE)

#: Adaptations that are always permitted and are never policy violations.
DOCUMENTED_EXCEPTIONS = (
    "Replace all source text, business names, contacts, identities and irrelevant subject matter with the target content.",
    "Supplied brand colours override the source colours.",
    "Use an approved available font of equivalent visual character when the exact source font is unavailable.",
    "When a different aspect ratio is requested, map source anchors into the target canvas and document the adaptation.",
)

_INSTRUCTIONS = {
    STRICT: (
        "Reproduce the reference composition as closely as practical. Preserve the "
        "dominant image placement and scale, graphic-panel geometry, overlap "
        "relationships, typography hierarchy and reading flow. Make no discretionary "
        "redesign. Adapt only what is necessary for the supplied target copy, assets "
        "and brand. Replace all source text, business identities and irrelevant "
        "subject matter. Record necessary deviations."
    ),
    CLOSE: (
        "Preserve the reference composition, dominant image placement and relative "
        "scale, graphic-panel geometry, overlap relationships, typography hierarchy "
        "and reading flow. Adapt only what is necessary for the supplied target copy, "
        "assets and brand. Replace all source text, business identities and "
        "irrelevant subject matter. Avoid a new generic layout. Record necessary "
        "deviations."
    ),
    GUIDED: (
        "Adapt selected reference visual qualities and hierarchy to the target brief. "
        "You may reorganize regions and proportions to improve content fit and visual "
        "impact. Explain the retained inspiration and deliberate structural changes."
    ),
    BROAD: (
        "Treat the reference mainly as an atmosphere and style reference. Major "
        "structural changes are allowed. Carry over selected visual qualities such as "
        "lighting, palette, depth and typographic character, and reorganize layout, "
        "hierarchy and proportions as the target brief requires. Replace all source "
        "text and business identities. Explain which qualities were retained."
    ),
    FREE: (
        "Create the strongest composition for the target brief and brand. You may "
        "redesign all layout, scale, hierarchy and visual placement. The reference is "
        "optional inspiration, not a geometric constraint. Do not copy source "
        "identities or factual content."
    ),
}

_MUST_PRESERVE = {
    STRICT: [
        "Overall composition and reading flow",
        "Dominant image placement and scale",
        "Graphic-panel geometry and overlaps",
        "Typography hierarchy and relative text scale",
    ],
    CLOSE: [
        "Recognizable layout and major anchors",
        "Dominant image placement and relative scale",
        "Graphic-panel geometry and overlap relationships",
        "Typography hierarchy and reading flow",
    ],
    GUIDED: [
        "Selected visual qualities and overall hierarchy",
    ],
    BROAD: [
        "Selected visual qualities such as lighting, palette and depth",
    ],
    FREE: [],
}

_MAY_CHANGE = {
    STRICT: [
        "Localized fitting needed for the target copy, supplied assets and brand",
    ],
    CLOSE: [
        "Localized adjustments for target copy, supplied assets, brand and fit",
    ],
    GUIDED: [
        "Individual regions and proportions when justified by the brief",
        "At 50, meaningful restructuring",
    ],
    BROAD: [
        "Major structural changes",
        "Layout, hierarchy and proportions",
    ],
    FREE: [
        "Structure, hierarchy, placements and proportions",
        "Everything about the source layout",
    ],
}


def mode_for(design_freedom: int) -> str:
    """Map the numeric setting to its policy band (SPEC section 2.7 table)."""
    if design_freedom == 0:
        return STRICT
    if design_freedom <= 25:
        return CLOSE
    if design_freedom <= 50:
        return GUIDED
    if design_freedom <= 75:
        return BROAD
    return FREE


def derive_reference_policy(design_freedom: int) -> dict:
    """Return the policy for a validated 0..100 integer."""
    if (
        isinstance(design_freedom, bool)
        or not isinstance(design_freedom, int)
        or not 0 <= design_freedom <= 100
    ):
        raise ValueError("designFreedom must be an integer from 0 through 100")
    mode = mode_for(design_freedom)
    return {
        "designFreedom": design_freedom,
        "policyVersion": POLICY_VERSION,
        "mode": mode,
        "mustPreserve": list(_MUST_PRESERVE[mode]),
        "mayChange": list(_MAY_CHANGE[mode]),
        "documentedExceptions": list(DOCUMENTED_EXCEPTIONS),
        "instruction": _INSTRUCTIONS[mode],
    }
