"""PREPARE_COPY: build the authoritative copy manifest.

This stage is the single source of truth for every word that reaches the
deliverable (SPEC section 4). Text that the concept image happens to contain, or
that OCR might recover from it, is never authoritative - EXTRACT_SCENE may only
reference contentKeys produced here.

Two guarantees are enforced in code rather than left to the model:

* every string in requiredTexts appears verbatim, and
* no item contains a fact absent from the brief or business details.

The second is checked for the categories that cause real damage - prices,
percentages, phone numbers, dates and URLs - because a model that invents a
"20% off" is producing a design the business is legally obliged to honour.
"""

from __future__ import annotations

import re

import conf
from lib import artifacts, jobs, prompts
from lib.errors import CopyInvalid, Stage
from lib.providers import vision

COPY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["contentKey", "role", "text"],
                "properties": {
                    "contentKey": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": [
                            "title",
                            "subtitle",
                            "body",
                            "service",
                            "cta",
                            "contact",
                            "business",
                            "legal",
                        ],
                    },
                    "text": {"type": "string"},
                },
            },
        }
    },
}

#: Underscores are allowed inside a segment because multi-word keys are the
#: natural result of asking for a descriptive name - service.personal_support
#: rather than service.2 - and the key is an internal identifier tying copy to a
#: scene layer, so the separator carries no meaning of its own. Strict
#: structured output has no pattern support, so this cannot be enforced at
#: generation time; the prompt states the rule instead.
CONTENT_KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")

_NUMBER = r"\d+(?:[.,]\d+)?"

#: Claims whose NUMBER must appear in the brief. Each pattern captures the whole
#: number so "$499" is checked as 499, not as 4. Digits in general are too noisy
#: to check this way - a phone number's digits appear in many forms - so these
#: target only the shapes that constitute an offer.
_NUMERIC_CLAIMS = (
    (re.compile(rf"({_NUMBER})\s*%"), "a percentage"),
    (re.compile(rf"[$€£₪]\s*({_NUMBER})"), "a price"),
    (re.compile(rf"({_NUMBER})\s*(?:[$€£₪]|דולר|שקלים|שקל|ש\"ח|אירו)"), "a price"),
)

#: Claims that must appear in the brief verbatim; there is no number to ground.
_LITERAL_CLAIMS = ((re.compile(r"https?://\S+|www\.\S+"), "a URL"),)


def run(job_id, attempt, asset_id=None):
    job = jobs.get_job_unscoped(job_id)
    jobs.set_stage(job_id, Stage.PREPARE_COPY)
    request = artifacts.get_json(job_id, job["requestKey"])

    brief = request["brief"]
    business = brief.get("business") or {}
    required = brief.get("requiredTexts") or []

    prompt = prompts.render(
        "prepare-copy",
        post=brief["post"],
        business_block=prompts.field_block(
            [
                ("Name", business.get("name")),
                ("Phone", business.get("phone")),
                ("Email", business.get("email")),
                ("Website", business.get("website")),
            ]
        ),
        required_block=prompts.bullet_list(required),
        instructions_block=prompts.instructions_block(brief.get("instructions")),
    )

    result = vision.analyze(
        prompt,
        (),
        schema=COPY_SCHEMA,
        schema_name="copy_manifest_v1",
        model=conf.COPY_MODEL,
        operation_id=f"{job_id}:prepare_copy:{attempt}",
    )

    items = validate_manifest(result.get("items", []), brief, business)

    key = artifacts.artifact_key(job_id, Stage.PREPARE_COPY, attempt, "copy.json")
    stored = artifacts.put_json(key, {"items": items})
    jobs.record_artifacts(job_id, Stage.PREPARE_COPY, attempt, {"copy": stored["key"]})
    return {"copy": stored["key"], "itemCount": len(items)}


def validate_manifest(items, brief, business):
    """Enforce the copy guarantees. Raises CopyInvalid on any violation."""
    if not items:
        raise CopyInvalid("The copy manifest is empty.")
    if len(items) > 40:
        raise CopyInvalid(f"The copy manifest has {len(items)} items; the limit is 40.")

    seen = set()
    cleaned = []
    for item in items:
        key = item["contentKey"]
        if not CONTENT_KEY.match(key):
            raise CopyInvalid(f"Invalid contentKey {key!r}; expected dot.notation.")
        if key in seen:
            raise CopyInvalid(f"Duplicate contentKey {key!r} in the copy manifest.")
        seen.add(key)

        text = item["text"].strip()
        if not text:
            raise CopyInvalid(f"contentKey {key!r} has empty text.")
        if len(text) > 400:
            raise CopyInvalid(f"contentKey {key!r} is {len(text)} characters; max 400.")
        cleaned.append({"contentKey": key, "role": item["role"], "text": text})

    _check_required_texts(cleaned, brief.get("requiredTexts") or [])
    _check_contacts_are_separate(cleaned, business)
    _check_no_invented_facts(cleaned, brief, business)
    return cleaned


def _check_required_texts(items, required):
    """requiredTexts must survive verbatim (SPEC section 4)."""
    present = {i["text"] for i in items}
    missing = [r for r in required if r.strip() and r.strip() not in present]
    if missing:
        raise CopyInvalid(
            "requiredTexts were altered or dropped from the copy manifest: "
            + ", ".join(repr(m) for m in missing)
        )


def _check_contacts_are_separate(items, business):
    """Phone and email stay distinct items (SPEC section 4).

    Merging them into one line makes the contact row impossible to lay out as
    separate LTR boxes inside an RTL design.
    """
    phone = (business.get("phone") or "").strip()
    email = (business.get("email") or "").strip()
    if not phone or not email:
        return
    for item in items:
        if phone in item["text"] and email in item["text"]:
            raise CopyInvalid(
                f"contentKey {item['contentKey']!r} merges phone and email; "
                "they must be separate items."
            )


def _check_no_invented_facts(items, brief, business):
    """Reject offer-shaped claims that do not appear in the source material."""
    source = " ".join(
        [
            brief.get("post", ""),
            brief.get("instructions") or "",
            " ".join(brief.get("requiredTexts") or []),
            " ".join(str(v) for v in business.values() if v),
        ]
    )
    # Grounding is on the value, not the formatting: the model legitimately
    # rewrites "499 דולר" as "$499", and rejecting that would fire on correct
    # output.
    source_numbers = {_normalize(n) for n in re.findall(_NUMBER, source)}

    for item in items:
        for pattern, label in _NUMERIC_CLAIMS:
            for number in pattern.findall(item["text"]):
                if _normalize(number) not in source_numbers:
                    raise _invented(item, label)
        for pattern, label in _LITERAL_CLAIMS:
            for found in pattern.findall(item["text"]):
                if found not in source:
                    raise _invented(item, label)


def _normalize(number):
    return number.replace(",", "").replace(".", "").lstrip("0") or "0"


def _invented(item, label):
    return CopyInvalid(
        f"contentKey {item['contentKey']!r} introduces {label} "
        f"that is not in the brief: {item['text']!r}"
    )
