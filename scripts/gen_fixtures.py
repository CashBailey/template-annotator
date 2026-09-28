#!/usr/bin/env python3
"""Regenerate every committed file under ``tests/fixtures/``.

    python scripts/gen_fixtures.py            # (re)write tests/fixtures/
    python scripts/gen_fixtures.py --check    # verify committed bytes, exit 1 on drift

Everything is deterministic: pure PIL geometry (no fonts, no randomness, no
timestamps) for the PNGs, and a metadata- and /ID-scrubbed PDF. Running
``--check`` twice in a row must pass both times.

The KNOWN content of each fixture lives in ``tests/helpers/images.py`` and
``tests/helpers/pdfs.py`` — assert against those constants, not magic numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from helpers import images, pdfs  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

# --- project JSONs ---------------------------------------------------------
# Written exactly as areaDef's Project.write does (indent=2, no trailing
# newline), so load + rewrite round-trips byte-for-byte.

SIMPLE_RECTS = {
    "schema_version": 1,
    "image": {"path": "form_800x600.png", "width": 800, "height": 600},
    "shapes": [
        {"id": 1, "name": "header", "kind": "rect",
         "bbox": [80.0, 40.0, 300.0, 80.0], "page": 0},
        # y2 = 500 sits exactly 6 px above the last rule of form_800x600.png
        # (y = 506), i.e. inside the default snap tolerance of 12 px.
        {"id": 2, "name": "field_a", "kind": "rect",
         "bbox": [80.0, 460.0, 400.0, 500.0], "page": 0},
        {"id": 3, "name": "field_b", "kind": "rect",
         "bbox": [420.0, 460.0, 700.0, 500.0], "page": 0},
    ],
}

MULTIPAGE = {
    "schema_version": 1,
    "image": {"path": "sample_form.pdf", "width": 800, "height": 600},
    "shapes": [
        {"id": 1, "name": "p0_title", "kind": "rect",
         "bbox": [60.0, 50.0, 400.0, 90.0], "page": 0},
        {"id": 2, "name": "p0_field", "kind": "rect",
         "bbox": [60.0, 300.0, 400.0, 340.0], "page": 0},
        {"id": 3, "name": "p1_header", "kind": "rect",
         "bbox": [60.0, 100.0, 740.0, 144.0], "page": 1},
        {"id": 4, "name": "p1_cell", "kind": "rect",
         "bbox": [60.0, 200.0, 300.0, 240.0], "page": 1},
        {"id": 5, "name": "p2_box", "kind": "rect",
         "bbox": [200.0, 180.0, 600.0, 420.0], "page": 2},
    ],
}

PROJECTS = {"simple_rects.json": SIMPLE_RECTS, "multipage.json": MULTIPAGE}


def generate(dest: Path) -> list[Path]:
    """Write every fixture under ``dest``; returns the paths relative to it."""
    written = list(images.write_pngs(dest))
    written.append(pdfs.build_sample_form_pdf(dest / "sample_form.pdf"))

    projects = dest / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    for name, payload in PROJECTS.items():
        path = projects / name
        path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")
        written.append(path)
    return [p.relative_to(dest) for p in written]


def _all_files(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def check() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        expected = set(generate(fresh))
        committed = _all_files(FIXTURES_DIR) if FIXTURES_DIR.is_dir() else set()

        problems = []
        for rel in sorted(expected - committed):
            problems.append(f"MISSING  {rel}")
        for rel in sorted(committed - expected):
            problems.append(f"STALE    {rel}")
        for rel in sorted(expected & committed):
            if (fresh / rel).read_bytes() != (FIXTURES_DIR / rel).read_bytes():
                problems.append(f"DIFFERS  {rel}")

    if problems:
        print("\n".join(problems))
        print(f"\n{len(problems)} fixture problem(s). Run: python {Path(__file__).name}",
              file=sys.stderr)
        return 1
    print(f"OK: {len(expected)} fixture(s) byte-identical.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="verify the committed fixtures instead of rewriting them")
    args = ap.parse_args()
    if args.check:
        return check()
    for rel in generate(FIXTURES_DIR):
        print(f"wrote {FIXTURES_DIR.relative_to(REPO_ROOT) / rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
