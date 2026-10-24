"""Mandatory content: every approved text must be present, exactly, in the design.

This is deterministic and cheap, so it runs after every correction rather than
being left to a vision model to notice. A repair that touches typography or layout
is the kind of change that can quietly drop a line, and a model reading a
screenshot is the wrong instrument to catch a missing character in a phone number.

Two views are checked because they can differ: the scene (what the layout says it
contains) and the HTML (what will actually be delivered). A layer can be in the
scene and absent from the document, and the HTML is what ships.
"""

from __future__ import annotations

from html.parser import HTMLParser

from lib import review_policy


class _TextCollector(HTMLParser):
    """Collects the visible copy of every `.words` block, in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.texts = []
        self._depth = 0
        self._words_depth = None
        self._in_text = False
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        self._depth += 1
        classes = (dict(attrs).get("class") or "").split()
        if "words" in classes:
            self._words_depth = self._depth
        elif "text" in classes and self._words_depth is not None:
            self._in_text = True
            self._buffer = []

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self._in_text:
            self.texts.append("".join(self._buffer))
            self._in_text = False
        if self._words_depth == self._depth:
            self._words_depth = None
        self._depth -= 1

    def handle_data(self, data):
        if self._in_text:
            self._buffer.append(data)


def html_texts(html):
    """The copy of every text block in a delivered document."""
    collector = _TextCollector()
    collector.feed(html)
    return collector.texts


def _issue(code, key, message, element=None):
    return {
        "code": code,
        "category": review_policy.CONTENT,
        "severity": review_policy.BLOCKING,
        "affected": key,
        "impact": message,
        "suggestion": "Restore the approved wording exactly.",
        "region": None,
        "message": message,
        "layerId": element,
        "assetId": None,
        # Wording only ever comes from the approved copy, so the repair is
        # always the same: put the approved text back.
        "repairTarget": "content",
        "affectedElementIds": [key],
        "requiresImageGeneration": False,
        "constraints": [],
    }


def check(scene, copy_items, html=None):
    """Blocking content issues, empty when every approved text is present.

    `scene` is checked always; `html`, when given, is checked as well. Each
    approved item must appear exactly once with its exact wording, and nothing
    may appear that was not approved.
    """
    issues = []
    approved = {item["contentKey"]: item["text"] for item in copy_items}
    layers = {}
    for layer in scene["textLayers"]:
        layers.setdefault(layer["contentKey"], []).append(layer)

    for key, text in approved.items():
        found = layers.get(key)
        if not found:
            issues.append(
                _issue(
                    "REQUIRED_COPY_MISSING", key,
                    f"The approved text for {key} is missing from the design.",
                )
            )
        elif found[0]["text"] != text:
            issues.append(
                _issue(
                    "APPROVED_COPY_ALTERED", key,
                    f"The text for {key} does not match the approved wording.",
                    element=found[0]["id"],
                )
            )
    for key in layers:
        if key not in approved:
            issues.append(
                _issue(
                    "UNAPPROVED_TEXT", key,
                    f"The design contains text for {key}, which was never approved.",
                    element=layers[key][0]["id"],
                )
            )

    if html is not None:
        delivered = html_texts(html)
        for key, text in approved.items():
            if delivered.count(text) == 0 and not any(i["affected"] == key for i in issues):
                issues.append(
                    _issue(
                        "REQUIRED_COPY_MISSING", key,
                        f"The approved text for {key} is not in the delivered HTML.",
                    )
                )
    return issues


def missing_keys(issues):
    return sorted({i["affected"] for i in issues if i["code"] == "REQUIRED_COPY_MISSING"})
