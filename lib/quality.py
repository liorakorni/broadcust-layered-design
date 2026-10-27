"""A quality score for the delivered candidate: a summary of findings, not a verdict.

Built only from what the pipeline already produced for that candidate - the review's
findings, the render checks, the measured distance from the approved concept, the layer
contract. It makes no model call, and a failure here never discards the HTML (the caller
treats an exception as "no score").

THE RUBRIC IS PROVISIONAL. The weights, the penalties and the bands below are reasoned
defaults, not calibrated: no set of designs scored by people exists yet to fit them to.
Read the score as "how much is still open", compare it only between candidates of the
same job, and do not set a publish threshold on it. It is never the reason a candidate
is chosen (lib/candidates.py ranks on ordered criteria) and never makes a candidate
publishable: a critical finding overrides everything, so a design that scores 90 and is
missing mandatory text is still not publish-ready.

Each dimension is 0-100. The total is the weighted mean over the dimensions that apply
(concept adherence does not apply to a layout-first job and its weight is redistributed,
and says so). If a dimension that applies could not be assessed, the whole score is null
with the reason, rather than a number that quietly assumes the best.
"""

from __future__ import annotations

from lib import finding_identity

RUBRIC_VERSION = "quality-rubric-1"

#: Provisional. Sum to 100.
WEIGHTS = {
    "content": 30,
    "readabilityTypography": 25,
    "layoutComposition": 20,
    "editability": 15,
    "conceptAdherence": 10,
}

#: Points removed for one open finding of each severity, from a dimension.
BLOCKING_PENALTY = 30
WARNING_PENALTY = 8

#: Finding families by dimension (lib/finding_identity.py names them).
FAMILIES = {
    "content": ("content",),
    "readabilityTypography": ("readability", "headline", "typography", "clipping", "overlap"),
    "editability": ("icon",),
}
# Everything else a review can say about the picture belongs to layout and composition.

#: Distance from the approved concept (0 identical) at or below which adherence is full,
#: and at which it reaches zero. The tolerance is widened by the persisted
#: templateFlexibility, so a request that allowed more difference is not scored down for it.
FIDELITY_FULL = 0.05
FIDELITY_ZERO = 0.30

#: Bands, provisional.
BANDS = ((85, "good"), (70, "acceptable"), (0, "needs-work"))


def _family(issue):
    return finding_identity.family_of(issue)


def _dimension_of(family):
    for name, families in FAMILIES.items():
        if family in families:
            return name
    return "layoutComposition"


def assess(*, review, candidate, job, layer_contract, has_concept=True):
    """`{qualityScore, qualityBreakdown, rubricVersion, qualityReason, critical, ...}`."""
    base = {"rubricVersion": RUBRIC_VERSION, "provisional": True}
    if not review or "blockingIssues" not in review:
        return {**base, "qualityScore": None, "qualityBreakdown": None, "critical": [],
                "qualityReason": "The review record is incomplete, so nothing was scored."}

    penalties = {name: 0 for name in WEIGHTS}
    findings = {name: [] for name in WEIGHTS}
    critical = []
    for severity, issues, penalty in (
        ("blocking", review.get("blockingIssues") or [], BLOCKING_PENALTY),
        ("warning", review.get("warnings") or [], WARNING_PENALTY),
    ):
        for issue in issues:
            dimension = _dimension_of(_family(issue))
            penalties[dimension] += penalty
            findings[dimension].append({"code": issue.get("code"), "severity": severity})
            if severity == "blocking" and dimension in ("readabilityTypography", "content"):
                critical.append({"code": issue.get("code"), "reason": f"blocking {dimension} finding"})

    scores = {name: max(0, 100 - penalties[name]) for name in WEIGHTS}
    notes = {name: None for name in WEIGHTS}

    if not review.get("mandatoryContentOk", False):
        scores["content"] = 0
        critical.append({"code": "MANDATORY_CONTENT_MISSING",
                         "reason": "an approved text is missing from the design"})
    failed = [c["name"] for c in review.get("renderChecks") or [] if c.get("status") != "passed"]
    if failed:
        scores["readabilityTypography"] = min(scores["readabilityTypography"], 40)
        notes["readabilityTypography"] = f"render checks not passed: {', '.join(sorted(failed))}"

    if layer_contract and layer_contract.get("checked"):
        if layer_contract["violations"]:
            scores["editability"] = max(0, scores["editability"] - 40)
            notes["editability"] = "layer-contract violations: " + ", ".join(
                sorted({v["code"] for v in layer_contract["violations"]}))
            critical.append({"code": layer_contract["violations"][0]["code"],
                             "reason": "the layer contract is violated"})
    fallbacks = [f for f in (job.get("fallbacksUsed") or {}).values()]
    if fallbacks:
        notes["editability"] = (notes["editability"] or "") + " fallback used (no editability loss)"

    applicable = dict(WEIGHTS)
    unassessed = []
    if not has_concept:
        applicable.pop("conceptAdherence")
        scores.pop("conceptAdherence")
        notes["conceptAdherence"] = "not applicable: no approved concept in this flow"
    else:
        fidelity = (candidate or {}).get("fidelity")
        if fidelity is None:
            unassessed.append("conceptAdherence")
        else:
            widen = 1 + float((job.get("templateTolerance") or {}).get("value", 20)) / 100.0
            full, zero = FIDELITY_FULL * widen, FIDELITY_ZERO * widen
            if fidelity <= full:
                scores["conceptAdherence"] = 100
            else:
                scores["conceptAdherence"] = max(
                    0, round(100 * (zero - fidelity) / (zero - full)))
            notes["conceptAdherence"] = f"distance {fidelity:.3f}, full credit up to {full:.3f}"

    if unassessed:
        return {**base, "qualityScore": None, "qualityBreakdown": None, "critical": critical,
                "qualityReason": "Not assessed: " + ", ".join(unassessed)
                + " could not be measured for this candidate."}

    total_weight = sum(applicable.values())
    score = round(sum(scores[name] * weight for name, weight in applicable.items())
                  / total_weight)
    breakdown = {
        name: {"score": scores[name], "weight": applicable[name],
               "openFindings": findings[name], "note": notes[name]}
        for name in applicable
    }
    return {
        **base, "qualityScore": score, "qualityBreakdown": breakdown,
        "qualityBand": next(label for floor, label in BANDS if score >= floor),
        "critical": critical, "qualityReason": None,
        "calibration": "Provisional weights and bands; not calibrated against human ratings. "
                       "Use to compare candidates of one job, not as a publish threshold.",
    }
