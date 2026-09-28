"""GUI data-safety tests: the paths where unsaved annotations can be lost.

Everything here drives the real seams -- menu actions, shortcuts, the PDF page
buttons -- with the scripted dialog stubs from ``tests/conftest.py``. The close
prompt itself lives in ``test_gui_dialogs.py`` (D4); this file covers the OTHER
document-replacing transitions (Open, Load JSON), the save -> load round trip,
and the corrupt-input paths that used to abort the process.
"""

from __future__ import annotations

import json
import sys

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMessageBox

import areaDef
from helpers.gui import (
    CTRL,
    click,
    draw,
    flush_timers,
    key,
    load_media,
    menu_action,
    show_window,
)
from helpers.images import FORM_SIZE

pytestmark = [pytest.mark.e2e, pytest.mark.gui]

SAMPLE_PDF_PAGES = 3


@pytest.fixture
def win(annotator_window, qtbot):
    return show_window(annotator_window, qtbot)


@pytest.fixture
def loaded_win(win, qtbot, fixtures_dir):
    load_media(win, qtbot, fixtures_dir / "form_800x600.png")
    return win


def load_json_via_menu(win, path):
    """File > Load JSON (no shortcut -> trigger the QAction)."""
    menu_action(win, "File", "Load JSON").trigger()


def levels(stub):
    return [level for level, _title, _text in stub.messages]


def titles(stub):
    return [title for _level, title, _text in stub.messages]


# ---------------------------------------------------------------------------
# D6 -- save -> Load JSON round trip
# ---------------------------------------------------------------------------

def test_d6_save_then_load_json_round_trip(loaded_win, qtbot, tmp_path,
                                           no_file_dialogs, fixtures_dir):
    """Ctrl+S then File > Load JSON restores exactly what was drawn."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 100, 500, 220), tool_key=Qt.Key.Key_C)
    win.set_selected(win.shapes[0].sid)
    win.name_edit.setText("invoice_no")
    win.apply_name()

    out = tmp_path / "roundtrip.json"
    no_file_dialogs.save_paths = [str(out)]
    key(qtbot, win, Qt.Key.Key_S, CTRL)
    assert out.is_file()
    assert win._has_unsaved_changes is False

    before = [(s.sid, s.kind, s.name, s.bbox(), s.page) for s in win.shapes]
    assert win.next_sid == 3

    # Replace the document with something else, then load the saved file back.
    load_media(win, qtbot, fixtures_dir / "blank_100x100.png")
    assert win.shapes == []

    no_file_dialogs.open_paths = [str(out)]
    load_json_via_menu(win, out)
    flush_timers(qtbot)

    assert no_file_dialogs.open_calls[-1] == ("open", "Load JSON")
    assert [(s.sid, s.kind, s.name, s.bbox(), s.page) for s in win.shapes] == before
    assert win.next_sid == 3
    assert win.areas_list.count() == 2
    assert win.image_path == str((fixtures_dir / "form_800x600.png").resolve())
    assert win.canvas.image_size == FORM_SIZE
    assert win._has_unsaved_changes is False
    assert not win.windowTitle().startswith("*")
    assert "form_800x600.png" in win.windowTitle()


def test_d6_malformed_json_reports_and_keeps_the_document(loaded_win, qtbot, tmp_path,
                                                          no_file_dialogs,
                                                          auto_answer_messagebox):
    """A file that is not JSON at all: one error dialog, document untouched."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    kept = [s.bbox() for s in win.shapes]

    bad = tmp_path / "broken.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    no_file_dialogs.open_paths = [str(bad)]
    auto_answer_messagebox.answer = QMessageBox.StandardButton.No  # discard prompt
    load_json_via_menu(win, bad)

    assert levels(auto_answer_messagebox) == ["critical"]
    assert [s.bbox() for s in win.shapes] == kept
    assert win.canvas.image_size == FORM_SIZE
    assert win.isVisible() is True


def test_d6_pdf_out_of_range_page_is_clamped_with_a_warning(win, qtbot, tmp_path,
                                                            fixtures_dir,
                                                            no_file_dialogs,
                                                            auto_answer_messagebox):
    """A shape recorded on page 99 of a 3-page PDF lands on the last page and
    the user is told, instead of vanishing (or indexing out of range)."""
    pdf = fixtures_dir / "sample_form.pdf"
    project = tmp_path / "outofrange.json"
    project.write_text(json.dumps({
        "schema_version": 1,
        "image": {"path": str(pdf), "width": 800, "height": 600},
        "shapes": [
            {"id": 1, "name": "on_page_0", "kind": "rect",
             "bbox": [10, 10, 60, 60], "page": 0},
            {"id": 1, "name": "ghost", "kind": "rect",
             "bbox": [20, 20, 80, 80], "page": 99},
        ],
    }), encoding="utf-8")

    no_file_dialogs.open_paths = [str(project)]
    load_json_via_menu(win, project)
    flush_timers(qtbot)

    assert titles(auto_answer_messagebox) == ["Loaded with adjustments"]
    assert "out-of-range" in auto_answer_messagebox.messages[0][2]
    assert win._is_pdf is True and win._pdf_page_count == SAMPLE_PDF_PAGES
    assert win._current_pdf_page == 0
    assert [s.name for s in win.shapes] == ["on_page_0"]
    last = SAMPLE_PDF_PAGES - 1
    assert [s.name for s in win._pdf_page_states[last].shapes] == ["ghost"]
    # The clamped shape is really reachable: page forward to it.
    for _ in range(last):
        qtbot.mouseClick(win.pdf_next_btn, Qt.MouseButton.LeftButton)
    assert win._current_pdf_page == last
    assert [s.name for s in win.shapes] == ["ghost"]


def test_opening_another_image_leaves_no_ghost_shape_items(loaded_win, qtbot,
                                                           fixtures_dir):
    """The new document's canvas must not keep the old document's graphics.

    ``_set_canvas_image()`` ends in ``canvas.set_image()``, which redraws
    ``self.shapes`` -- so the model had to be cleared BEFORE the swap, not after.
    """
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    assert win.canvas._shape_items

    load_media(win, qtbot, fixtures_dir / "blank_100x100.png")

    assert win.shapes == []
    assert win.canvas._shape_items == {}
    assert win.selected_sids == []
    assert win.areas_list.count() == 0


def test_load_json_clears_a_stale_multi_selection(loaded_win, qtbot, tmp_path,
                                                  no_file_dialogs):
    """Ctrl+click multi-selection belongs to the replaced document."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 100, 500, 220))
    key(qtbot, win, Qt.Key.Key_V)
    click(qtbot, win, 200, 150)
    click(qtbot, win, 450, 160, CTRL)     # ctrl+click = add to the selection
    assert len(win.selected_sids) == 2

    out = tmp_path / "sel.json"
    no_file_dialogs.save_paths = [str(out)]
    key(qtbot, win, Qt.Key.Key_S, CTRL)
    no_file_dialogs.open_paths = [str(out)]
    load_json_via_menu(win, out)
    flush_timers(qtbot)

    assert win.selected_sids == []
    assert win.selected_sid is None


def test_undo_after_save_marks_the_document_dirty_again(loaded_win, qtbot, tmp_path,
                                                        no_file_dialogs):
    """Save, undo, close -> the close must prompt: the file on disk is stale."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))

    out = tmp_path / "undone.json"
    no_file_dialogs.save_paths = [str(out)]
    key(qtbot, win, Qt.Key.Key_S, CTRL)
    assert win._has_unsaved_changes is False

    key(qtbot, win, Qt.Key.Key_Z, CTRL)
    assert win.shapes == []
    assert win._has_unsaved_changes is True
    assert win.windowTitle().startswith("*")

    key(qtbot, win, Qt.Key.Key_Y, CTRL)   # redo is just as unsaved
    assert len(win.shapes) == 1
    assert win._has_unsaved_changes is True


def test_pdf_page_clamp_keeps_shape_ids_unique(win, qtbot, tmp_path, fixtures_dir,
                                               no_file_dialogs,
                                               auto_answer_messagebox):
    """Two shapes with the same id on two out-of-range pages both land on the
    last page -- they must not end up sharing a (page, sid) identity there."""
    pdf = fixtures_dir / "sample_form.pdf"
    project = tmp_path / "clash.json"
    project.write_text(json.dumps({
        "schema_version": 1,
        "image": {"path": str(pdf), "width": 800, "height": 600},
        "shapes": [
            {"id": 1, "name": "from_98", "kind": "rect", "bbox": [10, 10, 60, 60], "page": 98},
            {"id": 1, "name": "from_99", "kind": "rect", "bbox": [20, 20, 80, 80], "page": 99},
        ],
    }), encoding="utf-8")

    no_file_dialogs.open_paths = [str(project)]
    load_json_via_menu(win, project)
    flush_timers(qtbot)

    last = SAMPLE_PDF_PAGES - 1
    landed = win._pdf_page_states[last].shapes
    assert [s.name for s in landed] == ["from_98", "from_99"]
    assert len({(s.page, s.sid) for s in landed}) == 2


# ---------------------------------------------------------------------------
# Dirty-transition prompt: draw, then File > Open / Load JSON
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer,saved,replaced", [
    (QMessageBox.StandardButton.Yes, True, True),
    (QMessageBox.StandardButton.No, False, True),
    (QMessageBox.StandardButton.Cancel, False, False),
])
def test_open_over_dirty_document_prompts(loaded_win, qtbot, tmp_path, fixtures_dir,
                                          no_file_dialogs, auto_answer_messagebox,
                                          answer, saved, replaced):
    """Ctrl+O over unsaved work asks first -- it used to discard silently."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    assert win._has_unsaved_changes is True

    out = tmp_path / "onopen.json"
    no_file_dialogs.save_paths = [str(out)]
    no_file_dialogs.open_paths = [str(fixtures_dir / "blank_100x100.png")]
    auto_answer_messagebox.answer = answer

    key(qtbot, win, Qt.Key.Key_O, CTRL)
    flush_timers(qtbot)

    assert [t for t, _ in auto_answer_messagebox.questions] == ["Unsaved Changes"]
    assert out.is_file() is saved
    if replaced:
        assert win.canvas.image_size == (100, 100)
        assert win.shapes == []
        assert no_file_dialogs.open_calls == [("open", "Open Image / PDF")]
    else:
        # Cancel: no file picker was even opened, the work is still here.
        assert no_file_dialogs.open_calls == []
        assert win.canvas.image_size == FORM_SIZE
        assert len(win.shapes) == 1
        assert win._has_unsaved_changes is True


def test_load_json_over_dirty_document_prompts_and_cancel_aborts(
        loaded_win, qtbot, tmp_path, no_file_dialogs, auto_answer_messagebox):
    """Same guard on File > Load JSON."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    other = tmp_path / "other.json"
    other.write_text(json.dumps({
        "schema_version": 1,
        "image": {"path": None, "width": 800, "height": 600},
        "shapes": [{"id": 9, "name": "imported", "kind": "rect",
                    "bbox": [1, 2, 3, 4], "page": 0}],
    }), encoding="utf-8")

    auto_answer_messagebox.answer = QMessageBox.StandardButton.Cancel
    no_file_dialogs.open_paths = [str(other)]
    load_json_via_menu(win, other)

    assert [t for t, _ in auto_answer_messagebox.questions] == ["Unsaved Changes"]
    assert no_file_dialogs.open_calls == []          # aborted before the picker
    assert [s.name for s in win.shapes] == ["area_1"]

    # Answering No goes through and replaces the shapes.
    auto_answer_messagebox.answer = QMessageBox.StandardButton.No
    no_file_dialogs.open_paths = [str(other)]
    load_json_via_menu(win, other)
    assert [s.name for s in win.shapes] == ["imported"]


# ---------------------------------------------------------------------------
# Corrupt / hostile project JSON
# ---------------------------------------------------------------------------

def test_load_json_aborts_when_the_referenced_image_cannot_be_decoded(
        loaded_win, qtbot, tmp_path, no_file_dialogs, auto_answer_messagebox):
    """The referenced image EXISTS but is not decodable: the shapes must not be
    installed over the previously loaded image (that mixes two projects and
    Save would then write project B's shapes with project A's path/dims)."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    kept_path, kept_size = win.image_path, win.canvas.image_size

    broken = tmp_path / "truncated.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n not really a png")
    project = tmp_path / "refs_broken.json"
    project.write_text(json.dumps({
        "schema_version": 1,
        "image": {"path": str(broken), "width": 800, "height": 600},
        "shapes": [{"id": 5, "name": "other_project", "kind": "rect",
                    "bbox": [0, 0, 10, 10], "page": 0}],
    }), encoding="utf-8")

    auto_answer_messagebox.answer = QMessageBox.StandardButton.No
    no_file_dialogs.open_paths = [str(project)]
    load_json_via_menu(win, project)

    assert levels(auto_answer_messagebox) == ["critical", "warning"]
    assert [s.name for s in win.shapes] == ["area_1"]    # not replaced
    assert win.image_path == kept_path
    assert win.canvas.image_size == kept_size


def test_load_json_with_non_string_image_path_does_not_crash(
        loaded_win, qtbot, tmp_path, no_file_dialogs, auto_answer_messagebox):
    """``os.path.exists(1)`` is True (it reads an int as a file descriptor), so
    an int path used to reach the image loader."""
    win = loaded_win
    project = tmp_path / "int_path.json"
    project.write_text(json.dumps({
        "schema_version": 1,
        "image": {"path": 1, "width": 800, "height": 600},
        "shapes": [{"id": 1, "name": "imported", "kind": "rect",
                    "bbox": [10, 10, 60, 60], "page": 0}],
    }), encoding="utf-8")

    auto_answer_messagebox.answer = QMessageBox.StandardButton.No
    no_file_dialogs.open_paths = [str(project)]
    load_json_via_menu(win, project)

    # Treated as "no image reference": the open document stays, shapes load.
    assert levels(auto_answer_messagebox) == []
    assert [s.name for s in win.shapes] == ["imported"]
    assert win.image_path.endswith("form_800x600.png")
    assert win.isVisible() is True


def test_load_json_unexpected_failure_is_reported_not_fatal(
        loaded_win, qtbot, tmp_path, monkeypatch, no_file_dialogs,
        auto_answer_messagebox):
    """Anything unforeseen inside the loader becomes a dialog. Unhandled, it
    would escape into the Qt slot and PyQt6 would abort the process."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))

    project = tmp_path / "fine.json"
    project.write_text(json.dumps({
        "schema_version": 1, "image": {"path": None, "width": 800, "height": 600},
        "shapes": [],
    }), encoding="utf-8")

    def boom(_raw):
        raise RuntimeError("simulated loader defect")

    monkeypatch.setattr(areaDef, "_parse_shapes", boom)
    auto_answer_messagebox.answer = QMessageBox.StandardButton.No
    no_file_dialogs.open_paths = [str(project)]
    load_json_via_menu(win, project)

    assert levels(auto_answer_messagebox) == ["critical"]
    assert "simulated loader defect" in auto_answer_messagebox.messages[0][2]
    assert [s.name for s in win.shapes] == ["area_1"]
    assert win.isVisible() is True


# ---------------------------------------------------------------------------
# PDF page flip on an unrenderable page
# ---------------------------------------------------------------------------

def test_pdf_page_render_failure_stays_on_the_current_page(win, qtbot, fixtures_dir,
                                                           monkeypatch,
                                                           auto_answer_messagebox):
    """Only page 0 is probe-rendered at open time; a page that fails later must
    leave the app fully on the page it is showing."""
    load_media(win, qtbot, fixtures_dir / "sample_form.pdf")
    draw(qtbot, win, (100, 100, 300, 200))
    before = [s.bbox() for s in win.shapes]
    before_title = win.windowTitle()

    def boom(index, doc=None):
        raise RuntimeError(f"corrupt content stream on page {index}")

    monkeypatch.setattr(win, "_render_pdf_page", boom)
    qtbot.mouseClick(win.pdf_next_btn, Qt.MouseButton.LeftButton)

    assert levels(auto_answer_messagebox) == ["critical"]
    assert "page 2" in auto_answer_messagebox.messages[0][2]
    assert win._current_pdf_page == 0
    assert [s.bbox() for s in win.shapes] == before
    assert win.windowTitle() == before_title
    assert win._pdf_page_states == {}      # page 0's state was not stashed away
    assert win.isVisible() is True


# ---------------------------------------------------------------------------
# Last-resort excepthook (GUI entry path only)
# ---------------------------------------------------------------------------

def test_gui_excepthook_reports_instead_of_aborting(monkeypatch, auto_answer_messagebox):
    """PyQt6 calls qFatal() for a slot exception only while sys.excepthook is
    the interpreter default; the GUI entry path replaces it."""
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    areaDef._install_gui_excepthook()
    assert sys.excepthook is areaDef._gui_excepthook

    sys.excepthook(ValueError, ValueError("boom"), None)   # must return normally

    assert levels(auto_answer_messagebox) == ["critical"]
    assert titles(auto_answer_messagebox) == ["Unexpected Error"]
    assert "boom" in auto_answer_messagebox.messages[0][2]
