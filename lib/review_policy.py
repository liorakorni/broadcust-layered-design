"""Review strictness and the severity policy behind every concept decision.

`designFreedom` says how much the generator MAY depart from the reference.
`reviewStrictness` says how much departure the reviewer TOLERATES. They are
separate on purpose: asking for maximum adherence (designFreedom 0) tells the
generator to try hard, it does not turn every pixel difference into a failure.

The policy has one rule that outranks all others: a candidate is judged on what
a difference DOES to the design, not on how different it is. Similarity never
overrides a mandatory content or technical requirement, and there is no
"N% overlap" threshold anywhere - a small difference that harms the composition
blocks, a large one that does not harm it only warns.

Pure: no I/O, no configuration, no provider calls. The same module renders the
rules into the generation, review and correction prompts, which is what keeps
the three consistent.
"""

from __future__ import annotations

import copy

BALANCED = "balanced"
STRICTNESS_LEVELS = (BALANCED,)
DEFAULT_STRICTNESS = BALANCED

WARNING = "warning"
BLOCKING = "blocking"
SEVERITIES = (WARNING, BLOCKING)

VISUAL = "visual"
CONTENT = "content"
TECHNICAL = "technical"
#: What the vision model may declare. The backend, not the model, decides what a
#: category is allowed to mean (see `normalize_issue`).
ISSUE_CATEGORIES = (VISUAL, CONTENT, TECHNICAL)

#: Categories an older review (policy 1.0) used, mapped onto the current three.
LEGACY_CATEGORY = {
    "layoutAdherence": VISUAL,
    "composition": VISUAL,
    "readability": CONTENT,
    "content": CONTENT,
    "other": VISUAL,
}

# Required concept-stage checks. A check that did not run is reported as
# "missing" and never counts as passing.
CHECK_MANDATORY_COPY = "mandatoryCopy"
CHECK_REFERENCE_STRUCTURE = "referenceStructure"
#: Whether the concept is composed in the resolved direction. A required check
#: rather than a visual issue: a left-to-right composition for Hebrew copy cannot
#: be corrected by a text or layout patch later, so it has to be caught here,
#: inside the existing concept-correction bound.
CHECK_COMPOSITION_DIRECTION = "compositionDirection"
REQUIRED_CONCEPT_CHECKS = (
    CHECK_MANDATORY_COPY, CHECK_REFERENCE_STRUCTURE, CHECK_COMPOSITION_DIRECTION,
)

PASSED = "passed"
FAILED = "failed"
MISSING = "missing"
NOT_APPLICABLE = "notApplicable"

_SEVERITY_RULES = {
    BALANCED: {
        "warning": [
            "Small differences in position, spacing or size.",
            "Minor decorative variations.",
            "An extra subtle line or ornament that does not harm the composition.",
            "Natural photographic differences that preserve the visual balance.",
            "Localized typography adjustments needed for Hebrew or the supplied copy.",
        ],
        "blockingVisual": [
            "A material change to the intended composition or hierarchy.",
            "Main imagery that interferes with the text.",
            "A significant loss of the intended emphasis.",
            "Overlaps, cropping or contrast problems that materially harm usability.",
        ],
        "blockingContent": [
            "Missing mandatory approved text, including the business identity when required.",
            "Incorrect contact details.",
            "Unsupported claims or forbidden content.",
            "Required text that is unreadable.",
        ],
        "blockingTechnical": [
            "Broken HTML or missing assets.",
            "Required text that is not editable.",
            "Layer separation or alpha that is invalid and breaks the composition.",
            "Final rendering failures.",
        ],
    }
}

#: What the reference does and does not control. Used by generation, review and
#: correction alike, so no stage can quietly demand the reference's subject.
CONTENT_BOUNDARY = (
    "The brief decides subject matter, approved text and brand. The reference "
    "decides layout and visual styling only.",
    "Never require a person, a pose, an object category, equipment or exact "
    "body or subject bounds merely because they appear in the reference.",
    "Judge image regions by position, size, edge cropping and visual role, not by "
    "what they depict.",
)


def validate_strictness(value):
    """Return a supported strictness level or raise ValueError naming the options."""
    if not isinstance(value, str) or value not in STRICTNESS_LEVELS:
        raise ValueError(
            "reviewStrictness must be one of: " + ", ".join(STRICTNESS_LEVELS)
        )
    return value


def derive_review_policy(strictness=DEFAULT_STRICTNESS):
    """The versioned review policy that is persisted next to the reference policy."""
    level = validate_strictness(strictness)
    return {
        "reviewStrictness": level,
        "severityRules": copy.deepcopy(_SEVERITY_RULES[level]),
        "contentBoundary": list(CONTENT_BOUNDARY),
        "requiredConceptChecks": list(REQUIRED_CONCEPT_CHECKS),
        "principle": (
            "Judge what a difference does to the design, not how different it is. "
            "Visual similarity never overrides a mandatory content or technical "
            "requirement, and minor visual differences alone never fail a job."
        ),
    }


def severity_block(review_policy):
    """Render the severity rules as the one text every prompt shares."""
    rules = review_policy["severityRules"]

    def bullets(items):
        return "\n".join(f"  - {i}" for i in items)

    return "\n".join(
        [
            f"REVIEW POLICY ({review_policy['reviewStrictness']}):",
            review_policy["principle"],
            "",
            "WARNING (the design continues; no correction is made for these):",
            bullets(rules["warning"]),
            "BLOCKING VISUAL (category \"visual\", severity \"blocking\"):",
            bullets(rules["blockingVisual"]),
            "BLOCKING CONTENT (category \"content\", always blocking):",
            bullets(rules["blockingContent"]),
            "BLOCKING TECHNICAL (category \"technical\", always blocking):",
            bullets(rules["blockingTechnical"]),
            "",
            "Reference boundary:",
            "\n".join(f"  - {i}" for i in review_policy["contentBoundary"]),
        ]
    )


# --------------------------------------------------------------------------
# Issue normalisation
# --------------------------------------------------------------------------


def issue_message(issue):
    """One caller-readable sentence assembled from the structured fields."""
    parts = [f"{issue['affected']}: {issue['impact']}".strip(": ").strip()]
    if issue.get("suggestion"):
        parts.append(f"Fix: {issue['suggestion']}")
    return " ".join(p for p in parts if p)


def normalize_issue(raw):
    """Apply the backend's severity rules to one model-reported issue.

    * Content and technical issues are always blocking. The model cannot talk a
      missing business name or a broken asset down to a warning.
    * A visual issue is blocking only when it names the element it affects and
      explains the actual impact. A "blocking" visual finding that cannot say
      what it harms is an opinion about similarity, and similarity alone must not
      fail a job - so it is kept, as a warning, with the reason recorded.
    """
    category = raw.get("category")
    category = LEGACY_CATEGORY.get(category, category)
    if category not in ISSUE_CATEGORIES:
        category = VISUAL
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        severity = BLOCKING
    affected = (raw.get("affected") or "").strip()
    impact = (raw.get("impact") or "").strip()
    suggestion = (raw.get("suggestion") or None)
    suggestion = suggestion.strip() if isinstance(suggestion, str) and suggestion.strip() else None

    adjustment = None
    if category in (CONTENT, TECHNICAL) and severity != BLOCKING:
        severity = BLOCKING
        adjustment = f"{category} issues are always blocking"
    elif category == VISUAL and severity == BLOCKING and not (affected and impact):
        severity = WARNING
        adjustment = "downgraded: a blocking visual issue must name the affected element and its actual impact"

    issue = {
        "code": raw.get("code") or "UNSPECIFIED",
        "category": category,
        "severity": severity,
        "affected": affected or "unspecified",
        "impact": impact or "No impact was described.",
        "suggestion": suggestion,
        "region": raw.get("region"),
    }
    if adjustment:
        issue["severityAdjustment"] = adjustment
    issue["message"] = issue_message(issue)
    return issue


def split_issues(issues):
    """Return `(blocking, warnings)` from normalized issues."""
    blocking = [i for i in issues if i["severity"] == BLOCKING]
    warnings = [i for i in issues if i["severity"] != BLOCKING]
    return blocking, warnings


# --------------------------------------------------------------------------
# Concept evaluation
# --------------------------------------------------------------------------


def evaluate_concept(verdict, policy, copy_items, placements=None, anchor_ids=()):
    """Turn a parsed concept-review verdict into the backend's decision.

    `verdict` is a plain dict (the model's own words, already schema-checked).
    Nothing the model asserts about passing is consulted: eligibility is derived
    here from the evidence it reported.

    Eligible means all of:
      * every approved copy item is present, correct and readable;
      * every required check actually ran (a missing one is never a pass);
      * no blocking visual, content or technical issue remains;
      * the model did not report an unresolvable constraint conflict.
    """
    placements = placements or {}
    issues = [normalize_issue(i) for i in verdict.get("issues", [])]
    if not policy.get("mustPreserve"):
        # Free composition: the reference's structure is not binding, so a
        # departure from one of its anchors is not a blocker. Anything else
        # visual - a poor composition, an unreadable layout - still is.
        anchors = set(anchor_ids)
        for issue in issues:
            if (
                issue["category"] == VISUAL
                and issue["severity"] == BLOCKING
                and issue["affected"] in anchors
            ):
                issue["severity"] = WARNING
                issue["severityAdjustment"] = (
                    "downgraded: the reference structure is not binding at this designFreedom"
                )

    copy_checks, copy_status, copy_issues = _copy_evaluation(
        verdict.get("copyChecks", []), copy_items, placements, issues
    )
    issues.extend(copy_issues)

    structure_status = _structure_status(verdict.get("checks", []), policy)
    direction_status = _direction_status(verdict, policy)
    if direction_status == FAILED:
        # Recorded as an issue as well as a failed check, because the concept
        # correction works from the blocking issues: without one it would be
        # told the concept failed and not what to change.
        resolved = policy.get("direction") or {}
        issues.append(normalize_issue({
            "code": "COMPOSITION_DIRECTION_WRONG",
            "category": VISUAL,
            "severity": BLOCKING,
            "affected": "composition",
            "impact": (
                "The composition runs the wrong way for the approved copy "
                f"(resolved {resolved.get('layoutDirection')}), so the reading "
                "order and the column positions are wrong for the language. "
                "Translating a left-to-right composition is not the same design."
            ),
            "suggestion": resolved.get("reason"),
            "region": None,
        }))
    required = [
        {"name": CHECK_MANDATORY_COPY, "status": copy_status},
        {"name": CHECK_REFERENCE_STRUCTURE, "status": structure_status},
        {"name": CHECK_COMPOSITION_DIRECTION, "status": direction_status},
    ]

    blocking, warnings = split_issues(issues)
    incomplete = [c["name"] for c in required if c["status"] in (MISSING,)]
    failed_checks = [c["name"] for c in required if c["status"] == FAILED]
    structural_failures = sum(
        1 for c in verdict.get("checks", []) if not c.get("passed", False)
    )
    conflicted = bool((verdict.get("constraintConflict") or {}).get("conflicted"))

    eligible = not blocking and not incomplete and not failed_checks and not conflicted
    return {
        "eligible": eligible,
        "issues": issues,
        "blockingIssues": blocking,
        "warnings": warnings,
        "copyChecks": copy_checks,
        "requiredChecks": required,
        "incompleteChecks": incomplete,
        "structuralFailures": structural_failures,
    }


def _copy_evaluation(reported, copy_items, placements, model_issues):
    """Deterministic mandatory-content check against the approved manifest."""
    by_key = {}
    for entry in reported:
        by_key.setdefault(entry["contentKey"], []).append(entry)
    already = {
        i["affected"] for i in model_issues if i["category"] == CONTENT
    }

    checks = []
    issues = []
    for item in copy_items:
        key = item["contentKey"]
        entries = by_key.get(key)
        if not entries:
            # The review did not look at this item: that is an incomplete review,
            # not evidence that the text is there.
            checks.append(
                {"contentKey": key, "text": item["text"], "status": MISSING,
                 "observed": "The review did not report on this item."}
            )
            continue
        # More than one entry for a key: take the worst, never the best.
        entry = sorted(entries, key=lambda e: (e["present"], e["correct"], e["readable"]))[0]
        ok = entry["present"] and entry["correct"] and entry["readable"]
        checks.append(
            {
                "contentKey": key,
                "text": item["text"],
                "status": PASSED if ok else FAILED,
                "present": entry["present"],
                "correct": entry["correct"],
                "readable": entry["readable"],
                "observed": entry["observed"],
            }
        )
        if ok:
            continue
        if key in already:
            continue
        if not entry["present"]:
            code, what = "MISSING_REQUIRED_COPY", "is missing from the concept"
        elif not entry["correct"]:
            code, what = "INCORRECT_REQUIRED_COPY", "is not the approved wording"
        else:
            code, what = "UNREADABLE_REQUIRED_COPY", "is not readable"
        issue = {
            "code": code,
            "category": CONTENT,
            "severity": BLOCKING,
            "affected": key,
            "impact": f"Approved text {item['text']!r} {what}.",
            "suggestion": (
                f"Place exactly {item['text']!r} at its planned position."
                if key in placements else f"Show exactly {item['text']!r}."
            ),
            "region": placements.get(key),
        }
        issue["message"] = issue_message(issue)
        issues.append(issue)

    # A failed item outranks an unreported one: the check is known to be failing.
    if any(c["status"] == FAILED for c in checks):
        status = FAILED
    elif any(c["status"] == MISSING for c in checks):
        status = MISSING
    else:
        status = PASSED
    return checks, status, issues


def _direction_status(verdict, policy):
    """Did the concept come out in the direction the policy resolved?

    `notApplicable` when the policy carries no direction (a job whose policy
    predates it): there is nothing to have got wrong.
    """
    if not (policy.get("direction") or {}):
        return NOT_APPLICABLE
    reported = verdict.get("compositionDirectionOk")
    if reported is None:
        return MISSING
    return PASSED if reported else FAILED


def _structure_status(checks, policy):
    """The reference-structure check ran if the review reported its checks.

    The prompt asks for one check per "must preserve" item. Fewer entries than
    items means part of the structure was never examined.
    """
    required = len(policy.get("mustPreserve") or [])
    if required == 0:
        return NOT_APPLICABLE
    if len(checks) < required:
        return MISSING
    return PASSED


# --------------------------------------------------------------------------
# Ranking and selection
# --------------------------------------------------------------------------


def rank_key(candidate):
    """Lower is better.

    Blocking issues first, then structural adherence, then the number of
    warnings that matter. The attempt number breaks ties toward the EARLIER
    candidate: nothing about a later attempt makes it better, so it is never
    preferred merely for being the most recent.
    """
    return (
        len(candidate["blockingIssues"]),
        candidate["structuralFailures"],
        len(candidate["warnings"]),
        int(candidate["attempt"]),
    )


def select_best(candidates):
    """The best ELIGIBLE candidate, or None. Never falls back to ineligible."""
    eligible = [c for c in candidates if c["eligible"]]
    return min(eligible, key=rank_key) if eligible else None


def best_correction_base(candidates):
    """The candidate cheapest to repair, used as the editing base.

    Ranked like `select_best` but across every candidate: when none is eligible
    the base is the one closest to it.
    """
    return min(candidates, key=rank_key) if candidates else None


def selection_reason(selected, candidates):
    """Why this candidate: persisted with the selection, never reconstructed."""
    if len(candidates) == 1:
        return (
            f"Attempt {selected['attempt']} is eligible: all mandatory content checks "
            f"passed, every required check completed and no blocking issue remains."
        )
    summary = "; ".join(
        f"attempt {c['attempt']}: "
        + ("eligible" if c["eligible"] else f"{len(c['blockingIssues'])} blocking")
        + f", {c['structuralFailures']} structural failure(s), "
        f"{len(c['warnings'])} warning(s)"
        for c in sorted(candidates, key=lambda c: int(c["attempt"]))
    )
    return (
        f"Attempt {selected['attempt']} is the best eligible candidate (fewest "
        f"structural failures, then fewest warnings; ties go to the earlier "
        f"attempt). Considered: {summary}."
    )
