"""Translate `designFreedom` into an explicit reference-adherence policy.

SPEC section 2.7. `designFreedom` is a creative-control setting, not a measured
pixel difference, similarity score or provider parameter. This module turns the
number into constraints stated in plain English, which the concept prompt, the
concept review and every concept correction all consume.

The policy is deliberately content-neutral. The brief controls subject matter and
approved text; the reference controls layout and visual styling. Nothing here may
require a person, a pose, an object category or exact body bounds merely because
they appear in the reference. How strictly a deviation is judged is NOT decided
here: that is `reviewStrictness` (lib/review_policy.py), carried in the same
policy document so one versioned artifact describes both.

Pure: no I/O, no configuration, no provider calls.
"""

from __future__ import annotations

from lib import review_policy

#: 2.0: content-neutral structural constraints, `reviewStrictness` and the
#: severity policy, planned copy placements in the reference analysis. A 1.0
#: policy or analysis is never reused under 2.0 (see lib/policy_context.py).
POLICY_VERSION = "2.0"

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
        "Reproduce the reference layout and visual styling as closely as practical. "
        "Preserve the placement and scale of the dominant image region, graphic-panel "
        "geometry, overlap relationships, typography hierarchy and reading flow. Make "
        "no discretionary redesign. The brief, not the reference, decides what the "
        "image shows: take its subject matter from the brief. Adapt only what is "
        "necessary for the supplied target copy, assets and brand. Replace all source "
        "text, business identities and source subject matter. Record necessary "
        "deviations."
    ),
    CLOSE: (
        "Preserve the reference layout, the placement and relative scale of the "
        "dominant image region, graphic-panel geometry, overlap relationships, "
        "typography hierarchy and reading flow. The brief, not the reference, decides "
        "what the image shows. Adapt only what is necessary for the supplied target "
        "copy, assets and brand. Replace all source text, business identities and "
        "source subject matter. Avoid a new generic layout. Record necessary "
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
        "Placement and scale of the dominant image region (position, size and edge cropping; not its subject)",
        "Graphic-panel geometry and overlaps",
        "Typography hierarchy and relative text scale",
    ],
    CLOSE: [
        "Recognizable layout and major anchors",
        "Placement and relative scale of the dominant image region (not its subject)",
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


def derive_reference_policy(
    design_freedom: int,
    review_strictness: str = review_policy.DEFAULT_STRICTNESS,
) -> dict:
    """Return the versioned policy for a validated 0..100 integer.

    `review_strictness` is independent of `design_freedom`: the first governs how
    freely the concept is generated, the second how its differences are judged.
    """
    if (
        isinstance(design_freedom, bool)
        or not isinstance(design_freedom, int)
        or not 0 <= design_freedom <= 100
    ):
        raise ValueError("designFreedom must be an integer from 0 through 100")
    mode = mode_for(design_freedom)
    review = review_policy.derive_review_policy(review_strictness)
    return {
        "designFreedom": design_freedom,
        "policyVersion": POLICY_VERSION,
        "reviewStrictness": review["reviewStrictness"],
        "reviewPolicy": review,
        "contentBoundary": list(review["contentBoundary"]),
        "mode": mode,
        "mustPreserve": list(_MUST_PRESERVE[mode]),
        "mayChange": list(_MAY_CHANGE[mode]),
        "documentedExceptions": list(DOCUMENTED_EXCEPTIONS),
        "instruction": _INSTRUCTIONS[mode],
    }
