"""Visual regression against 4 committed golden screenshots.

Oracle: the ``screenshot`` CLI subcommand, canvas mode only (never
``--window``), ``theme=light``. Every screenshot is its own subprocess via
the ``cli`` fixture, so each one gets isolated Qt state.

Goldens live in ``tests/goldens/linux-py/``, keyed by platform + Qt/Freetype
toolchain -- rendering is not byte-portable across OSes. When that directory
is absent the golden-comparison test skips with a clear reason; the two
structural checks below are golden-free (no dependency on ``goldens_dir`` at
all) and always run, including on a platform with no committed goldens.

Label rendering matters here: AnnotatorWindow draws each shape's ``name`` as
a QGraphicsTextItem, and font rasterization depends on whatever
"sans-serif" resolves to on the box that generates the goldens. This dev
box has Noto/Ubuntu/URW fonts installed (``fc-match`` picks "Noto Sans");
CI installs only ``fonts-dejavu-core``. Rather than chase font parity
across machines, every golden shape carries an empty name (set via
``edit-shape --name ""``), which draws a zero-glyph label item -- the
committed goldens are geometry-only. g2 still round-trips a unicode name
through ``add-shape``/``edit-shape`` (proving the CLI handles it) before
blanking it for the actual screenshot. See ``tests/goldens/README.md``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from helpers.images import (
    CHECKBOX_SIZE,
    FORM_SIZE,
    MAX_DIFF_PIXEL_FRAC,
    RMS_TOLERANCE,
    assert_images_match,
    image_diff_stats,
)
from helpers.pdfs import SAMPLE_PDF_PAGE_PX

pytestmark = [pytest.mark.e2e, pytest.mark.visual]


def _run(cli, *args):
    result = cli.run(*args)
    assert result.ok(), result
    return result


def _png_size(path):
    from PIL import Image
    with Image.open(path) as img:
        return img.size


def _shoot(cli, proj, page, out, theme="light"):
    _run(cli, "screenshot", proj, "--page", page, "--theme", theme, "-o", out)
    return out


def _strip_names(cli, proj_path, ids):
    """Blank every shape's name so the label item Qt draws has no glyphs."""
    for sid in ids:
        _run(cli, "edit-shape", proj_path, "--id", sid, "--name", "", "-o", proj_path)


def _write_project(path: Path, image: dict, shapes: list) -> Path:
    payload = {"schema_version": 1, "image": image, "shapes": shapes}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Golden project builders -- each returns (project_path, page, expected_size)
# ---------------------------------------------------------------------------

def _g1_simple_rects(cli, fixtures_dir, tmp_path):
    """g1: the committed simple_rects.json fixture over form_800x600.png."""
    src_json = fixtures_dir / "projects" / "simple_rects.json"
    data = json.loads(src_json.read_text(encoding="utf-8"))
    for shape in data["shapes"]:
        shape["name"] = ""
    proj = tmp_path / "g1_simple_rects.json"
    proj.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # image.path is a bare filename resolved against the CLI's cwd (tmp_path).
    shutil.copy(fixtures_dir / "form_800x600.png", tmp_path / "form_800x600.png")
    return proj, 0, FORM_SIZE


def _g2_circle_chamfer(cli, fixtures_dir, tmp_path):
    """g2: circle + chamfer over checkboxes_600x400.png. A unicode name is
    round-tripped through add-shape/edit-shape (proving the CLI handles it)
    then blanked before the golden screenshot -- see module docstring."""
    source = tmp_path / "g2_source.png"
    shutil.copy(fixtures_dir / "checkboxes_600x400.png", source)
    proj = tmp_path / "g2_circle_chamfer.json"
    _run(cli, "new-project", source, "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "circle", "--name", "円★",
         "--bbox", 90, 80, 230, 220, "-o", proj)
    _run(cli, "add-shape", proj, "--kind", "chamfer", "--name", "étiquette",
         "--bbox", 320, 140, 520, 300, "--chamfer", 24, "-o", proj)
    doc = json.loads(proj.read_text(encoding="utf-8"))
    names = {s["name"] for s in doc["shapes"]}
    assert names == {"円★", "étiquette"}, doc  # unicode round-trip proof
    _strip_names(cli, proj, [s["id"] for s in doc["shapes"]])
    return proj, 0, CHECKBOX_SIZE


def _g3_pdf_page1(cli, fixtures_dir, tmp_path):
    """g3: PDF page 1 (index 0) of sample_form.pdf, with 2 rects over the
    known title band and first checkbox (see task-2-report.md Sec 2)."""
    pdf = fixtures_dir / "sample_form.pdf"
    proj = tmp_path / "g3_pdf_page1.json"
    _run(cli, "new-project", pdf, "--page", 0, "-o", proj)
    _run(cli, "add-shape", proj, "--name", "title_box",
         "--bbox", 55, 20, 380, 60, "-o", proj)
    _run(cli, "add-shape", proj, "--name", "checkbox_box",
         "--bbox", 55, 415, 150, 450, "-o", proj)
    doc = json.loads(proj.read_text(encoding="utf-8"))
    _strip_names(cli, proj, [s["id"] for s in doc["shapes"]])
    return proj, 0, SAMPLE_PDF_PAGE_PX


def _g4_sourceless_blank(cli, fixtures_dir, tmp_path):
    """g4: no source image at all -- ``_cli_screenshot`` synthesizes a blank
    white page from the declared dims. No image decode, no font dependency:
    the most stable of the four goldens."""
    size = (640, 480)
    shapes = [
        {"id": 1, "name": "", "kind": "rect", "bbox": [50.0, 50.0, 250.0, 150.0], "page": 0},
        {"id": 2, "name": "", "kind": "circle", "bbox": [350.0, 60.0, 450.0, 160.0], "page": 0},
        {"id": 3, "name": "", "kind": "chamfer", "bbox": [100.0, 260.0, 320.0, 400.0],
         "chamfer": 24.0, "page": 0},
    ]
    proj = tmp_path / "g4_sourceless.json"
    _write_project(proj, {"path": None, "width": size[0], "height": size[1]}, shapes)
    return proj, 0, size


_BUILDERS = {
    "g1_simple_rects": _g1_simple_rects,
    "g2_circle_chamfer": _g2_circle_chamfer,
    "g3_pdf_page1": _g3_pdf_page1,
    "g4_sourceless_blank": _g4_sourceless_blank,
}
GOLDEN_IDS = tuple(_BUILDERS)


def _build(golden_id, cli, fixtures_dir, tmp_path):
    return _BUILDERS[golden_id](cli, fixtures_dir, tmp_path)


# ---------------------------------------------------------------------------
# Golden comparison (skips cleanly when tests/goldens/linux-py is absent)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("golden_id", GOLDEN_IDS)
def test_golden_screenshot(golden_id, cli, fixtures_dir, tmp_path, goldens_dir, update_goldens):
    proj, page, expected_size = _build(golden_id, cli, fixtures_dir, tmp_path)
    golden_path = goldens_dir / f"{golden_id}.png"

    if update_goldens:
        goldens_dir.mkdir(parents=True, exist_ok=True)
        first = _shoot(cli, proj, page, tmp_path / f"{golden_id}_update1.png")
        assert _png_size(first) == expected_size, (golden_id, _png_size(first), expected_size)
        shutil.copy(first, golden_path)
        # Self-consistency: a second, independent screenshot run (fresh
        # subprocess, fresh Qt state) must still compare clean against the
        # golden we just wrote -- proves the update flow is idempotent.
        second = _shoot(cli, proj, page, tmp_path / f"{golden_id}_update2.png")
        assert_images_match(second, golden_path)
        return

    if not goldens_dir.exists():
        pytest.skip(f"no golden images for this platform: {goldens_dir} does not exist "
                    f"(regenerate with --update-goldens)")

    assert golden_path.is_file(), f"missing golden {golden_path} (run with --update-goldens)"
    actual = _shoot(cli, proj, page, tmp_path / f"{golden_id}_actual.png")

    actual_size, golden_size = _png_size(actual), _png_size(golden_path)
    assert actual_size == golden_size == expected_size, (
        f"{golden_id}: size mismatch -- actual {actual_size}, golden {golden_size}, "
        f"expected {expected_size}")

    rms, frac, _ = image_diff_stats(actual, golden_path)
    print(f"[golden] {golden_id}: RMS={rms:.4f} (limit {RMS_TOLERANCE}) "
          f"frac={frac:.5f} (limit {MAX_DIFF_PIXEL_FRAC})")
    assert_images_match(actual, golden_path)


# ---------------------------------------------------------------------------
# Golden-free structural checks -- run everywhere, would run on macOS too
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("golden_id", GOLDEN_IDS)
def test_screenshot_size_matches_source(golden_id, cli, fixtures_dir, tmp_path):
    proj, page, expected_size = _build(golden_id, cli, fixtures_dir, tmp_path)
    out = _shoot(cli, proj, page, tmp_path / "shot.png")
    assert _png_size(out) == expected_size


@pytest.mark.parametrize("golden_id", GOLDEN_IDS)
def test_screenshot_shapes_vs_cleared_differ(golden_id, cli, fixtures_dir, tmp_path):
    proj, page, _size = _build(golden_id, cli, fixtures_dir, tmp_path)
    with_shapes = _shoot(cli, proj, page, tmp_path / "with_shapes.png")

    cleared = tmp_path / "cleared.json"
    _run(cli, "clear-shapes", proj, "--page", page, "-o", cleared)
    without_shapes = _shoot(cli, cleared, page, tmp_path / "without_shapes.png")

    rms, frac, _ = image_diff_stats(with_shapes, without_shapes)
    assert rms > RMS_TOLERANCE or frac > MAX_DIFF_PIXEL_FRAC, (
        f"{golden_id}: with/without-shapes screenshots are identical within "
        f"tolerance (RMS {rms:.3f}, {frac * 100:.3f}%)")


# ---------------------------------------------------------------------------
# Determinism hardening -- two independent subprocess runs of the same
# project must be byte-identical, or at worst within the golden tolerance.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("golden_id", GOLDEN_IDS)
def test_screenshot_determinism_across_runs(golden_id, cli, fixtures_dir, tmp_path):
    proj, page, _size = _build(golden_id, cli, fixtures_dir, tmp_path)
    run1 = _shoot(cli, proj, page, tmp_path / "run1.png")
    run2 = _shoot(cli, proj, page, tmp_path / "run2.png")

    identical = run1.read_bytes() == run2.read_bytes()
    rms, frac, _ = image_diff_stats(run1, run2)
    print(f"[determinism] {golden_id}: byte_identical={identical} "
          f"RMS={rms:.4f} frac={frac:.5f}")
    assert rms <= RMS_TOLERANCE and frac <= MAX_DIFF_PIXEL_FRAC, (
        f"{golden_id}: two screenshots of the same project differ beyond "
        f"tolerance across independent subprocess runs (RMS {rms:.3f}, "
        f"{frac * 100:.3f}%)")
