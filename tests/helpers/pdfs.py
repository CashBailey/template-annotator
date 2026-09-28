"""Deterministic 3-page ``sample_form.pdf`` builder (PyMuPDF).

Byte-identical on every run: fixed page geometry, fixed metadata (including the
dates), and the three things MuPDF injects by itself scrubbed afterwards — the
trailer ``/ID``, the ``% Written by MuPDF x.y.z`` header comment, and the
``/Producer (MuPDF x.y.z)`` that MuPDF embeds *inside the Catalog* (separate
from the ``/Info`` object the trailer points at). ``build_sample_form_pdf``
raises if the string ``MuPDF`` survives anywhere in the output.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz

#: Page geometry. areaDef renders PDFs at PDF_RENDER_SCALE = 2.0, so every page
#: is 800 x 600 px once rendered — the same pixel space as form_800x600.png.
SAMPLE_PDF_PAGE_PT = (400.0, 300.0)
SAMPLE_PDF_PAGE_PX = (800, 600)
SAMPLE_PDF_PAGES = 3

_FONT = "helv"
_FIXED_DATE = "D:20240101000000Z"
_FIXED_ID = (b"/ID[<00000000000000000000000000000001>"
             b"<00000000000000000000000000000002>]")
_ID_RE = re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]")
_HEADER_COMMENT_RE = re.compile(rb"^% Written by [^\n]*$", re.MULTILINE)
_HEADER_COMMENT = b"% areaDef fixture"
#: Replaces the Catalog's inline /Info, which MuPDF fills with its own version.
_CATALOG_INFO = "<</Producer(scripts/gen_fixtures.py)>>"

#: Page 1 horizontal rules, in points (px = pt * 2).
PAGE1_RULES_PT = (80.0, 130.0, 180.0)
#: Page 1 checkbox squares (top-left corners, in points); each is 12 pt square.
PAGE1_CHECKBOX_PT = ((30.0, 210.0), (60.0, 210.0))
PAGE1_CHECKBOX_SIDE_PT = 12.0
#: Page 2 solid header bar (a high-contrast region), in points.
PAGE2_BAR_PT = (30.0, 50.0, 370.0, 72.0)
PAGE2_RULES_PT = (100.0, 140.0, 180.0, 220.0)
#: Page 3 single outlined rectangle, in points.
PAGE3_RECT_PT = (100.0, 90.0, 300.0, 210.0)

_RULE_X_PT = (30.0, 370.0)


def _page(doc):
    return doc.new_page(width=SAMPLE_PDF_PAGE_PT[0], height=SAMPLE_PDF_PAGE_PT[1])


def build_sample_form_pdf(path: Path) -> Path:
    """Write the 3-page fixture PDF to ``path`` and return it."""
    doc = fitz.open()

    # Page 1 - a form: title, three rules, two checkbox squares.
    p1 = _page(doc)
    p1.insert_text((30, 34), "SAMPLE FORM PAGE 1", fontname=_FONT, fontsize=14)
    for y in PAGE1_RULES_PT:
        p1.draw_line(fitz.Point(_RULE_X_PT[0], y), fitz.Point(_RULE_X_PT[1], y), width=1.5)
    for x, y in PAGE1_CHECKBOX_PT:
        s = PAGE1_CHECKBOX_SIDE_PT
        p1.draw_rect(fitz.Rect(x, y, x + s, y + s), width=1.0)

    # Page 2 - a table: solid knockout header bar, then four rules.
    p2 = _page(doc)
    p2.insert_text((30, 34), "SAMPLE FORM PAGE 2", fontname=_FONT, fontsize=14)
    p2.draw_rect(fitz.Rect(*PAGE2_BAR_PT), color=(0, 0, 0), fill=(0, 0, 0), width=0)
    for y in PAGE2_RULES_PT:
        p2.draw_line(fitz.Point(_RULE_X_PT[0], y), fitz.Point(_RULE_X_PT[1], y), width=1.5)

    # Page 3 - sparse: one big outlined box and nothing else.
    p3 = _page(doc)
    p3.insert_text((30, 34), "SAMPLE FORM PAGE 3", fontname=_FONT, fontsize=14)
    p3.draw_rect(fitz.Rect(*PAGE3_RECT_PT), width=2.0)

    doc.set_metadata({
        "title": "areaDef sample form",
        "author": "areaDef test fixtures",
        "subject": "E2E fixture",
        "keywords": "areadef,fixture",
        "creator": "scripts/gen_fixtures.py",
        "producer": "scripts/gen_fixtures.py",
        "creationDate": _FIXED_DATE,
        "modDate": _FIXED_DATE,
        "trapped": "",
    })
    # MuPDF also stamps its version into an /Info dict *inside the Catalog*,
    # which set_metadata() does not touch. Overwrite it before serializing.
    doc.xref_set_key(doc.pdf_catalog(), "Info", _CATALOG_INFO)
    data = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()

    # The trailer sits after the xref table, so swapping the /ID (same length)
    # cannot disturb any offset or startxref.
    patched, n = _ID_RE.subn(_FIXED_ID, data)
    if n != 1:
        raise RuntimeError(f"expected exactly one trailer /ID to scrub, found {n}")

    # The "% Written by MuPDF x.y.z" header comment precedes every object, so it
    # must be replaced with EXACTLY as many bytes or every xref offset breaks.
    # PyMuPDF >= 1.28 stopped writing the comment entirely, so zero matches is
    # fine (the desired end state); more than one means the writer changed in a
    # way this scrub does not understand.
    # ponytail: same-length padding, not an offset rewrite — a MuPDF upgrade that
    # changes this comment's *length* still shifts the fixture bytes, but so
    # would any other change in its writer. --check guards hand-edits, not
    # library upgrades. Rewrite the xref offsets only if that stops being true.
    patched, n = _HEADER_COMMENT_RE.subn(
        lambda m: _HEADER_COMMENT[:len(m.group(0))].ljust(len(m.group(0))), patched)
    if n > 1:
        raise RuntimeError(f"expected at most one MuPDF header comment, found {n}")

    if b"MuPDF" in patched:
        raise RuntimeError("a MuPDF version string leaked into the fixture PDF")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(patched)
    return path
