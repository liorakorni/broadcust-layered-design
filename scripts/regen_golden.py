#!/usr/bin/env python3
"""Regenerate tests/golden/design.html from the fixture in tests/test_assemble.py.

Run this only after a deliberate change to the frozen HTML structure, and review
the resulting diff against docs/CONTRACTS.md section 5 before committing.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from lib.assemble_html import assemble  # noqa: E402
from tests.test_assemble import ASSETS, CANVAS, GOLDEN, scene  # noqa: E402

GOLDEN.parent.mkdir(parents=True, exist_ok=True)
GOLDEN.write_text(assemble(scene(), ASSETS, CANVAS), encoding="utf-8")
print(f"wrote {GOLDEN}")
