"""Direction is resolved before the concept is generated, never patched after.

A Hebrew design is not an English design with the words swapped: the text column
sits on the right and the eye enters from the right. Deciding that after the
concept exists leaves only two options, both wrong - accept a left-to-right
composition with Hebrew in it, or mirror a finished picture and flip the
photograph, the logo and every pictogram with it.
"""

import copy
import json

import pytest

from lib import direction_policy, reference_policy, review_policy
from lib.schemas import canonical_request_hash, parse_start_request

HEBREW = [
    {"contentKey": "title.primary", "text": "אימון אישי"},
    {"contentKey": "business.name", "text": "FIT HOME"},
    {"contentKey": "contact.phone", "text": "050-1234567"},
    {"contentKey": "contact.email", "text": "info@fithome.example"},
]
LATIN = [
    {"contentKey": "title.primary", "text": "Personal training at home"},
    {"contentKey": "business.name", "text": "FIT HOME"},
]


def request(**overrides):
    base = {
        "brief": {"post": "A marketing post for in-home personal training.",
                  "instructions": None},
        "layoutDirection": "auto",
        "textDirection": "auto",
        "referenceLayoutPolicy": "adapt-to-locale",
    }
    base.update(overrides)
    return base


# --- precedence ----------------------------------------------------------------


def test_hebrew_copy_defaults_to_an_rtl_composition():
    resolved = direction_policy.resolve(request(), HEBREW)
    assert resolved["layoutDirection"] == "rtl"
    assert resolved["textDirection"] == "rtl"
    assert resolved["source"] == "locale-default"
    assert "text column sits on the right" in resolved["reason"]
    # The instruction the prompts carry says where the columns go.
    block = direction_policy.prompt_block(resolved)
    assert "text column sits on the RIGHT" in block
    assert "photography sits on the LEFT" in block


def test_latin_copy_stays_left_to_right():
    resolved = direction_policy.resolve(request(), LATIN)
    assert (resolved["layoutDirection"], resolved["textDirection"]) == ("ltr", "ltr")
    assert "text column sits on the LEFT" in direction_policy.prompt_block(resolved)


def test_an_explicit_direction_beats_the_locale_default():
    resolved = direction_policy.resolve(request(layoutDirection="ltr"), HEBREW)
    assert resolved["layoutDirection"] == "ltr"
    assert resolved["source"] == "request"
    # Text still runs the way the script does: the two questions are separate.
    assert resolved["textDirection"] == "rtl"


def test_an_explicit_text_direction_is_kept_independently():
    resolved = direction_policy.resolve(request(textDirection="ltr"), HEBREW)
    assert (resolved["layoutDirection"], resolved["textDirection"]) == ("rtl", "ltr")


def test_preserve_keeps_the_reference_direction_for_hebrew():
    resolved = direction_policy.resolve(
        request(referenceLayoutPolicy="preserve"), HEBREW)
    assert resolved["layoutDirection"] == "preserve-reference"
    assert resolved["source"] == "reference-policy"
    block = direction_policy.prompt_block(resolved)
    assert "Keep the reference's own composition direction" in block
    assert "Do not mirror it to suit the language" in block


@pytest.mark.parametrize("instruction", [
    "Keep the text on the left and the photography on the right.",
    "טקסט בצד שמאל ומוקד צילומי בצד ימין, ללא היפוך כל הקומפוזיציה.",
    "Preserve the composition of the reference exactly.",
])
def test_explicit_placement_in_the_brief_beats_the_locale_default(instruction):
    """The customer asked for that composition; the locale does not overrule it."""
    resolved = direction_policy.resolve(
        request(brief={"post": "A post.", "instructions": instruction}), HEBREW)
    assert resolved["layoutDirection"] == "preserve-reference"
    assert resolved["source"] == "brief"


@pytest.mark.parametrize("brief_text", [
    "Bright daylight, copyright 2026, a lightweight feel.",
    "A premium post with generous spacing.",
])
def test_a_word_that_merely_contains_a_direction_is_not_an_instruction(brief_text):
    resolved = direction_policy.resolve(
        request(brief={"post": brief_text, "instructions": None}), HEBREW)
    assert resolved["layoutDirection"] == "rtl"
    assert resolved["source"] == "locale-default"


def test_mixed_hebrew_and_latin_keeps_latin_order():
    """The business name, the phone number and the email are never reversed."""
    resolved = direction_policy.resolve(request(), HEBREW)
    block = direction_policy.prompt_block(resolved)
    assert "Business names, Latin words, phone" in block
    assert "never reversed" in block and "character by character" in block
    assert "A line that mixes scripts keeps each run in its own direction" in block
    # And nothing in the instruction mirrors imagery.
    assert "Never mirror a photograph" in block
    assert "Mirror COLUMN POSITIONS only" in block


def test_arabic_is_right_to_left_too():
    assert direction_policy.has_rtl([{"text": "تدريب شخصي"}])
    assert not direction_policy.has_rtl([{"text": "FIT HOME"}, {"text": "050-1234567"}])


# --- carried in the versioned policy -------------------------------------------


def test_the_policy_document_carries_the_resolved_direction():
    resolved = direction_policy.resolve(request(), HEBREW)
    policy = reference_policy.derive_reference_policy(0, "balanced", resolved)
    assert policy["direction"] == resolved
    # Bumped, so a concept composed under the old policy is never reused here.
    assert policy["policyVersion"] == "3.0"


def test_a_policy_without_a_direction_still_derives():
    policy = reference_policy.derive_reference_policy(0)
    assert policy["direction"] is None


# --- the concept review judges it ----------------------------------------------


def verdict(direction_ok=True, policy=None):
    """A complete, passing verdict: one structural check per preserved item."""
    preserved = (policy or reference_policy.derive_reference_policy(0))["mustPreserve"]
    return {
        "mode": "strict-adherence",
        "checks": [{"anchorId": "a", "feature": feature, "expectedPolicy": "p",
                    "observed": "as the reference", "passed": True}
                   for feature in preserved],
        "copyChecks": [{"contentKey": item["contentKey"], "present": True,
                        "correct": True, "readable": True, "observed": item["text"]}
                       for item in HEBREW],
        "documentedExceptions": [],
        "issues": [],
        "suggestions": [],
        "constraintConflict": {"conflicted": False, "reason": None},
        "compositionDirectionOk": direction_ok,
    }


def evaluate(direction_ok=True, direction=None):
    policy = reference_policy.derive_reference_policy(
        0, "balanced", direction or direction_policy.resolve(request(), HEBREW))
    return review_policy.evaluate_concept(
        verdict(direction_ok, policy), policy, HEBREW)


def test_a_wrong_direction_concept_is_a_concept_defect():
    out = evaluate(direction_ok=False)

    assert out["eligible"] is False
    check = next(c for c in out["requiredChecks"]
                 if c["name"] == review_policy.CHECK_COMPOSITION_DIRECTION)
    assert check["status"] == review_policy.FAILED
    # And it says what to change, so the bounded concept correction can act.
    issue = next(i for i in out["blockingIssues"]
                 if i["code"] == "COMPOSITION_DIRECTION_WRONG")
    assert "resolved rtl" in issue["impact"]
    assert issue["severity"] == "blocking"


def test_a_correctly_composed_concept_passes_the_direction_check():
    out = evaluate(direction_ok=True)
    check = next(c for c in out["requiredChecks"]
                 if c["name"] == review_policy.CHECK_COMPOSITION_DIRECTION)
    assert check["status"] == review_policy.PASSED
    assert out["eligible"] is True


def test_an_unanswered_direction_question_is_a_check_that_did_not_run():
    """A missing answer is never a pass."""
    policy = reference_policy.derive_reference_policy(
        0, "balanced", direction_policy.resolve(request(), HEBREW))
    raw = verdict(policy=policy)
    raw.pop("compositionDirectionOk")
    out = review_policy.evaluate_concept(raw, policy, HEBREW)
    assert out["eligible"] is False
    assert review_policy.CHECK_COMPOSITION_DIRECTION in out["incompleteChecks"]


def test_a_job_whose_policy_predates_the_check_is_not_failed_by_it():
    policy = reference_policy.derive_reference_policy(0)
    raw = verdict(policy=policy)
    raw.pop("compositionDirectionOk")
    out = review_policy.evaluate_concept(raw, policy, HEBREW)
    check = next(c for c in out["requiredChecks"]
                 if c["name"] == review_policy.CHECK_COMPOSITION_DIRECTION)
    assert check["status"] == review_policy.NOT_APPLICABLE
    assert out["eligible"] is True


# --- idempotency ---------------------------------------------------------------


def test_the_request_hash_is_unchanged_when_the_new_fields_are_default(start_payload):
    import hashlib

    omitted = copy.deepcopy(start_payload)
    for field in ("layoutDirection", "textDirection", "referenceLayoutPolicy"):
        omitted.pop(field, None)
    explicit = {**copy.deepcopy(omitted), "layoutDirection": "auto",
                "textDirection": "auto", "referenceLayoutPolicy": "adapt-to-locale"}

    hashed = canonical_request_hash(parse_start_request(omitted))
    assert hashed == canonical_request_hash(parse_start_request(explicit))

    body = parse_start_request(omitted).model_dump(mode="json")
    for field in ("reviewStrictness", "designFlow", "layoutDirection",
                  "textDirection", "referenceLayoutPolicy"):
        body.pop(field)
    assert hashed == hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def test_a_non_default_direction_is_a_different_request(start_payload):
    plain = parse_start_request(copy.deepcopy(start_payload))
    rtl = parse_start_request({**copy.deepcopy(start_payload), "layoutDirection": "rtl"})
    assert canonical_request_hash(plain) != canonical_request_hash(rtl)


@pytest.mark.parametrize("field,value", [
    ("layoutDirection", "sideways"),
    ("textDirection", "mirror"),
    ("referenceLayoutPolicy", "whatever"),
])
def test_an_unknown_direction_value_is_refused(start_payload, field, value):
    from lib.errors import InvalidRequest

    with pytest.raises(InvalidRequest):
        parse_start_request({**copy.deepcopy(start_payload), field: value})


# --- resolved once, at acceptance ------------------------------------------------

#: The brief of the live job that failed review (cbf8474e...). It asks for the text
#: "בצד שמאל" - left - so the composition follows the brief, not the locale. That
#: was the right reading of that brief; the fixture below it is the corrected one.
FAILING_BRIEF = {
    "post": "פוסט שיווקי לאימוני כושר אישיים.",
    "instructions": ("צור פוסט בעברית בהיצמדות מרבית למבנה ולסגנון של הרפרנס. "
                     "טקסט בצד שמאל ומוקד צילומי בצד ימין. "
                     "הטקסט בעברית יהיה בכיוון RTL, ללא היפוך כל הקומפוזיציה."),
    "requiredTexts": ["אימון אישי", "אצלכם בבית"],
}

#: The same request with the placement instruction removed.
CORRECTED_BRIEF = {
    "post": "פוסט שיווקי לאימוני כושר אישיים.",
    "instructions": ("צור פוסט בעברית בהיצמדות מרבית למבנה ולסגנון של הרפרנס. "
                     "הטקסט בעברית יהיה בכיוון RTL."),
    "requiredTexts": ["אימון אישי", "אצלכם בבית"],
}


def test_the_acceptance_resolution_follows_the_brief_when_it_places_things():
    resolved = direction_policy.resolve_for_request(request(brief=FAILING_BRIEF))

    assert resolved["layoutDirection"] == "preserve-reference"
    assert resolved["source"] == "brief"
    assert resolved["resolvedAt"] == "acceptance"
    sides = {c["side"] for c in resolved["placementConstraints"]}
    assert {"left", "right"} <= sides
    assert {c["kind"] for c in resolved["placementConstraints"]} >= {"placement", "mirror"}
    assert "reason" in resolved and resolved["reason"]


def test_the_corrected_hebrew_fixture_composes_right_to_left_from_the_start():
    resolved = direction_policy.resolve_for_request(request(brief=CORRECTED_BRIEF))

    assert resolved["layoutDirection"] == "rtl"
    assert resolved["textDirection"] == "rtl"
    assert resolved["referenceLayoutPolicy"] == "adapt-to-locale"
    assert resolved["source"] == "locale-default"
    assert resolved["placementConstraints"] == []
    # The concept prompt carries it, so the concept begins right to left.
    assert "text column sits on the RIGHT" in direction_policy.prompt_block(resolved)


def test_an_explicit_request_field_still_beats_the_brief_at_acceptance():
    resolved = direction_policy.resolve_for_request(
        request(brief=FAILING_BRIEF, layoutDirection="rtl"))
    assert resolved["layoutDirection"] == "rtl"
    assert resolved["source"] == "request"


def test_a_latin_request_resolves_left_to_right_without_any_copy():
    resolved = direction_policy.resolve_for_request(request(
        brief={"post": "A winter sale.", "instructions": None,
               "requiredTexts": ["Winter sale", "Book now"]}))
    assert resolved["layoutDirection"] == "ltr"


def test_the_acceptance_resolution_is_persisted_and_the_stages_read_it(monkeypatch):
    """Acceptance writes it onto the job; ANALYZE_REFERENCE uses it instead of
    deciding again from the approved copy."""
    import inspect

    from lib import jobs
    from lib.stages import analyze_reference

    assert "layout_policy" in inspect.signature(jobs.create_job).parameters
    source = inspect.getsource(analyze_reference.run)
    assert 'job.get("layoutPolicy")' in source
    # A job accepted before the field existed still resolves the old way.
    assert "direction_policy.resolve(request, copy_items)" in source
