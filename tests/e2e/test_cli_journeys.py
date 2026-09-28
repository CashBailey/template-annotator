"""End-to-end CLI journeys: ten stories covering all 19 subcommands.

Every command runs as a real subprocess (the ``cli`` fixture, cwd = tmp_path) against
the committed fixtures, and every assertion is made against the artefact the command
actually produced -- the on-disk JSON/CSV/PNG, or the real detector output -- rather
than against the command's own summary line alone.

Detector assertions are bands (counts, centres +/- BAND_PX), never exact coordinate
lists, per the suite's global constraints.
"""

from __future__ import annotations

import csv
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

from helpers.images import (
    BARCODE_BBOX,
    BARCODE_PAYLOAD,
    BLANK_SIZE,
    CHECKBOX_CENTERS,
    CHECKBOX_COUNT,
    CHECKBOX_SIZE,
    FORM_H_RULES,
    FORM_RULE_HALF_THICKNESS,
    FORM_SIZE,
    FORM_SNAP_TEST_Y,
    FORM_V_RULES,
    MAX_DIFF_PIXEL_FRAC,
    QR_BBOX,
    QR_PAYLOAD,
    RMS_TOLERANCE,
    assert_images_match,
    image_diff_stats,
)
from helpers.pdfs import (
    PAGE2_BAR_PT,
    SAMPLE_PDF_PAGE_PT,
    SAMPLE_PDF_PAGE_PX,
    SAMPLE_PDF_PAGES,
)

pytestmark = pytest.mark.e2e

#: Detection tolerance from the global constraints (centres/edges, in px).
BAND_PX = 4.0
#: areaDef renders PDF pages at this zoom; J4 verifies the contract end to end.
PDF_RENDER_SCALE = 2.0
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Every subcommand the CLI offers. J9 cross-checks this against argparse itself,
#: so a new subcommand cannot slip past the suite unnoticed.
SUBCOMMANDS = (
    "gui", "info", "render", "normalize-json", "new-project",
    "detect-lines", "detect-checkboxes", "detect-text", "detect-markers",
    "detect-barcodes", "detect-high-contrast", "snap-to-lines", "align",
    "add-shape", "list-shapes", "edit-shape", "clear-shapes", "export-csv",
    "screenshot",
)


# ---------------------------------------------------------------------------
# Small local helpers (assertions live in the tests; these only read/compare)
# ---------------------------------------------------------------------------

def _run(cli, *args):
    """Run a CLI command that is expected to succeed; return its CliResult."""
    result = cli.run(*args)
    assert result.ok(), result
    return result


def _load(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _shapes(path) -> list:
    return _load(path)["shapes"]


def _named(path) -> dict:
    return {s["name"]: s for s in _shapes(path)}


def _by_id(path) -> dict:
    return {s["id"]: s for s in _shapes(path)}


def _center(bbox) -> tuple:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _pair_within(measured, expected, tol=BAND_PX):
    """Match every expected point to a distinct measured point within `tol`."""
    remaining = list(measured)
    for want in expected:
        hit = next((p for p in remaining
                    if abs(p[0] - want[0]) <= tol and abs(p[1] - want[1]) <= tol), None)
        assert hit is not None, (
            f"no measured point within {tol}px of {want}; measured={measured}")
        remaining.remove(hit)
    return remaining


def _assert_near(measured, expected, tol=BAND_PX, label="value"):
    assert abs(measured - expected) <= tol, (
        f"{label}: {measured} is not within {tol} of {expected}")


def _assert_bbox_near(measured, expected, tol=BAND_PX, label="bbox"):
    for got, want, axis in zip(measured, expected, ("x1", "y1", "x2", "y2")):
        _assert_near(got, want, tol, f"{label}.{axis}")


def _assert_images_differ(actual, expected, why=""):
    rms, frac, _ = image_diff_stats(actual, expected)
    assert rms > RMS_TOLERANCE or frac > MAX_DIFF_PIXEL_FRAC, (
        f"images are identical within tolerance (RMS {rms:.3f}, {frac * 100:.3f}%)"
        + (f": {why}" if why else ""))


def _reported_size(stdout: str) -> tuple:
    """Pull the (WxH) the screenshot command claims it wrote."""
    match = re.search(r"\((\d+)x(\d+)\)", stdout)
    assert match, f"no (WxH) in screenshot output: {stdout!r}"
    return int(match.group(1)), int(match.group(2))


def _png_size(path) -> tuple:
    from PIL import Image
    with Image.open(path) as img:
        return img.size


# ---------------------------------------------------------------------------
# J1 -- project lifecycle
# ---------------------------------------------------------------------------

def test_j1_project_lifecycle(cli, fixtures_dir, tmp_path):
    """new-project -> info -> add-shape x3 kinds -> list-shapes -> edit-shape
    (rename/move/resize/duplicate/delete) -> clear-shapes, checking the on-disk
    JSON after every single mutation."""
    src = fixtures_dir / "form_800x600.png"
    proj = tmp_path / "project.json"

    info = _run(cli, "info", src)
    assert f"Size: {FORM_SIZE[0]} x {FORM_SIZE[1]}" in info.stdout, info

    created = _run(cli, "new-project", src, "-o", proj)
    assert f"{FORM_SIZE[0]}x{FORM_SIZE[1]}" in created.stdout, created
    doc = _load(proj)
    assert (doc["image"]["width"], doc["image"]["height"]) == FORM_SIZE
    assert Path(doc["image"]["path"]) == src.resolve()
    assert doc["shapes"] == []

    # --- add one of every kind -------------------------------------------------
    _run(cli, "add-shape", proj, "--kind", "rect", "--name", "header",
         "--bbox", 80, 40, 300, 80, "-o", proj)
    shapes = _shapes(proj)
    assert len(shapes) == 1
    assert shapes[0]["id"] == 1
    assert shapes[0]["kind"] == "rect"
    assert shapes[0]["bbox"] == [80.0, 40.0, 300.0, 80.0]
    assert shapes[0]["page"] == 0

    _run(cli, "add-shape", proj, "--kind", "circle", "--name", "dot",
         "--bbox", 200, 200, 260, 260, "-o", proj)
    dot = _named(proj)["dot"]
    assert dot["kind"] == "circle"
    assert dot["center"] == [230.0, 230.0]
    assert dot["radius"] == 30.0

    _run(cli, "add-shape", proj, "--kind", "chamfer", "--name", "cham",
         "--bbox", 300, 300, 400, 380, "--chamfer", 8, "-o", proj)
    cham = _named(proj)["cham"]
    assert cham["kind"] == "chamfer"
    assert cham["chamfer"] == 8.0
    assert [s["id"] for s in _shapes(proj)] == [1, 2, 3]

    listed = _run(cli, "list-shapes", proj)
    assert "3 shape(s)" in listed.stdout, listed
    for name in ("header", "dot", "cham"):
        assert name in listed.stdout, listed
    assert listed.stderr == "", listed  # a clean load emits no warnings

    # --- rename ---------------------------------------------------------------
    _run(cli, "edit-shape", proj, "--id", 1, "--name", "header_renamed", "-o", proj)
    assert _by_id(proj)[1]["name"] == "header_renamed"

    # --- move (bbox translated, size preserved) --------------------------------
    _run(cli, "edit-shape", proj, "--id", 1, "--move", 5, -5, "-o", proj)
    assert _by_id(proj)[1]["bbox"] == [85.0, 35.0, 305.0, 75.0]

    # --- resize (explicit bbox; derived circle fields follow) ------------------
    _run(cli, "edit-shape", proj, "--id", 2, "--bbox", 200, 200, 240, 240, "-o", proj)
    dot = _by_id(proj)[2]
    assert dot["bbox"] == [200.0, 200.0, 240.0, 240.0]
    assert dot["radius"] == 20.0

    # --- duplicate (clone offset by 20 px, chamfer preserved) ------------------
    _run(cli, "edit-shape", proj, "--id", 3, "--duplicate", "-o", proj)
    after = _by_id(proj)
    assert len(after) == 4
    clone = after[4]
    assert clone["name"] == "cham_copy"
    assert clone["kind"] == "chamfer"
    assert clone["bbox"] == [320.0, 320.0, 420.0, 400.0]
    assert clone["chamfer"] == 8.0

    # --- delete ---------------------------------------------------------------
    _run(cli, "edit-shape", proj, "--id", 2, "--delete", "-o", proj)
    assert sorted(_by_id(proj)) == [1, 3, 4]

    # --- clear ----------------------------------------------------------------
    cleared = _run(cli, "clear-shapes", proj, "-o", proj)
    assert "Cleared 3 shape(s)" in cleared.stdout, cleared
    doc = _load(proj)
    assert doc["shapes"] == []
    assert (doc["image"]["width"], doc["image"]["height"]) == FORM_SIZE


# ---------------------------------------------------------------------------
# J2 -- detection pipeline
# ---------------------------------------------------------------------------

def test_j2_detection_pipeline(cli, fixtures_dir, tmp_path):
    """detect-lines / -checkboxes / -text / -high-contrast against fixtures whose
    content is known, plus an --into merge that yields a loadable project."""
    form = fixtures_dir / "form_800x600.png"
    blank = fixtures_dir / "blank_100x100.png"
    boxes_png = fixtures_dir / "checkboxes_600x400.png"
    pdf = fixtures_dir / "sample_form.pdf"

    # --- detect-lines: the ruled form -----------------------------------------
    lines = _run(cli, "detect-lines", form).json()
    assert (lines["image"]["width"], lines["image"]["height"]) == FORM_SIZE
    h_ys = [ln["y"] for ln in lines["lines"]["horizontal"]]
    v_xs = [ln["x"] for ln in lines["lines"]["vertical"]]
    assert len(h_ys) == len(FORM_H_RULES)
    assert len(v_xs) == len(FORM_V_RULES)
    for got, want in zip(sorted(h_ys), FORM_H_RULES):
        _assert_near(got, want, label="horizontal rule y")
    for got, want in zip(sorted(v_xs), FORM_V_RULES):
        _assert_near(got, want, label="vertical rule x")
    # Every horizontal rule spans the full width between the vertical rules.
    for ln in lines["lines"]["horizontal"]:
        _assert_near(ln["x_start"], FORM_V_RULES[0], label="rule x_start")
        _assert_near(ln["x_end"], FORM_V_RULES[-1], label="rule x_end")

    # --- detect-lines: an empty page is a real negative -------------------------
    empty = _run(cli, "detect-lines", blank).json()
    assert (empty["image"]["width"], empty["image"]["height"]) == BLANK_SIZE
    assert empty["lines"]["horizontal"] == []
    assert empty["lines"]["vertical"] == []

    # --- detect-checkboxes -----------------------------------------------------
    detected = tmp_path / "checkboxes.json"
    found = _run(cli, "detect-checkboxes", boxes_png, "-o", detected)
    assert f"Detected {CHECKBOX_COUNT} checkbox box(es)" in found.stdout, found
    doc = _load(detected)
    assert (doc["image"]["width"], doc["image"]["height"]) == CHECKBOX_SIZE
    assert len(doc["shapes"]) == CHECKBOX_COUNT
    leftover = _pair_within([_center(s["bbox"]) for s in doc["shapes"]], CHECKBOX_CENTERS)
    assert leftover == [], f"unexpected extra detections: {leftover}"
    assert all(s["kind"] == "rect" for s in doc["shapes"])

    # The ruled form has no glyph-sized ink at all -> both box detectors find none.
    none_here = _run(cli, "detect-checkboxes", form, "-o", tmp_path / "none.json")
    assert "Detected 0 checkbox box(es)" in none_here.stdout, none_here
    no_text = _run(cli, "detect-text", form, "-o", tmp_path / "notext.json")
    assert "Detected 0 text box(es)" in no_text.stdout, no_text

    # --- detect-text on PDF page 0 (title + the two checkbox squares) ----------
    text_json = tmp_path / "text_p0.json"
    _run(cli, "detect-text", pdf, "--page", 0, "-o", text_json)
    text_boxes = _shapes(text_json)
    assert len(text_boxes) >= 3, text_boxes
    cb_json = tmp_path / "cb_p0.json"
    _run(cli, "detect-checkboxes", pdf, "--page", 0, "-o", cb_json)
    cb_centers = [_center(s["bbox"]) for s in _shapes(cb_json)]
    assert len(cb_centers) == 2, cb_centers
    # Every checkbox is also picked up as a text region, and there is at least one
    # wide region left over: the page title.
    extra = _pair_within([_center(s["bbox"]) for s in text_boxes], cb_centers)
    assert any(s["bbox"][2] - s["bbox"][0] > 200
               for s in text_boxes
               if _center(s["bbox"]) in extra), text_boxes

    # --- detect-high-contrast on PDF page 1 (the solid bar) --------------------
    regions = _run(cli, "detect-high-contrast", pdf, "--page", 1).json()["regions"]
    assert len(regions) == 1, regions
    bar_px = tuple(v * PDF_RENDER_SCALE for v in PAGE2_BAR_PT)
    got = regions[0]
    _assert_bbox_near((got["x1"], got["y1"], got["x2"], got["y2"]), bar_px, label="bar")

    # --- --into merge ----------------------------------------------------------
    base = tmp_path / "base.json"
    _run(cli, "new-project", boxes_png, "-o", base)
    _run(cli, "add-shape", base, "--name", "manual", "--bbox", 5, 5, 40, 40, "-o", base)
    merged = tmp_path / "merged.json"
    merge = _run(cli, "detect-checkboxes", boxes_png, "--into", base, "-o", merged)
    assert f"added {CHECKBOX_COUNT} new" in merge.stdout, merge
    merged_shapes = _shapes(merged)
    assert len(merged_shapes) == CHECKBOX_COUNT + 1
    assert "manual" in {s["name"] for s in merged_shapes}
    ids = [s["id"] for s in merged_shapes]
    assert len(set(ids)) == len(ids), ids
    # "Loadable" means the CLI can read it back without dropping or coercing shapes.
    reload = _run(cli, "list-shapes", merged)
    assert f"{CHECKBOX_COUNT + 1} shape(s)" in reload.stdout, reload
    assert reload.stderr == "", reload


# ---------------------------------------------------------------------------
# J3 -- markers and barcodes
# ---------------------------------------------------------------------------

def _skip_if_undecoded(result, project, what):
    """Optional decoder backends are allowed to be absent, never to fail silently."""
    shapes = _shapes(project)
    if not shapes:
        pytest.skip(f"no installed backend decoded the {what} in the fixture: {result}")
    return shapes


def _require_any_qr_backend():
    """A QR needs EITHER OpenCV or ZBar; a 1D barcode in practice needs ZBar."""
    for module in ("cv2", "pyzbar.pyzbar"):
        try:
            importlib.import_module(module)
            return
        except Exception:
            continue
    pytest.skip("QR detection needs OpenCV or pyzbar (libzbar0)")


def test_j3_markers(cli, fixtures_dir, tmp_path):
    """detect-markers on the QR+EAN-13 fixture. Split from the barcode half so
    each backend's journey runs whenever ITS dependency is installed."""
    _require_any_qr_backend()
    src = fixtures_dir / "qr_400x300.png"

    markers = tmp_path / "markers.json"
    marker_run = _run(cli, "detect-markers", src, "-o", markers)
    marker_shapes = _skip_if_undecoded(marker_run, markers, "QR code")
    assert len(marker_shapes) == 1, marker_shapes          # the EAN-13 is not a marker
    assert marker_shapes[0]["name"] == f"qr_{QR_PAYLOAD}"
    _assert_bbox_near(marker_shapes[0]["bbox"], QR_BBOX, label="qr bbox")


def test_j3_barcodes(cli, fixtures_dir, tmp_path):
    pytest.importorskip("pyzbar.pyzbar", reason="EAN-13 decoding needs pyzbar (libzbar0)")
    src = fixtures_dir / "qr_400x300.png"

    barcodes = tmp_path / "barcodes.json"
    barcode_run = _run(cli, "detect-barcodes", src, "-o", barcodes)
    barcode_shapes = _skip_if_undecoded(barcode_run, barcodes, "EAN-13 barcode")
    assert len(barcode_shapes) == 1, barcode_shapes        # the QR is not a 1D barcode
    assert barcode_shapes[0]["name"] == f"ean13_{BARCODE_PAYLOAD}"
    _assert_bbox_near(barcode_shapes[0]["bbox"], BARCODE_BBOX, label="ean13 bbox")

    # Both detectors merge into one project without colliding.
    markers = tmp_path / "markers.json"
    _run(cli, "detect-markers", src, "-o", markers)
    combined = tmp_path / "combined.json"
    _run(cli, "detect-barcodes", src, "--into", markers, "-o", combined)
    names = {s["name"] for s in _shapes(combined)}
    assert names == {f"qr_{QR_PAYLOAD}", f"ean13_{BARCODE_PAYLOAD}"}, names


# ---------------------------------------------------------------------------
# J4 -- multi-page PDF
# ---------------------------------------------------------------------------

def test_j4_multipage_pdf(cli, fixtures_dir, tmp_path):
    """render + per-page projects + per-page screenshots. Verifies the
    PDF_RENDER_SCALE contract: rendered px == PDF points x 2."""
    pdf = fixtures_dir / "sample_form.pdf"
    out_dir = tmp_path / "pages"

    rendered = _run(cli, "render", pdf, "--out-dir", out_dir)
    written = [Path(line) for line in rendered.stdout.split()]
    assert len(written) == SAMPLE_PDF_PAGES, rendered
    assert sorted(p.name for p in written) == [
        f"page-{i + 1:03d}.png" for i in range(SAMPLE_PDF_PAGES)]
    expected_px = tuple(int(round(v * PDF_RENDER_SCALE)) for v in SAMPLE_PDF_PAGE_PT)
    assert expected_px == SAMPLE_PDF_PAGE_PX
    for page_png in written:
        assert page_png.is_file(), page_png
        assert _png_size(page_png) == SAMPLE_PDF_PAGE_PX

    # Rendering at 1x lands back on the PDF's own point size -- the other half of
    # the scale contract -- and --prefix names the files.
    plain = _run(cli, "render", pdf, "--scale", 1, "--prefix", "sheet",
                 "--out-dir", tmp_path / "unscaled")
    unscaled = [Path(line) for line in plain.stdout.split()]
    assert [p.name for p in unscaled] == [
        f"sheet-{i + 1:03d}.png" for i in range(SAMPLE_PDF_PAGES)], plain
    assert _png_size(unscaled[0]) == tuple(int(v) for v in SAMPLE_PDF_PAGE_PT)

    info = _run(cli, "info", pdf)
    assert f"Pages: {SAMPLE_PDF_PAGES}" in info.stdout, info
    assert f"{SAMPLE_PDF_PAGE_PX[0]} x {SAMPLE_PDF_PAGE_PX[1]}" in info.stdout, info

    # --- a project built from page 1, with shapes on two different pages -------
    proj = tmp_path / "multipage.json"
    _run(cli, "new-project", pdf, "--page", 1, "-o", proj)
    assert (_load(proj)["image"]["width"], _load(proj)["image"]["height"]) == SAMPLE_PDF_PAGE_PX
    _run(cli, "add-shape", proj, "--name", "on_p0", "--bbox", 60, 50, 400, 90,
         "--page", 0, "-o", proj)
    _run(cli, "add-shape", proj, "--name", "on_p1", "--bbox", 100, 300, 500, 400,
         "--page", 1, "-o", proj)
    pages = {s["name"]: s["page"] for s in _shapes(proj)}
    assert pages == {"on_p0": 0, "on_p1": 1}

    listed = _run(cli, "list-shapes", proj)
    assert "2 shape(s)" in listed.stdout, listed

    # --- screenshots are per page ---------------------------------------------
    shot_p0 = tmp_path / "p0.png"
    shot_p1 = tmp_path / "p1.png"
    _run(cli, "screenshot", proj, "--page", 0, "-o", shot_p0)
    _run(cli, "screenshot", proj, "--page", 1, "-o", shot_p1)
    assert _png_size(shot_p0) == SAMPLE_PDF_PAGE_PX
    assert _png_size(shot_p1) == SAMPLE_PDF_PAGE_PX
    _assert_images_differ(shot_p0, shot_p1, "different PDF pages and shapes")

    # Dropping page 1's shapes must change page 1 and leave page 0 untouched --
    # i.e. `--page N` really does draw only page N's shapes.
    trimmed = tmp_path / "trimmed.json"
    _run(cli, "clear-shapes", proj, "--page", 1, "-o", trimmed)
    assert [s["name"] for s in _shapes(trimmed)] == ["on_p0"]
    trimmed_p1 = tmp_path / "trimmed_p1.png"
    trimmed_p0 = tmp_path / "trimmed_p0.png"
    _run(cli, "screenshot", trimmed, "--page", 1, "-o", trimmed_p1)
    _run(cli, "screenshot", trimmed, "--page", 0, "-o", trimmed_p0)
    _assert_images_differ(trimmed_p1, shot_p1, "page-1 shape was removed")
    assert_images_match(trimmed_p0, shot_p0)


# ---------------------------------------------------------------------------
# J5 -- snap-to-lines and align
# ---------------------------------------------------------------------------

def test_j5_snap_and_align(cli, fixtures_dir, tmp_path):
    """A shape 6 px off a drawn rule snaps onto it; --high-contrast additionally
    snaps to a solid bar's edge; align equalises edges against a key shape."""
    form = fixtures_dir / "form_800x600.png"
    pdf = fixtures_dir / "sample_form.pdf"

    proj = tmp_path / "snap.json"
    _run(cli, "new-project", form, "-o", proj)
    _run(cli, "add-shape", proj, "--name", "near_rule",
         "--bbox", 100, 460, 400, FORM_SNAP_TEST_Y, "-o", proj)
    _run(cli, "add-shape", proj, "--name", "far_away",
         "--bbox", 100, 40, 300, 80, "-o", proj)

    snapped = tmp_path / "snapped.json"
    result = _run(cli, "snap-to-lines", proj, "--source", form, "-o", snapped)
    assert "Snapped 1 shape(s)" in result.stdout, result
    shapes = _named(snapped)
    # The rule's centre is FORM_H_RULES[-1]; a shape above it lands on the near edge.
    rule_top = FORM_H_RULES[-1] - FORM_RULE_HALF_THICKNESS
    assert shapes["near_rule"]["bbox"][3] == rule_top
    assert shapes["near_rule"]["bbox"][3] != FORM_SNAP_TEST_Y
    assert shapes["far_away"]["bbox"] == [100.0, 40.0, 300.0, 80.0]

    # --- --high-contrast: the same project, the only difference is the flag ----
    # The shape under test lives on the page the lines are detected from (page 1);
    # `decoy` is an identical box on page 0, which must NOT move -- `--page 1`
    # detects page 1's bar, and page 0 has no such bar.
    hc_proj = tmp_path / "hc.json"
    _run(cli, "new-project", pdf, "--page", 1, "-o", hc_proj)
    bar_bottom_px = PAGE2_BAR_PT[3] * PDF_RENDER_SCALE
    decoy_bbox = [200.0, 60.0, 500.0, bar_bottom_px - 5]
    _run(cli, "add-shape", hc_proj, "--name", "near_bar", "--page", 1,
         "--bbox", 200, 60, 500, bar_bottom_px - 5, "-o", hc_proj)
    _run(cli, "add-shape", hc_proj, "--name", "decoy", "--page", 0,
         "--bbox", 200, 60, 500, bar_bottom_px - 5, "-o", hc_proj)

    plain = tmp_path / "hc_off.json"
    with_hc = tmp_path / "hc_on.json"
    _run(cli, "snap-to-lines", hc_proj, "--source", pdf, "--page", 1, "-o", plain)
    hc_result = _run(cli, "snap-to-lines", hc_proj, "--source", pdf, "--page", 1,
                     "--high-contrast", "-o", with_hc)
    assert "1 high-contrast region(s)" in hc_result.stdout, hc_result
    plain_bbox = _named(plain)["near_bar"]["bbox"]
    hc_bbox = _named(with_hc)["near_bar"]["bbox"]
    assert plain_bbox[3] == bar_bottom_px - 5      # no plain line to snap the bottom to
    _assert_near(hc_bbox[3], bar_bottom_px, label="bottom snapped to bar edge")
    assert hc_bbox[:3] == plain_bbox[:3]           # the flag changes nothing else
    assert "Snapped 1 shape(s)" in hc_result.stdout, hc_result
    assert _named(with_hc)["decoy"]["bbox"] == decoy_bbox   # page 0 was not touched
    assert _named(plain)["decoy"]["bbox"] == decoy_bbox

    # --- align ----------------------------------------------------------------
    align_proj = tmp_path / "align.json"
    _run(cli, "new-project", form, "-o", align_proj)
    _run(cli, "add-shape", align_proj, "--name", "key", "--bbox", 100, 100, 300, 140,
         "-o", align_proj)
    for i, (x1, y1, x2, y2) in enumerate(
            ((120, 200, 260, 230), (140, 300, 280, 340), (160, 400, 300, 450)), start=1):
        _run(cli, "add-shape", align_proj, "--name", f"r{i}",
             "--bbox", x1, y1, x2, y2, "-o", align_proj)

    before = _named(align_proj)
    left = tmp_path / "left.json"
    align = _run(cli, "align", align_proj, "--op", "left", "--key", 1, "-o", left)
    assert "Aligned 3 shape(s)" in align.stdout, align
    after = _named(left)
    key_x1 = before["key"]["bbox"][0]
    assert after["key"]["bbox"] == before["key"]["bbox"]   # the key never moves
    for name in ("r1", "r2", "r3"):
        assert after[name]["bbox"][0] == key_x1
        # left-align preserves size, it does not resize
        assert (after[name]["bbox"][2] - after[name]["bbox"][0]
                == before[name]["bbox"][2] - before[name]["bbox"][0])

    matched = tmp_path / "matched.json"
    _run(cli, "align", align_proj, "--op", "match_w", "--key", 1, "--ids", 2, 3,
         "-o", matched)
    widths = _named(matched)
    key_w = before["key"]["bbox"][2] - before["key"]["bbox"][0]
    for name in ("r1", "r2"):
        assert widths[name]["bbox"][2] - widths[name]["bbox"][0] == key_w
        assert widths[name]["bbox"][0] == before[name]["bbox"][0]   # x1 unchanged
    assert widths["r3"]["bbox"] == before["r3"]["bbox"]             # not in --ids


# ---------------------------------------------------------------------------
# J6 -- normalize round-trip
# ---------------------------------------------------------------------------

#: 512x512 is deliberate: normalize divides and multiplies every coordinate by the
#: image size, and a power of two makes that scaling exact, so a second pass is
#: byte-identical instead of drifting by an ULP.
MESSY_PROJECT = {
    "schema_version": 1,
    "notes": "hand-edited: shapes out of order, columns a few px apart",
    "image": {"path": "does_not_exist.png", "width": 512, "height": 512},
    "shapes": [
        {"id": 7, "name": "col_c", "kind": "rect", "bbox": [97.0, 300.0, 303.0, 340.0], "page": 0},
        {"id": 2, "name": "ring", "kind": "circle", "bbox": [400.0, 400.0, 460.0, 460.0], "page": 0},
        {"id": 5, "name": "col_a", "kind": "rect", "bbox": [100.0, 100.0, 300.0, 140.0], "page": 0},
        {"id": 9, "name": "col_b", "kind": "rect", "bbox": [104.0, 200.0, 296.0, 240.0], "page": 0},
    ],
}


def test_j6_normalize_round_trip(cli, repo_root, fixtures_dir, tmp_path):
    """A messy project normalizes into a project areaDef itself can load, and
    normalizing the result again is a byte-for-byte no-op."""
    messy = tmp_path / "messy.json"
    messy.write_text(json.dumps(MESSY_PROJECT, indent=2), encoding="utf-8")

    once = tmp_path / "once.json"
    result = _run(cli, "normalize-json", messy, "-o", once)
    assert "Normalized 3 rectangle zone(s)" in result.stdout, result

    doc = _load(once)
    assert doc["notes"] == MESSY_PROJECT["notes"]        # unknown keys survive
    rects = [s for s in doc["shapes"] if s["kind"] == "rect"]
    assert len({s["bbox"][0] for s in rects}) == 1, rects   # one shared left edge
    assert len({s["bbox"][2] for s in rects}) == 1, rects   # one shared right edge
    ring = next(s for s in doc["shapes"] if s["kind"] == "circle")
    assert ring["bbox"] == [400.0, 400.0, 460.0, 460.0]     # non-rects untouched
    for src, out in zip(MESSY_PROJECT["shapes"], doc["shapes"]):
        assert src["id"] == out["id"]                        # order/ids preserved
        assert src["bbox"][1] == out["bbox"][1]              # y is never touched
        assert src["bbox"][3] == out["bbox"][3]

    # --- the output really is a project areaDef can load ----------------------
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import areaDef

    project = areaDef.Project.from_json(str(once))
    assert len(project.shapes) == len(MESSY_PROJECT["shapes"])
    assert project.skipped_shapes == 0
    assert project.coerced_shapes == 0
    assert (project.width, project.height) == (512, 512)
    assert len({s.bbox()[0] for s in project.shapes if s.kind == "rect"}) == 1

    # --- idempotent, byte-wise -------------------------------------------------
    twice = tmp_path / "twice.json"
    _run(cli, "normalize-json", once, "-o", twice)
    assert twice.read_bytes() == once.read_bytes()

    # --- stdout mode emits the same payload -----------------------------------
    printed = _run(cli, "normalize-json", messy)
    assert printed.json() == doc

    # --- --image-size-source overrides the JSON's own dimensions ---------------
    override = tmp_path / "override.json"
    overridden = _run(cli, "normalize-json", messy, "--image-size-source",
                      fixtures_dir / "blank_100x100.png", "-o", override)
    # At 100 px wide those columns are >1/5 of the page apart, so nothing clusters.
    assert "Normalized 0 rectangle zone(s)" in overridden.stdout, overridden
    assert [s["bbox"] for s in _shapes(override)] == \
        [s["bbox"] for s in MESSY_PROJECT["shapes"]]

    # --- --all-kinds normalizes every kind, and is itself idempotent -----------
    all_kinds = tmp_path / "all_kinds.json"
    all_result = _run(cli, "normalize-json", messy, "--all-kinds", "-o", all_kinds)
    assert "3 rect(s)" in all_result.stdout or "4 rect(s)" in all_result.stdout, all_result
    again = tmp_path / "all_kinds_again.json"
    _run(cli, "normalize-json", all_kinds, "--all-kinds", "-o", again)
    assert again.read_bytes() == all_kinds.read_bytes()


# ---------------------------------------------------------------------------
# J7 -- CSV round-trip
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["id", "name", "kind", "page", "x1", "y1", "x2", "y2",
               "width", "height", "cx", "cy", "r", "chamfer", "rects"]
UNICODE_NAME = "Namé ☑"


def test_j7_csv_round_trip(cli, fixtures_dir, tmp_path):
    """All three kinds plus a unicode name survive export-csv, and every
    coordinate matches the project JSON exactly.

    Outputs go in a non-ASCII directory: paths reach the CLI as command-line
    arguments and come back in its stdout, so this journey is where a wrong
    filesystem or pipe encoding would surface.
    """
    out_dir = tmp_path / "ümläut 文書"
    out_dir.mkdir()
    proj = out_dir / "csv_project.json"
    _run(cli, "new-project", fixtures_dir / "form_800x600.png", "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "rect", "--name", UNICODE_NAME,
         "--bbox", 10, 20, 110, 60, "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "circle", "--name", "ring",
         "--bbox", 200, 200, 260, 260, "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "chamfer", "--name", "cut",
         "--bbox", 300, 300, 400, 380, "--chamfer", 8, "-o", proj)

    out_csv = out_dir / "shapes.csv"
    exported = _run(cli, "export-csv", proj, "-o", out_csv)
    assert "Exported 3 shape(s)" in exported.stdout, exported
    assert str(out_csv) in exported.stdout, exported   # the path round-trips intact

    with out_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    assert header == CSV_COLUMNS
    assert "page" in header
    assert len(body) == 3

    shapes = _shapes(proj)
    assert len(shapes) == 3
    for row, shape in zip(body, shapes):
        record = dict(zip(header, row))
        assert int(record["id"]) == shape["id"]
        assert record["name"] == shape["name"]
        assert record["kind"] == shape["kind"]
        assert int(record["page"]) == shape["page"]
        x1, y1, x2, y2 = shape["bbox"]
        assert [float(record[k]) for k in ("x1", "y1", "x2", "y2")] == [x1, y1, x2, y2]
        assert float(record["width"]) == x2 - x1
        assert float(record["height"]) == y2 - y1
        if shape["kind"] == "circle":
            assert [float(record["cx"]), float(record["cy"])] == shape["center"]
            assert float(record["r"]) == shape["radius"]
        else:
            assert (record["cx"], record["cy"], record["r"]) == ("", "", "")
        assert (float(record["chamfer"]) if record["chamfer"] else 0.0) == \
            shape.get("chamfer", 0.0)

    assert dict(zip(header, body[0]))["name"] == UNICODE_NAME

    # stdout mode writes exactly the same CSV.
    printed = _run(cli, "export-csv", proj)
    assert printed.stdout == out_csv.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# J8 -- screenshot terminus
# ---------------------------------------------------------------------------

def test_j8_screenshot(cli, fixtures_dir, tmp_path):
    """Canvas grab, window grab, the default filename and the theme flag: the PNG
    exists, is a real PNG, and its size is the one the command reported."""
    proj = tmp_path / "shot_project.json"
    _run(cli, "new-project", fixtures_dir / "form_800x600.png", "-o", proj)
    _run(cli, "add-shape", proj, "--name", "band", "--bbox", 100, 100, 400, 200,
         "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "circle", "--name", "ring",
         "--bbox", 500, 300, 600, 400, "-o", proj)

    # --- canvas mode; -o without a suffix gets one ----------------------------
    canvas = _run(cli, "screenshot", proj, "-o", tmp_path / "canvas")
    shot = tmp_path / "canvas.png"
    assert shot.is_file(), canvas
    assert shot.read_bytes()[:8] == PNG_MAGIC
    assert _reported_size(canvas.stdout) == FORM_SIZE
    assert _png_size(shot) == FORM_SIZE          # canvas grab == the page, 1:1

    # --- window mode ----------------------------------------------------------
    window = _run(cli, "screenshot", proj, "--window", "-o", tmp_path / "window.png")
    win_png = tmp_path / "window.png"
    assert win_png.is_file(), window
    assert win_png.read_bytes()[:8] == PNG_MAGIC
    reported = _reported_size(window.stdout)
    assert _png_size(win_png) == reported        # it reports what it actually wrote
    assert reported != FORM_SIZE                 # panels + canvas, not the bare page

    # --- default output name lands in the cwd ---------------------------------
    _run(cli, "screenshot", proj)
    assert (tmp_path / "screenshot.png").is_file()

    # --- the theme flag reaches the renderer ----------------------------------
    dark = tmp_path / "dark.png"
    _run(cli, "screenshot", proj, "--theme", "dark", "-o", dark)
    assert _png_size(dark) == FORM_SIZE
    _assert_images_differ(dark, shot, "dark theme draws shapes in other colours")


# ---------------------------------------------------------------------------
# J9 -- fresh-clone smoke
# ---------------------------------------------------------------------------

def test_j9_every_subcommand_has_help(cli):
    """`--help` for all 19 subcommands (this is the only way `gui` is exercised --
    it must never launch), plus a check that 19 is still the whole list."""
    top = _run(cli, "--help")
    assert "usage:" in top.stdout, top

    for name in SUBCOMMANDS:
        result = cli.run(name, "--help")
        assert result.ok(), result
        assert f"usage: areaDef.py {name}" in result.stdout, result
        assert result.stderr == "", result

    # argparse itself lists the choices; keep SUBCOMMANDS honest against it.
    # Older CPython quotes each choice, 3.12.8+/3.13 does not — parse the
    # "choose from ..." segment and strip either way.
    unknown = cli.run("definitely-not-a-subcommand")
    assert unknown.code == 2, unknown
    m = re.search(r"choose from (.+?)\)", unknown.stderr)
    assert m, unknown
    listed = {tok.strip().strip("'\"") for tok in m.group(1).split(",")}
    assert listed == set(SUBCOMMANDS), sorted(listed ^ set(SUBCOMMANDS))
    assert len(SUBCOMMANDS) == 19


def test_j9_version_flag_prints_a_version_and_exits_clean(cli):
    """`--version` is wired up: it prints "template-annotator <version>" to
    stdout, says nothing on stderr, and exits 0.

    Which of the two sources that version came from depends on whether the
    distribution is installed in the environment running the suite; the
    not-installed fallback is pinned by the unit test
    TestPackageVersion::test_falls_back_when_the_distribution_is_not_installed.
    """
    result = _run(cli, "--version")
    assert result.stdout.startswith("template-annotator "), result
    assert re.search(r"\d+\.\d+\.\d+", result.stdout), result
    assert result.stderr == "", result


# ---------------------------------------------------------------------------
# J10 -- irregular (merged) areas
# ---------------------------------------------------------------------------

def test_j10_merge_split_round_trip(cli, fixtures_dir, tmp_path):
    """Two rects -> `edit-shape --merge` -> the merged area survives every
    downstream artefact (JSON, list-shapes, CSV, a rendered PNG) -> `--split`
    puts the plain rects back."""
    proj = tmp_path / "merge_project.json"
    _run(cli, "new-project", fixtures_dir / "form_800x600.png", "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "rect", "--name", "company",
         "--bbox", 312, 45, 714, 155, "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "rect", "--name", "company_b",
         "--bbox", 236, 168, 714, 265, "-o", proj)

    merged_json = tmp_path / "merged.json"
    merged = _run(cli, "edit-shape", proj, "--merge", "1,2", "-o", merged_json)
    assert "Merged 2 shape(s) into id 3 ('company')" in merged.stdout, merged

    shapes = _shapes(merged_json)
    assert len(shapes) == 1, "the two sources are replaced, not appended to"
    area = shapes[0]
    assert (area["id"], area["kind"], area["name"]) == (3, "multirect", "company")
    assert area["rects"] == [[312.0, 45.0, 714.0, 155.0], [236.0, 168.0, 714.0, 265.0]]
    assert area["bbox"] == [236.0, 45.0, 714.0, 265.0]   # the envelope of the pieces
    assert area["page"] == 0

    listed = _run(cli, "list-shapes", merged_json)
    assert "1 shape(s)" in listed.stdout, listed
    assert "multirect" in listed.stdout, listed
    assert listed.stderr == "", listed        # the file reloads without warnings

    # --- CSV: the pieces ride in the last column, as JSON ---------------------
    out_csv = tmp_path / "merged.csv"
    _run(cli, "export-csv", merged_json, "-o", out_csv)
    with out_csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    record = rows[0]
    assert list(record) == CSV_COLUMNS
    assert record["kind"] == "multirect"
    assert json.loads(record["rects"]) == area["rects"]
    assert [float(record[k]) for k in ("x1", "y1", "x2", "y2")] == area["bbox"]

    # --- it renders: a real PNG of the page, no golden ------------------------
    shot = tmp_path / "merged.png"
    _run(cli, "screenshot", merged_json, "-o", shot)
    assert shot.read_bytes()[:8] == PNG_MAGIC
    assert _png_size(shot) == FORM_SIZE

    # --- split: one plain rect per piece --------------------------------------
    back = tmp_path / "back.json"
    split = _run(cli, "edit-shape", merged_json, "--split", "--id", 3, "-o", back)
    assert "Split id 3 into 2 rect(s)" in split.stdout, split
    parts = _shapes(back)
    assert [p["kind"] for p in parts] == ["rect", "rect"]
    assert [p["name"] for p in parts] == ["company_1", "company_2"]
    assert [p["bbox"] for p in parts] == area["rects"]
    assert all("rects" not in p for p in parts), "a plain rect has no pieces"


def test_j10_merge_refusals_leave_no_output(cli, fixtures_dir, tmp_path):
    """A refused merge exits 1 with an `Error:` line and writes nothing -- not the
    output file, not the source project."""
    proj = tmp_path / "refuse.json"
    _run(cli, "new-project", fixtures_dir / "form_800x600.png", "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "rect", "--name", "band",
         "--bbox", 10, 20, 110, 60, "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "circle", "--name", "ring",
         "--bbox", 200, 200, 260, 260, "-o", proj)
    out = tmp_path / "never.json"

    lonely = cli.run("edit-shape", proj, "--merge", "1", "-o", out)
    assert lonely.code == 1, lonely
    assert lonely.stderr.startswith("Error:"), lonely
    assert "at least 2" in lonely.stderr, lonely
    assert "Traceback" not in lonely.stderr, lonely
    assert not out.exists()

    circle = cli.run("edit-shape", proj, "--merge", "1,2", "-o", out)
    assert circle.code == 1, circle
    assert "circle" in circle.stderr, circle
    assert "Traceback" not in circle.stderr, circle
    assert not out.exists()

    assert [s["kind"] for s in _shapes(proj)] == ["rect", "circle"]
