"""Prompt templates loaded from prompts/*.txt.

Prompts live in files rather than string literals so they can be reviewed and
tuned without touching pipeline logic.

Substitution is `{name}` only, applied with str.replace rather than str.format:
prompt text contains JSON braces and CSS, and format() would choke on them or,
worse, interpret a brace in customer copy.
"""

from __future__ import annotations

import pathlib

from lib.errors import ConfigurationInvalid

PROMPT_DIR = pathlib.Path(__file__).parent.parent / "prompts"

_cache = {}


def load(name):
    """Read a prompt template, cached for the life of the container."""
    if name not in _cache:
        path = PROMPT_DIR / f"{name}.txt"
        try:
            _cache[name] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationInvalid(
                f"Prompt template {name!r} is missing from the deployment package."
            ) from exc
    return _cache[name]


def render(name, **values):
    """Fill a template, refusing to leave a placeholder unfilled.

    An unresolved `{width}` reaching the model produces a plausible image at the
    wrong size, which then fails three stages later. Failing here names the cause.
    """
    text = load(name)
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    leftover = _unfilled(text)
    if leftover:
        raise ConfigurationInvalid(
            f"Prompt {name!r} has unfilled placeholders: {', '.join(leftover)}"
        )
    return text


def _unfilled(text):
    import re

    return sorted(set(re.findall(r"\{([a-z_]+)\}", text)))


def bullet_list(items):
    """Format strings as a bullet list, or a clear marker when empty."""
    items = [i for i in items if i]
    if not items:
        return "(none)"
    return "\n".join(f"- {i}" for i in items)


def copy_block(items):
    """Render the copy manifest so each line binds a contentKey to exact text."""
    if not items:
        return "(none)"
    return "\n".join(f'- {i["contentKey"]}: "{i["text"]}"' for i in items)


def field_block(pairs):
    """Render `label: value` lines, omitting anything absent.

    Absent fields are omitted rather than rendered as "none", so the model is
    never invited to fill a gap it can see.
    """
    lines = [f"- {label}: {value}" for label, value in pairs if value]
    return "\n".join(lines) if lines else "(not supplied)"


def instructions_block(instructions):
    if not instructions:
        return ""
    return f"ADDITIONAL INSTRUCTIONS:\n{instructions}"
