"""Design candidates: every assembled-and-reviewed attempt, ranked.

A repair loop produces several candidates, and the most recent is not necessarily
the best: a correction can fix one defect and introduce another, and a later
attempt can lose text an earlier one had. Each attempt is therefore kept as a
candidate (its immutable snapshot, render and review), and two decisions are made
from the full set rather than from "whatever came last":

  * which candidate the NEXT repair starts from  (`repair_base`)
  * which candidate is delivered                 (`select_deliverable`, and
                                                  `select_best_usable` when none
                                                  is approved)

The ranking is a priority order, not a single score, because a single score lets a
lot of one thing buy off a little of another. Earlier criteria always win:

  1. technical usability     the document, render, assets and snapshot are there
  2. mandatory content       every approved text is present
  3. readability             no blocking readability, overlap or clipping finding
  4. required editability    the independent components survived decomposition
  5. fidelity                a verified regression ranks below anything that is not,
                             then closeness to the approved concept
  6. remaining defects       blocking findings, then warnings
  7. the earlier attempt

Fewest blocking findings is deliberately *not* first. Measured on a live job, the
candidate with one blocking finding was a render that had lost the dark text
field, and the one with two had restored most of it; counting findings preferred
the worse picture. The pixel comparison against the approved concept (`fidelity`,
0 is identical) is the direct comparison used where the two reviews disagree
with each other.
"""

from __future__ import annotations

#: Fidelity differences smaller than this are noise between two renders of one
#: design, so they do not decide a ranking on their own.
FIDELITY_STEP = 0.01

#: Finding families that decide readability (see lib/finding_identity.py).
READABILITY_FAMILIES = ("readability", "overlap", "clipping")
EDITABILITY_FAMILIES = ("icon", "color")


def is_usable(candidate):
    """Whether a candidate could be handed back at all.

    Technical usability, and nothing about quality: the HTML exists with a recorded
    hash, the render exists, the assets it needs are listed, and the snapshot that
    describes them is named. Missing any of these means there is nothing coherent
    to return. It is checked again against storage when a candidate is chosen.
    """
    pointers = candidate.get("pointers") or {}
    assembled = pointers.get("ASSEMBLE") or {}
    rendered = pointers.get("RENDER") or {}
    return bool(
        candidate.get("htmlHash")
        and candidate.get("snapshotKey")
        and assembled.get("html")
        and assembled.get("scene")
        and assembled.get("assets")
        and rendered.get("native")
    )


def is_eligible(candidate):
    """A candidate may be delivered as passing only if it is approved and complete.

    Approved means the review found no blocking issue and every required render
    check passed. `mandatoryContentOk` is checked on its own as well, so a
    candidate with missing mandatory content is never delivered as passing even if
    some other path marked it approved.
    """
    return bool(candidate.get("approved")) and bool(candidate.get("mandatoryContentOk"))


def rank_key(candidate):
    fidelity = candidate.get("fidelity")
    return (
        0 if is_usable(candidate) else 1,
        0 if candidate.get("mandatoryContentOk") else 1,
        int(candidate.get("readabilityBlocking", 0)),
        int(candidate.get("editabilityBlocking", 0)),
        1 if candidate.get("progressVerdict") == "D" else 0,
        # Unmeasured fidelity ranks after measured: an unknown is not a good result.
        round(float(fidelity) / FIDELITY_STEP) if fidelity is not None else 10 ** 6,
        int(candidate.get("blockingCount", 0)),
        int(candidate.get("warningCount", 0)),
        int(candidate["attempt"]),
    )


def ranked(candidates):
    return sorted(candidates, key=rank_key)


def repair_base(candidates):
    """The candidate the next repair should patch: the best so far, not the latest."""
    ordered = ranked([c for c in candidates if is_usable(c)] or candidates)
    return ordered[0] if ordered else None


def select_deliverable(candidates):
    """The best ELIGIBLE candidate, or None when no candidate qualifies."""
    eligible = [c for c in candidates if is_eligible(c) and is_usable(c)]
    ordered = ranked(eligible)
    return ordered[0] if ordered else None


def select_best_usable(candidates):
    """The best usable candidate whether or not it passed, or None.

    Used when the loop ended without an approved candidate: a document that is
    structurally sound and can be edited is worth returning, flagged, over
    returning nothing. A candidate missing mandatory text is still usable for
    manual editing, and ranks below every one that has it.
    """
    usable = [c for c in candidates if is_usable(c)]
    ordered = ranked(usable)
    return ordered[0] if ordered else None


def selection_reason(chosen, candidates):
    others = [c for c in candidates if c["attempt"] != chosen["attempt"]]
    if not others:
        return (
            f"Attempt {chosen['attempt']} is the only candidate"
            + (" and it is eligible: approved, every mandatory text present, no "
               "blocking issue." if is_eligible(chosen) else ".")
        )
    beaten = ", ".join(
        f"attempt {c['attempt']} ({_why_not(chosen, c)})"
        for c in sorted(others, key=lambda c: c["attempt"])
    )
    return (
        f"Attempt {chosen['attempt']} ranks first ({_describe(chosen)}); "
        f"not chosen: {beaten}."
    )


def _describe(candidate):
    parts = []
    if candidate.get("mandatoryContentOk"):
        parts.append("every mandatory text present")
    else:
        parts.append("mandatory text missing")
    parts.append(f"{candidate.get('blockingCount', 0)} blocking, "
                 f"{candidate.get('warningCount', 0)} warning finding(s)")
    if candidate.get("fidelity") is not None:
        parts.append(f"{candidate['fidelity']:.3f} from the approved concept")
    return ", ".join(parts)


def _why_not(chosen, other):
    """The first criterion on which `other` ranks below `chosen`."""
    if not is_usable(other):
        return "not usable"
    if not other.get("mandatoryContentOk") and chosen.get("mandatoryContentOk"):
        return "mandatory text missing"
    for name, label in (("readabilityBlocking", "blocking readability finding(s)"),
                        ("editabilityBlocking", "blocking editability finding(s)")):
        if int(other.get(name, 0)) > int(chosen.get(name, 0)):
            return f"{other.get(name)} {label}"
    if other.get("progressVerdict") == "D" and chosen.get("progressVerdict") != "D":
        return "a repair verifiably made it worse"
    a, b = chosen.get("fidelity"), other.get("fidelity")
    if a is not None and b is not None and round(b / FIDELITY_STEP) > round(a / FIDELITY_STEP):
        extra = ""
        if int(other.get("blockingCount", 0)) < int(chosen.get("blockingCount", 0)):
            # The two reviews and the pixels disagree; the pixels decided.
            extra = (f", although it has fewer blocking findings "
                     f"({other.get('blockingCount', 0)} vs {chosen.get('blockingCount', 0)})")
        return f"{b - a:.3f} further from the approved concept{extra}"
    if int(other.get("blockingCount", 0)) > int(chosen.get("blockingCount", 0)):
        return f"{other.get('blockingCount', 0)} blocking issue(s)"
    return f"{other.get('warningCount', 0)} warning(s)"


def describe_preserved(candidates):
    """Words for a failure message naming the best candidate that was kept."""
    best = repair_base(candidates)
    if best is None:
        return "no candidate was preserved"
    state = (
        "mandatory text missing" if not best.get("mandatoryContentOk")
        else f"{best.get('blockingCount', 0)} blocking issue(s)"
    )
    return (
        f"the best candidate (attempt {best['attempt']}, {state}) is preserved at "
        f"{best.get('snapshotKey')}"
    )
