"""templateFlexibility: how closely the structural template must follow the approved concept.

`designFreedom` decides how freely the CONCEPT is generated from the reference.
`templateFlexibility` is a different dial: once the concept is approved, it decides how
much APPEARANCE variation is tolerated when the template (the "Design Base": fills,
torn edges, ornaments) is reconstructed from it. It never moves artwork, never changes
the approved composition and never touches designFreedom.

The integer 0-100 selects one of four profiles. A profile is a set of concrete
tolerances for LOCAL differences:

    contourShift    how far a contour may sit from where the concept has it, as a
                    fraction of the canvas side (a torn edge re-drawn a little rougher)
    textureDelta    mean colour difference tolerated inside artwork that is present in
                    both (grain, scratches, paper)
    featherWidth    share of the template's pixels that may be soft-edged
    fillExtent      area of scattered added artwork tolerated, as a fraction of the canvas
    gradientVeil    area of translucent veil tolerated that the concept does not have

These are NOT a similarity percentage, and a flexibility of 40 does NOT permit moving
artwork 40% of the canvas. They bound local variation only.

INVARIANTS are the same in every profile, including 100, because they are what makes a
template wrong rather than different:

    * artwork the concept does not have - farther than MAJOR_REACH from any like-coloured
      artwork in the concept - whose extent crosses MAJOR_SPAN of the canvas side (an
      added border or a continued frame), or whose area exceeds MAJOR_AREA;
    * text, contact accuracy, editability, direction, readability and subject
      visibility are not template concerns and are never relaxed by this dial.

The profile used is persisted on the job at acceptance and read by generation (the
extraction prompt), review (the template check) and repair (the repair prompt), so the
same tolerance applies at all three. Change a number here and bump POLICY_VERSION.
"""

from __future__ import annotations

POLICY_VERSION = "template-tolerance-1"

DEFAULT_FLEXIBILITY = 20

#: An added structure spanning this share of the canvas side is major in every profile.
MAJOR_SPAN = 0.30
#: ... and so is any added area this large, however compact.
MAJOR_AREA = 0.012
#: Artwork farther than this (share of the canvas side) from any like-coloured artwork in
#: the concept is ADDED, however rough the contour variation the profile allows. Same in
#: every profile: a torn edge may be re-drawn a few percent off, a border is not an edge.
MAJOR_REACH = 0.03
#: Added regions smaller than this share of the canvas are noise at the working size.
NOISE_AREA = 0.0004

PROFILES = (
    {"name": "strict", "upTo": 0,
     "contourShift": 0.004, "textureDelta": 0.04, "featherWidth": 0.01,
     "fillExtent": 0.002, "gradientVeil": 0.002,
     "intent": "Reconstruct the template as closely as the concept allows; do not "
               "change contours, texture or fill extent."},
    {"name": "small", "upTo": 20,
     "contourShift": 0.010, "textureDelta": 0.08, "featherWidth": 0.03,
     "fillExtent": 0.004, "gradientVeil": 0.006,
     "intent": "Small appearance variation is acceptable: a torn edge or texture may "
               "differ in detail, but its location and footprint stay the same."},
    {"name": "moderate", "upTo": 40,
     "contourShift": 0.020, "textureDelta": 0.14, "featherWidth": 0.06,
     "fillExtent": 0.008, "gradientVeil": 0.012,
     "intent": "Moderate appearance variation is acceptable in contour, texture and "
               "softness; structural fills keep their place and extent."},
    {"name": "broad", "upTo": 100,
     "contourShift": 0.035, "textureDelta": 0.22, "featherWidth": 0.10,
     "fillExtent": 0.010, "gradientVeil": 0.02,
     "intent": "Broader appearance variation is acceptable within the structural "
               "invariants: no added borders, frames or ornaments, and no change to "
               "where the structural fills are."},
)


def validate(value):
    """A request's templateFlexibility: an int 0-100. bool, float and str are refused."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("templateFlexibility must be an integer from 0 through 100")
    return value


def profile_for(flexibility):
    """The tolerance profile for a flexibility value (None means the default)."""
    value = DEFAULT_FLEXIBILITY if flexibility is None else validate(int(flexibility))
    for profile in PROFILES:
        if value <= profile["upTo"]:
            return profile
    return PROFILES[-1]


def resolve(flexibility):
    """The document persisted on the job: the value, the profile it selected and the version.

    Persisting the numbers, not just the name, means a later change to a profile cannot
    silently change the tolerance an already-accepted job is judged by.
    """
    value = DEFAULT_FLEXIBILITY if flexibility is None else validate(int(flexibility))
    profile = profile_for(value)
    return {
        "value": value,
        "profile": profile["name"],
        "policyVersion": POLICY_VERSION,
        "tolerances": {k: profile[k] for k in (
            "contourShift", "textureDelta", "featherWidth", "fillExtent", "gradientVeil")},
        "invariants": {"majorSpan": MAJOR_SPAN, "majorArea": MAJOR_AREA,
                       "majorReach": MAJOR_REACH},
        "intent": profile["intent"],
    }


def for_job(job):
    """The tolerance a job is judged by, or None for a job accepted before it existed."""
    return (job or {}).get("templateTolerance") or None


def prompt_clause(tolerance):
    """The sentence that carries the persisted tolerance into a generation or repair prompt."""
    if not tolerance:
        return ""
    return (
        f"Template tolerance ({tolerance['profile']}): {tolerance['intent']} "
        "Never add a border, frame, contour or ornament that the image does not "
        "already have, and never join separate ornaments into a continuous shape."
    )
