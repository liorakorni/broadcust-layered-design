"""Which way the design runs, decided before the concept is generated.

A Hebrew design is not an English design with the words swapped: the text column
sits on the right, the photograph on the left, and the eye enters from the right.
Deciding that after the concept exists means either accepting a left-to-right
composition with Hebrew text in it, or mirroring a finished picture - which
flips the photograph, the logo and every pictogram with it.

So direction is resolved here, from the request and the approved copy, and
carried into the concept prompt, the concept review and the extracted scene.
Three separate questions, because they have different answers:

* `layoutDirection` - where the columns sit.
* `textDirection` - how the writing runs inside them. Hebrew copy with a Latin
  phone number has both, per text layer, which is why the scene keeps a direction
  on every layer rather than one global value.
* `referenceLayoutPolicy` - whether the reference's own composition direction is
  kept, or adapted to the locale of the copy. Adapting is the default: a training
  reference in English may legitimately guide a Hebrew winter-holiday design, and
  the Hebrew version of that composition is the mirrored one.

Nothing here mirrors imagery. The instruction the prompts carry is explicit about
that: column positions move, pictures do not.
"""

from __future__ import annotations

import re

#: Hebrew, Arabic and the Hebrew/Arabic presentation forms.
RTL_SCRIPT = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")

#: Phrases in a brief that place things explicitly, or ask for the reference's
#: composition to be kept. An explicit instruction wins over the locale default:
#: the customer asked for that composition. Word boundaries matter - "bright"
#: and "copyright" are not placement instructions.
_PLACEMENT = re.compile(
    r"\b(left|right|mirror(?:ed)?|flip(?:ped)?)\b"
    r"|\b(?:same|keep|preserve)\s+(?:the\s+)?(?:layout|composition)\b"
    r"|בצד ימין|בצד שמאל|צד ימין|צד שמאל"
    r"|ללא היפוך|בלי להפוך|שמור על הקומפוזיציה|באותו מבנה",
    re.IGNORECASE,
)

VALUES = ("auto", "rtl", "ltr")
POLICIES = ("adapt-to-locale", "preserve")


def resolve_for_request(request):
    """Resolve the policy once, from the request alone, at acceptance.

    Every stage after acceptance - concept generation, review, decomposition,
    assembly and repair - has to work to the same answer, and the approved copy
    does not exist yet at that point. The locale is therefore read from what the
    request itself carries: the required texts when it supplies them, otherwise
    the brief's own prose. The result is persisted on the job (`layoutPolicy`) and
    never recomputed, so a later stage cannot reach a different conclusion from a
    different input, and a job already accepted is never reinterpreted.

    Adds to `resolve`'s result the explicit `placementConstraints` found in the
    brief and `resolvedAt`, which marks the document as the acceptance-time one.
    """
    brief = request.get("brief") or {}
    required = [t for t in (brief.get("requiredTexts") or []) if isinstance(t, str)]
    sources = required or [
        brief[key] for key in ("post", "instructions") if isinstance(brief.get(key), str)
    ]
    resolved = _resolve(request, "rtl" if any(RTL_SCRIPT.search(t) for t in sources) else "ltr")
    resolved["placementConstraints"] = placement_constraints(request)
    resolved["resolvedAt"] = "acceptance"
    return resolved


#: What a placement phrase asks for, by the word that matched. Anything not listed
#: still counts as an explicit placement instruction; it just names no side.
_SIDES = (
    (re.compile(r"left|שמאל", re.IGNORECASE), "left"),
    (re.compile(r"right|ימין", re.IGNORECASE), "right"),
)


def placement_constraints(request):
    """Every explicit placement instruction in the brief, as structured records.

    `[{"phrase", "side", "kind"}]`, empty when the brief places nothing. Kept as
    data rather than only as a reason string, so a later stage can honour the
    instruction without parsing prose.
    """
    brief = request.get("brief") or {}
    text = " ".join(str(v) for v in brief.values() if isinstance(v, str))
    found = []
    for match in _PLACEMENT.finditer(text):
        phrase = match.group(0)
        side = next((name for pattern, name in _SIDES if pattern.search(phrase)), None)
        kind = "placement" if side else (
            "mirror" if re.search(r"mirror|flip|היפוך|להפוך", phrase, re.IGNORECASE)
            else "preserve")
        record = {"phrase": phrase, "side": side, "kind": kind}
        if record not in found:
            found.append(record)
    return found


def resolve(request, copy_items):
    """Return the resolved direction policy for a request.

    `{layoutDirection, textDirection, referenceLayoutPolicy, locale, reason,
    source}`. `source` is why: "request", "brief", "reference-policy" or
    "locale-default", in precedence order.

    Kept for jobs accepted before the policy was persisted at acceptance, and for
    callers that already hold the approved copy.
    """
    return _resolve(request, "rtl" if has_rtl(copy_items) else "ltr")


def _resolve(request, locale):
    policy = request.get("referenceLayoutPolicy") or "adapt-to-locale"
    requested_layout = request.get("layoutDirection") or "auto"
    requested_text = request.get("textDirection") or "auto"

    text = requested_text if requested_text != "auto" else locale

    if requested_layout != "auto":
        return _resolved(
            requested_layout, text, policy, locale,
            source="request",
            reason=f"The request asked for a {requested_layout} layout.",
        )

    instruction = _placement_instruction(request)
    if instruction:
        return _resolved(
            "preserve-reference", text, policy, locale,
            source="brief",
            reason=(
                "The brief places elements explicitly "
                f"({instruction!r}), so the composition follows the brief and the "
                "reference rather than the locale."
            ),
        )

    if policy == "preserve":
        return _resolved(
            "preserve-reference", text, policy, locale,
            source="reference-policy",
            reason=(
                "referenceLayoutPolicy is 'preserve', so the reference's own "
                "composition direction is kept whatever the copy's script."
            ),
        )

    return _resolved(
        locale, text, policy, locale,
        source="locale-default",
        reason=(
            f"The approved copy is {'Hebrew or Arabic' if locale == 'rtl' else 'Latin'}"
            f", so the composition is laid out {locale}: "
            + (
                "for a split layout the text column sits on the right and the "
                "photography on the left."
                if locale == "rtl"
                else "the text column sits on the left."
            )
        ),
    )


def has_rtl(copy_items):
    """Whether the approved copy contains right-to-left script."""
    return any(RTL_SCRIPT.search(item.get("text") or "") for item in copy_items or [])


def _resolved(layout, text, policy, locale, *, source, reason):
    return {
        "layoutDirection": layout,
        "textDirection": text,
        "referenceLayoutPolicy": policy,
        "locale": locale,
        "source": source,
        "reason": reason,
    }


def _placement_instruction(request):
    """The placement phrase found in the brief, or None."""
    brief = request.get("brief") or {}
    text = " ".join(
        str(value) for value in brief.values() if isinstance(value, str)
    )
    found = _PLACEMENT.search(text)
    return found.group(0) if found else None


def prompt_block(resolved):
    """The direction instruction every concept-side prompt carries.

    One source of wording, so the generator, the correction and the review are
    judging the same rule.
    """
    layout = resolved["layoutDirection"]
    if layout == "preserve-reference":
        composition = (
            "Keep the reference's own composition direction exactly as it is. Do "
            "not mirror it to suit the language."
        )
    elif layout == "rtl":
        composition = (
            "Compose the design right to left from the start: the eye enters at "
            "the top right, the text column sits on the RIGHT, and in a split "
            "layout the photography sits on the LEFT. Do not build a left-to-right "
            "composition and translate it."
        )
    else:
        composition = (
            "Compose the design left to right: the text column sits on the LEFT."
        )

    return f"""DIRECTION ({resolved['source']}: {resolved['reason']})
- {composition}
- Mirror COLUMN POSITIONS only. Never mirror a photograph, a person, a logo, an
  icon or a pictogram: a mirrored phone handset or a reversed logo is a defect.
- Hebrew and Arabic text runs right to left. Business names, Latin words, phone
  numbers, email addresses and URLs keep their own order and are never reversed
  character by character, even inside a right-to-left line.
- A line that mixes scripts keeps each run in its own direction."""
