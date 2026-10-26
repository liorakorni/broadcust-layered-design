"""Which findings are the same finding.

A model rewords one defect between reviews: MISSING_TEXT_SIDE_DARKENING in the
first round, INSUFFICIENT_TEXT_FIELD_DARKENING in the second, the second naming
`shape-effect-photo-darkening` where the first named `add:effect-photo-darkening`.
Counting those as two defects - one fixed, one new - is how a repair that made
real progress gets read as a regression.

Identity is therefore semantic, from three things the finding itself states:

  * the element or region it is about
  * the requirement it says is violated (a *family*: readability, headline
    prominence, alignment, overlap, clipping, colour, content, ...)
  * the defect type, only where the family does not already say it

and never from the code string or the wording. Matching is one-to-one and
deliberately conservative in one direction only: two findings match when they are
about the same family and about the same element - or when either names no
element, because "the dark panel" and "shape-effect-photo-darkening" are one
place described twice. Findings of an unrecognised family match only on an
identical code, so an unknown defect is never merged into another.
"""

from __future__ import annotations

import re

#: Requirement families, first match wins. Ordered most specific first.
FAMILIES = (
    ("content", re.compile(
        # "MISSING_TEXT_SIDE_DARKENING" is about shading, not about lost copy, so
        # a bare "missing text" only counts when the code ends there.
        r"mandatory|required[_ ]?text|dropped[_ ]?text|text[_ ]?missing|"
        r"missing[_ ]?(copy|content|wording)|missing[_ ]?text$", re.I)),
    ("clipping", re.compile(r"clip|cut[_ ]?off|truncat|overflow|out[_ ]?of[_ ]?bounds", re.I)),
    ("overlap", re.compile(r"overlap|collid|intersect|collision", re.I)),
    ("readability", re.compile(
        r"darken|gradient|shading|scrim|readab|legib|text[_ ]?(side|field)|"
        r"dark[_ ]?(left|panel|column|field)|photo[_ ]?(busy|competes)|"
        r"contrast|background[_ ]?(busy|competes)", re.I)),
    ("headline", re.compile(
        r"headline|title|emphasis|prominen|hierarchy|oversized|footprint|"
        r"letter[_ ]?spacing|tracking|outline|stroke", re.I)),
    ("alignment", re.compile(r"align|anchor|drift|justif|right[_ ]?edge|left[_ ]?edge", re.I)),
    ("color", re.compile(r"colou?r|yellow|fill|hue|palette|circle[_ ]?colou?r", re.I)),
    ("icon", re.compile(r"icon|pictogram|glyph|badge", re.I)),
    ("typography", re.compile(r"font|typograph|weight|size|line[_ ]?height|kerning", re.I)),
)

#: Prefixes the pipeline adds when it turns an effect into a shape or a review asks
#: for something that does not exist yet. They are not part of what is meant.
_PREFIXES = ("add:", "shape-", "effect-", "text-", "t_", "l_")


def family_of(issue):
    """The requirement family a finding is about, or "other".

    The code is read first because it is the most compact statement of the
    requirement; the message and impact are read only when the code is silent.
    """
    for source in (
        str(issue.get("code") or ""),
        " ".join(str(issue.get(k) or "") for k in ("affected", "message", "impact")),
    ):
        for name, pattern in FAMILIES:
            if pattern.search(source):
                return name
    return "other"


def normalize_element(element):
    """An element id with the pipeline's decoration removed, lower-cased."""
    value = str(element or "").strip().lower()
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if value.startswith(prefix) and len(value) > len(prefix):
                value = value[len(prefix):]
                changed = True
    return value


def elements_of(issue):
    """The set of normalised element ids a finding names (empty when none)."""
    names = list(issue.get("affectedElementIds") or [])
    names += [issue.get("layerId"), issue.get("assetId")]
    out = {normalize_element(n) for n in names if n}
    # `affected` is prose for a vision finding ("The two-line headline hierarchy")
    # and an id for a measured one; only an id-shaped value counts as an element.
    affected = str(issue.get("affected") or "")
    if affected and re.fullmatch(r"[A-Za-z0-9_.:-]+", affected):
        out.add(normalize_element(affected))
    out.discard("")
    return out


def identity(issue):
    """`(family, elements)` - the stable identity of one finding."""
    return family_of(issue), frozenset(elements_of(issue))


def same_finding(a, b):
    """Whether two findings are about the same requirement on the same thing."""
    family_a, elements_a = identity(a)
    family_b, elements_b = identity(b)
    if family_a != family_b:
        return False
    if family_a == "other":
        return a.get("code") == b.get("code") and bool(a.get("code"))
    if not elements_a or not elements_b:
        return True
    return bool(elements_a & elements_b)


def match(previous, current):
    """Pair findings across two reviews, one-to-one.

    Returns `{"pairs": [(previous, current)], "resolved": [previous],
    "new": [current]}`. A previous finding with no partner was resolved; a current
    one with no partner is new. Pairing prefers the candidate that shares the most
    elements, so two findings in one family are not crossed.
    """
    remaining = list(current)
    pairs, resolved = [], []
    for old in previous:
        best, best_score = None, -1
        for new in remaining:
            if not same_finding(old, new):
                continue
            score = len(elements_of(old) & elements_of(new))
            if score > best_score:
                best, best_score = new, score
        if best is None:
            resolved.append(old)
        else:
            remaining.remove(best)
            pairs.append((old, best))
    return {"pairs": pairs, "resolved": resolved, "new": remaining}
