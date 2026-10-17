"""Copy manifest grounding rules.

These are the guarantees that keep the service from producing a design the
business is legally obliged to honour.
"""

import pytest

from lib.errors import CopyInvalid
from lib.stages.prepare_copy import validate_manifest

BRIEF = {
    "post": "חופשת חורף באיטליה. טיסות, מלון ומדריך צמוד. יציאה בינואר.",
    "instructions": None,
    "requiredTexts": ["חופשת חורף באיטליה"],
    "business": {
        "name": "Dolce Travel",
        "phone": "03-1234567",
        "email": "info@dolce.example",
    },
}
BUSINESS = BRIEF["business"]


def manifest(*items):
    return [
        {"contentKey": k, "role": r, "text": t} for k, r, t in items
    ]


def ok(items):
    return validate_manifest(items, BRIEF, BUSINESS)


def test_valid_manifest_passes():
    items = ok(
        manifest(
            ("title.primary", "title", "חופשת חורף באיטליה"),
            ("subtitle.primary", "subtitle", "טיסות, מלון ומדריך צמוד"),
            ("contact.phone", "contact", "03-1234567"),
            ("contact.email", "contact", "info@dolce.example"),
        )
    )
    assert len(items) == 4
    assert items[0]["text"] == "חופשת חורף באיטליה"


def test_empty_manifest_rejected():
    with pytest.raises(CopyInvalid, match="empty"):
        ok([])


def test_duplicate_content_key_rejected():
    with pytest.raises(CopyInvalid, match="Duplicate"):
        ok(
            manifest(
                ("title.primary", "title", "חופשת חורף באיטליה"),
                ("title.primary", "title", "משהו אחר"),
            )
        )


def test_malformed_content_key_rejected():
    with pytest.raises(CopyInvalid, match="dot.notation"):
        ok(manifest(("Title Primary", "title", "חופשת חורף באיטליה")))


def test_blank_text_rejected():
    with pytest.raises(CopyInvalid, match="empty text"):
        ok(manifest(("title.primary", "title", "   ")))


def test_required_text_must_survive_verbatim():
    """A reworded requiredText is the failure mode that matters most."""
    with pytest.raises(CopyInvalid, match="requiredTexts"):
        ok(manifest(("title.primary", "title", "חופשה באיטליה בחורף")))


def test_required_text_is_matched_after_stripping():
    ok(manifest(("title.primary", "title", "  חופשת חורף באיטליה  ")))


def test_phone_and_email_may_not_be_merged():
    with pytest.raises(CopyInvalid, match="separate items"):
        ok(
            manifest(
                ("title.primary", "title", "חופשת חורף באיטליה"),
                ("contact.all", "contact", "03-1234567 | info@dolce.example"),
            )
        )


def test_invented_discount_rejected():
    with pytest.raises(CopyInvalid, match="percentage"):
        ok(
            manifest(
                ("title.primary", "title", "חופשת חורף באיטליה"),
                ("cta.primary", "cta", "הנחה של 20%"),
            )
        )


def test_invented_price_rejected():
    with pytest.raises(CopyInvalid, match="price"):
        ok(
            manifest(
                ("title.primary", "title", "חופשת חורף באיטליה"),
                ("cta.primary", "cta", "רק $499"),
            )
        )


def test_invented_url_rejected():
    with pytest.raises(CopyInvalid, match="URL"):
        ok(
            manifest(
                ("title.primary", "title", "חופשת חורף באיטליה"),
                ("contact.website", "contact", "www.dolce-travel.example"),
            )
        )


def test_price_present_in_the_brief_is_allowed():
    brief = dict(BRIEF, post=BRIEF["post"] + " החל מ-$499 לאדם.")
    items = validate_manifest(
        manifest(
            ("title.primary", "title", "חופשת חורף באיטליה"),
            ("body.price", "body", "החל מ-$499"),
        ),
        brief,
        BUSINESS,
    )
    assert len(items) == 2


def test_price_reformatted_from_the_brief_is_allowed():
    """Grounding is on the number, not the exact formatting.

    The model legitimately reformats "499 דולר" as "$499"; rejecting that would
    make the check fire on correct output.
    """
    brief = dict(BRIEF, post=BRIEF["post"] + " המחיר 499 דולר.")
    items = validate_manifest(
        manifest(
            ("title.primary", "title", "חופשת חורף באיטליה"),
            ("body.price", "body", "$499"),
        ),
        brief,
        BUSINESS,
    )
    assert len(items) == 2


def test_website_from_business_details_is_allowed():
    business = dict(BUSINESS, website="https://dolce.example")
    brief = dict(BRIEF, business=business)
    items = validate_manifest(
        manifest(
            ("title.primary", "title", "חופשת חורף באיטליה"),
            ("contact.website", "contact", "https://dolce.example"),
        ),
        brief,
        business,
    )
    assert len(items) == 2


def test_oversized_item_rejected():
    with pytest.raises(CopyInvalid, match="max 400"):
        ok(
            manifest(
                ("title.primary", "title", "חופשת חורף באיטליה"),
                ("body.long", "body", "א" * 401),
            )
        )


def test_too_many_items_rejected():
    items = manifest(("title.primary", "title", "חופשת חורף באיטליה"))
    items += [
        {"contentKey": f"service.{i}", "role": "service", "text": f"שירות {i}"}
        for i in range(45)
    ]
    with pytest.raises(CopyInvalid, match="limit is 40"):
        ok(items)
