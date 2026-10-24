"""Design candidates: every assembled-and-reviewed attempt, ranked.

A repair loop produces several candidates, and the most recent is not necessarily
the best: a correction can fix one defect and introduce another, and a later
attempt can lose text an earlier one had. Each attempt is therefore kept as a
candidate (its immutable snapshot, render and review), and two decisions are made
from the full set rather than from "whatever came last":

  * which candidate the NEXT repair starts from  (`repair_base`)
  * which candidate is delivered                 (`select_deliverable`)

Ranking puts mandatory content first, then blocking issues, then warnings, then the
earlier attempt - so a candidate that dropped required text can never outrank one
that kept it, however few other problems it has.
"""

from __future__ import annotations


def is_eligible(candidate):
    """A candidate may be delivered only if it is approved and complete.

    Approved means the review found no blocking issue and every required render
    check passed. `mandatoryContentOk` is checked on its own as well, so a
    candidate with missing mandatory content is never delivered even if some other
    path marked it approved.
    """
    return bool(candidate.get("approved")) and bool(candidate.get("mandatoryContentOk"))


def rank_key(candidate):
    return (
        0 if candidate.get("mandatoryContentOk") else 1,
        int(candidate.get("blockingCount", 0)),
        int(candidate.get("warningCount", 0)),
        int(candidate["attempt"]),
    )


def ranked(candidates):
    return sorted(candidates, key=rank_key)


def repair_base(candidates):
    """The candidate the next repair should patch: the best so far, not the latest."""
    ordered = ranked(candidates)
    return ordered[0] if ordered else None


def select_deliverable(candidates):
    """The best ELIGIBLE candidate, or None when no candidate qualifies."""
    eligible = [c for c in candidates if is_eligible(c)]
    ordered = ranked(eligible)
    return ordered[0] if ordered else None


def selection_reason(chosen, candidates):
    others = [c for c in candidates if c["attempt"] != chosen["attempt"]]
    if not others:
        return (
            f"Attempt {chosen['attempt']} is the only candidate and it is eligible: "
            "approved, every mandatory text present, no blocking issue."
        )
    beaten = ", ".join(
        f"attempt {c['attempt']} ({_why_not(c)})" for c in sorted(others, key=lambda c: c["attempt"])
    )
    return (
        f"Attempt {chosen['attempt']} is the best eligible candidate "
        f"({chosen.get('warningCount', 0)} warning(s)); not chosen: {beaten}."
    )


def _why_not(candidate):
    if not candidate.get("mandatoryContentOk"):
        return "mandatory text missing"
    if not candidate.get("approved"):
        return f"{candidate.get('blockingCount', 0)} blocking issue(s)"
    return f"{candidate.get('warningCount', 0)} warning(s)"


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
