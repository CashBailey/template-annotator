"""End-to-end CLI failure modes.

The contract every case checks: a bad input exits non-zero, says why on stderr,
never prints a traceback, and never leaves a half-written output file behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

#: A path that cannot exist, used for every "input file is missing" case.
MISSING = "/nonexistent-areadef-dir/missing"


def _assert_clean_failure(result, *, code=1, contains=""):
    """Non-zero exit, an explanation on stderr, and no traceback anywhere."""
    assert result.code == code, result
    assert "Traceback" not in result.stderr, result
    assert "Traceback" not in result.stdout, result
    assert result.stderr.strip(), result
    if contains:
        assert contains in result.stderr, result


def _write_project(path: Path, shapes=None, image=None) -> Path:
    payload = {
        "schema_version": 1,
        "image": image or {"path": None, "width": 800, "height": 600},
        "shapes": shapes if shapes is not None else [
            {"id": 1, "name": "only", "kind": "rect",
             "bbox": [10.0, 20.0, 110.0, 60.0], "page": 0},
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Missing input files -- every subcommand that reads a path
# ---------------------------------------------------------------------------

MISSING_INPUT_CASES = {
    "info": ("info", f"{MISSING}.png"),
    "render": ("render", f"{MISSING}.pdf"),
    "normalize-json": ("normalize-json", f"{MISSING}.json"),
    "new-project": ("new-project", f"{MISSING}.png"),
    "detect-lines": ("detect-lines", f"{MISSING}.png"),
    "detect-checkboxes": ("detect-checkboxes", f"{MISSING}.png"),
    "detect-text": ("detect-text", f"{MISSING}.png"),
    "detect-markers": ("detect-markers", f"{MISSING}.png"),
    "detect-barcodes": ("detect-barcodes", f"{MISSING}.png"),
    "detect-high-contrast": ("detect-high-contrast", f"{MISSING}.png"),
    "snap-to-lines": ("snap-to-lines", f"{MISSING}.json", "--source", f"{MISSING}.png"),
    "align": ("align", f"{MISSING}.json", "--op", "left", "--key", "1"),
    "add-shape": ("add-shape", f"{MISSING}.json", "--bbox", "1", "2", "3", "4"),
    "list-shapes": ("list-shapes", f"{MISSING}.json"),
    "edit-shape": ("edit-shape", f"{MISSING}.json", "--id", "1", "--delete"),
    "clear-shapes": ("clear-shapes", f"{MISSING}.json"),
    "export-csv": ("export-csv", f"{MISSING}.json"),
    "screenshot": ("screenshot", f"{MISSING}.json"),
}


@pytest.mark.parametrize("args", list(MISSING_INPUT_CASES.values()),
                         ids=list(MISSING_INPUT_CASES))
def test_missing_input_file_fails_cleanly(cli, args):
    # The wording is not uniform (`render` reports PyMuPDF's "no such file"
    # instead of the CLI's "file does not exist"), but every command must name
    # the path it could not read.
    _assert_clean_failure(cli.run(*args), contains=MISSING)


def test_missing_source_for_snap_is_named_separately(cli, tmp_path, fixtures_dir):
    """snap-to-lines takes two paths; the message must say which one is missing."""
    project = _write_project(tmp_path / "p.json")
    result = cli.run("snap-to-lines", project, "--source", f"{MISSING}.png")
    _assert_clean_failure(result, contains="source image/PDF does not exist")


def test_missing_into_target_fails(cli, fixtures_dir, tmp_path):
    result = cli.run("detect-checkboxes", fixtures_dir / "checkboxes_600x400.png",
                     "--into", f"{MISSING}.json", "-o", tmp_path / "out.json")
    _assert_clean_failure(result, contains="project to merge into does not exist")
    assert not (tmp_path / "out.json").exists()


# ---------------------------------------------------------------------------
# Page ranges
# ---------------------------------------------------------------------------

def test_screenshot_page_out_of_range_on_single_image(cli, fixtures_dir, tmp_path):
    """A single-image project has exactly one page (areaDef.py:5239)."""
    out = tmp_path / "shot.png"
    result = cli.run("screenshot", fixtures_dir / "projects" / "simple_rects.json",
                     "--page", 3, "-o", out)
    _assert_clean_failure(result, contains="out of range (0..0)")
    assert not out.exists(), "a failed screenshot must not leave a PNG behind"


def test_screenshot_page_out_of_range_on_pdf(cli, fixtures_dir, tmp_path):
    pdf = fixtures_dir / "sample_form.pdf"
    project = _write_project(
        tmp_path / "pdf_project.json",
        image={"path": str(pdf), "width": 800, "height": 600},
    )
    out = tmp_path / "shot.png"
    result = cli.run("screenshot", project, "--page", 5, "-o", out)
    _assert_clean_failure(result, contains="out of range (0..2)")
    assert not out.exists()


def test_detect_on_missing_pdf_page(cli, fixtures_dir):
    result = cli.run("detect-lines", fixtures_dir / "sample_form.pdf", "--page", 9)
    _assert_clean_failure(result, contains="out of range")


# ---------------------------------------------------------------------------
# Corrupt / truncated / wrong-shaped JSON
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["list-shapes", "export-csv", "screenshot",
                                     "clear-shapes", "normalize-json"])
def test_truncated_json_project_fails_cleanly(cli, tmp_path, command):
    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"schema_version": 1, "shapes": [', encoding="utf-8")
    out = tmp_path / f"out_{command}"
    args = [command, truncated]
    if command != "list-shapes":
        args += ["-o", out]
    result = cli.run(*args)
    _assert_clean_failure(result)
    assert not out.exists(), f"{command} wrote an output file despite failing"


def test_json_root_must_be_an_object(cli, tmp_path):
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    _assert_clean_failure(cli.run("list-shapes", array),
                          contains="root must be an object")


def test_malformed_shapes_are_reported_not_silently_dropped(cli, tmp_path):
    """A load that drops shapes still succeeds, but it must say so on stderr."""
    project = _write_project(tmp_path / "half_bad.json", shapes=[
        {"id": 1, "name": "ok", "kind": "rect", "bbox": [1, 2, 30, 40], "page": 0},
        {"id": 2, "name": "truncated_bbox", "kind": "rect", "bbox": [1, 2], "page": 0},
    ])
    result = cli.run("list-shapes", project)
    assert result.ok(), result
    assert "Warning: 1 malformed shape(s) were dropped" in result.stderr, result
    assert "1 shape(s)" in result.stdout, result


# ---------------------------------------------------------------------------
# Bad arguments to a valid command
# ---------------------------------------------------------------------------

def test_edit_shape_unknown_id(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    out = tmp_path / "out.json"
    result = cli.run("edit-shape", project, "--id", 999, "--name", "nope", "-o", out)
    _assert_clean_failure(result, contains="no shape with id 999")
    assert not out.exists()
    assert json.loads(project.read_text(encoding="utf-8"))["shapes"][0]["name"] == "only"


def test_edit_shape_conflicting_actions(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("edit-shape", project, "--id", 1, "--delete", "--name", "x",
                     "-o", tmp_path / "out.json")
    _assert_clean_failure(result, contains="cannot be combined")


def test_edit_shape_without_an_action(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("edit-shape", project, "--id", 1, "-o", tmp_path / "out.json")
    _assert_clean_failure(result, contains="no edit specified")


def test_align_unknown_key_shape(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("align", project, "--op", "left", "--key", 42,
                     "-o", tmp_path / "out.json")
    _assert_clean_failure(result, contains="no shape with id 42")


def test_align_needs_more_than_the_key(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("align", project, "--op", "left", "--key", 1,
                     "-o", tmp_path / "out.json")
    _assert_clean_failure(result, contains="at least one shape besides the key")


def test_ambiguous_shape_id_across_pages(cli, fixtures_dir, tmp_path):
    """Ids repeat across PDF pages; the CLI must refuse to guess."""
    project = _write_project(tmp_path / "p.json", shapes=[
        {"id": 1, "name": "p0", "kind": "rect", "bbox": [1, 2, 30, 40], "page": 0},
        {"id": 1, "name": "p1", "kind": "rect", "bbox": [1, 2, 30, 40], "page": 1},
    ])
    result = cli.run("edit-shape", project, "--id", 1, "--delete",
                     "-o", tmp_path / "out.json")
    _assert_clean_failure(result, contains="specify --page")


def test_non_finite_coordinates_rejected(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("add-shape", project, "--bbox", "nan", 1, 2, 3,
                     "-o", tmp_path / "out.json")
    _assert_clean_failure(result, contains="must be finite")


def test_render_rejects_a_non_pdf(cli, fixtures_dir, tmp_path):
    result = cli.run("render", fixtures_dir / "form_800x600.png",
                     "--out-dir", tmp_path / "pages")
    _assert_clean_failure(result, contains="expects a PDF document")


def test_render_rejects_a_non_positive_scale(cli, fixtures_dir, tmp_path):
    result = cli.run("render", fixtures_dir / "sample_form.pdf", "--scale", 0,
                     "--out-dir", tmp_path / "pages")
    _assert_clean_failure(result, contains="scale must be greater than zero")


# ---------------------------------------------------------------------------
# argparse-level failures (exit 2)
# ---------------------------------------------------------------------------

def test_unknown_subcommand_is_argparse_exit_2(cli):
    result = cli.run("frobnicate")
    _assert_clean_failure(result, code=2, contains="invalid choice: 'frobnicate'")


def test_unknown_shape_kind_is_argparse_exit_2(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("add-shape", project, "--kind", "hexagon",
                     "--bbox", 1, 2, 3, 4, "-o", tmp_path / "out.json")
    _assert_clean_failure(result, code=2, contains="invalid choice: 'hexagon'")
    assert not (tmp_path / "out.json").exists()


def test_unknown_align_op_is_argparse_exit_2(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("align", project, "--op", "diagonal", "--key", 1)
    _assert_clean_failure(result, code=2, contains="invalid choice: 'diagonal'")


def test_missing_required_flag_is_argparse_exit_2(cli, tmp_path):
    project = _write_project(tmp_path / "p.json")
    result = cli.run("add-shape", project)          # --bbox is required
    _assert_clean_failure(result, code=2, contains="--bbox")
