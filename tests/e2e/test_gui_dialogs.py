"""GUI dialog tests: menu actions and the Qt static dialogs they open.

Dialog statics (``QFileDialog.getOpenFileName`` / ``getSaveFileName``,
``QMessageBox.question``, ``QInputDialog.getText``) are replaced by the scripted
stubs from ``tests/conftest.py``; everything else is real. Menu entries are
triggered by their keyboard shortcut where the app defines one (Ctrl+O, Ctrl+S)
and by ``QAction.trigger()`` where it does not (Export CSV, Exit) -- never by
clicking menu geometry, which does not paint offscreen.

Assertions are on the artefacts the app actually produced (the JSON/CSV on disk,
the window's state), not on its status-bar text.
"""

from __future__ import annotations

import csv
import json

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QMessageBox

import areaDef
from areaDef import Project
from helpers.gui import (
    CTRL,
    assert_bbox,
    draw,
    key,
    load_media,
    menu_action,
    show_window,
)
from helpers.images import FORM_SIZE

pytestmark = [pytest.mark.e2e, pytest.mark.gui]


@pytest.fixture
def win(annotator_window, qtbot):
    """A shown, activated window with nothing loaded yet."""
    return show_window(annotator_window, qtbot)


@pytest.fixture
def loaded_win(win, qtbot, fixtures_dir):
    """...and with the 800x600 form open, ready to draw on."""
    load_media(win, qtbot, fixtures_dir / "form_800x600.png")
    return win


def rename_by_double_click(qtbot, win, row=0):
    """Double-click a row of the Areas list -> ``itemDoubleClicked`` -> the
    rename dialog. Offscreen the double-click only registers on an already
    pressed-in list, so this sends a single click first (a real user's first
    press does the same)."""
    viewport = win.areas_list.viewport()
    center = win.areas_list.visualItemRect(win.areas_list.item(row)).center()
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    qtbot.mouseDClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)


# ---------------------------------------------------------------------------
# D1 -- File > Open
# ---------------------------------------------------------------------------

def test_d1_open_loads_fixture_png(win, qtbot, fixtures_dir, no_file_dialogs):
    png = fixtures_dir / "form_800x600.png"
    action = menu_action(win, "File", "Open Image / PDF")
    assert action.shortcut() == QKeySequence(QKeySequence.StandardKey.Open)

    no_file_dialogs.open_paths = [str(png)]
    key(qtbot, win, Qt.Key.Key_O, CTRL)

    assert no_file_dialogs.open_calls == [("open", "Open Image / PDF")]
    assert win.image_path == str(png.resolve())
    assert win.canvas.image_size == FORM_SIZE
    assert win.canvas.pixmap_item is not None
    assert win._is_pdf is False
    assert win._has_unsaved_changes is False

    # A cancelled dialog (empty queue -> "") must leave the document alone.
    key(qtbot, win, Qt.Key.Key_O, CTRL)
    assert len(no_file_dialogs.open_calls) == 2
    assert win.image_path == str(png.resolve())


# ---------------------------------------------------------------------------
# D2 -- File > Save
# ---------------------------------------------------------------------------

def test_d2_save_writes_loadable_project_json(loaded_win, qtbot, tmp_path,
                                              no_file_dialogs, fixtures_dir):
    win = loaded_win
    action = menu_action(win, "File", "Save JSON")
    assert action.shortcut() == QKeySequence(QKeySequence.StandardKey.Save)

    draw(qtbot, win, (100, 100, 300, 200))
    assert win._has_unsaved_changes is True

    out = tmp_path / "project.json"
    no_file_dialogs.save_paths = [str(out)]
    key(qtbot, win, Qt.Key.Key_S, CTRL)

    assert no_file_dialogs.save_calls == [("save", "Save JSON")]
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["image"]["path"] == str((fixtures_dir / "form_800x600.png").resolve())
    assert (payload["image"]["width"], payload["image"]["height"]) == FORM_SIZE
    assert len(payload["shapes"]) == 1

    # Loadable: the headless project reader accepts it and agrees on geometry.
    project = Project.from_json(str(out))
    assert (project.width, project.height) == FORM_SIZE
    assert len(project.shapes) == 1
    assert project.shapes[0].kind == "rect"
    assert project.shapes[0].name == "area_1"
    assert_bbox(project.shapes[0].bbox(), (100, 100, 300, 200))

    assert win._has_unsaved_changes is False
    assert not win.windowTitle().startswith("*")


# ---------------------------------------------------------------------------
# D3 -- rename via QInputDialog (accept + cancel)
# ---------------------------------------------------------------------------

def test_d3_rename_accept_and_cancel(loaded_win, qtbot, scripted_input_dialog):
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    shape = win.shapes[0]
    assert shape.name == "area_1"

    # Accept.
    scripted_input_dialog.texts = ["invoice_no"]
    rename_by_double_click(qtbot, win)
    assert scripted_input_dialog.calls == [("Rename Shape", "Enter new name:")]
    assert shape.name == "invoice_no"
    assert win.areas_list.item(0).text().endswith("invoice_no")
    assert win.name_edit.text() == "invoice_no"

    # Cancel: an exhausted queue is the stub's "user pressed Cancel" (ok=False).
    rename_by_double_click(qtbot, win)
    assert len(scripted_input_dialog.calls) == 2
    assert shape.name == "invoice_no"
    assert win.areas_list.item(0).text().endswith("invoice_no")


# ---------------------------------------------------------------------------
# D4 -- unsaved-changes prompt on close (Yes / No / Cancel)
# ---------------------------------------------------------------------------
# areaDef.py:4604 offers Yes / No / Cancel -- there is no Discard button.
# Yes = save (through the save dialog) and close, No = close and lose the edits,
# Cancel = stay open.

@pytest.mark.parametrize("answer,saved,closed", [
    (QMessageBox.StandardButton.Yes, True, True),
    (QMessageBox.StandardButton.No, False, True),
    (QMessageBox.StandardButton.Cancel, False, False),
])
def test_d4_unsaved_changes_prompt(loaded_win, qtbot, tmp_path, no_file_dialogs,
                                   auto_answer_messagebox, answer, saved, closed):
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    assert win._has_unsaved_changes is True

    out = tmp_path / "onclose.json"
    no_file_dialogs.save_paths = [str(out)]
    auto_answer_messagebox.answer = answer

    # File > Exit is the closest event-level seam: it has no shortcut, so the
    # QAction is triggered directly (the brief's sanctioned route).
    menu_action(win, "File", "Exit").trigger()

    assert [title for title, _ in auto_answer_messagebox.questions] == ["Unsaved Changes"]
    assert out.is_file() is saved
    assert win.isVisible() is not closed
    assert win._has_unsaved_changes is not saved

    if saved:
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert len(payload["shapes"]) == 1
        assert no_file_dialogs.save_calls == [("save", "Save JSON")]
    else:
        assert no_file_dialogs.save_calls == []
    if not closed:
        # Cancel keeps the document alive and editable.
        assert win.shapes and win.canvas.pixmap_item is not None


@pytest.mark.parametrize("label", ["cancelled save dialog", "failed write"])
def test_d4_yes_without_a_written_file_keeps_the_window_open(
        loaded_win, qtbot, tmp_path, no_file_dialogs, auto_answer_messagebox, label):
    """Answering "Yes, save" must not close the window unless a file was really
    written -- cancelling the save picker (or a write that fails) used to close
    it anyway and take the annotations with it."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    assert win._has_unsaved_changes is True

    if label == "cancelled save dialog":
        no_file_dialogs.save_paths = []       # empty queue == user pressed Cancel
    else:
        # Parent is an existing FILE, so the atomic write raises (it creates
        # missing directories, so a merely absent one would have succeeded).
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        no_file_dialogs.save_paths = [str(blocker / "onclose.json")]
    auto_answer_messagebox.answer = QMessageBox.StandardButton.Yes

    menu_action(win, "File", "Exit").trigger()

    assert no_file_dialogs.save_calls == [("save", "Save JSON")], label
    assert win.isVisible() is True, label            # still open
    assert win._has_unsaved_changes is True, label   # still dirty
    assert len(win.shapes) == 1, label               # work intact
    assert win.canvas.pixmap_item is not None, label
    if label == "failed write":
        assert [lvl for lvl, _, _ in auto_answer_messagebox.messages] == ["critical"]


def test_d4_clean_document_closes_without_prompting(loaded_win, qtbot,
                                                    auto_answer_messagebox):
    """No unsaved changes -> no prompt at all."""
    win = loaded_win
    menu_action(win, "File", "Exit").trigger()
    assert auto_answer_messagebox.questions == []
    assert win.isVisible() is False


# ---------------------------------------------------------------------------
# D5 -- Export CSV
# ---------------------------------------------------------------------------

def test_d5_export_csv(loaded_win, qtbot, tmp_path, no_file_dialogs):
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 100, 500, 220), tool_key=Qt.Key.Key_C)

    out = tmp_path / "areas.csv"
    no_file_dialogs.save_paths = [str(out)]
    menu_action(win, "File", "Export CSV").trigger()

    assert no_file_dialogs.save_calls == [("save", "Export CSV")]
    assert out.is_file()
    with out.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    assert rows[0] == ["id", "name", "kind", "page", "x1", "y1", "x2", "y2",
                       "width", "height", "cx", "cy", "r", "chamfer", "rects"]
    assert len(rows) == 3
    rect, circle = rows[1], rows[2]
    assert rect[1:4] == ["area_1", "rect", "0"]
    assert_bbox([float(v) for v in rect[4:8]], (100, 100, 300, 200))
    assert (float(rect[8]), float(rect[9])) == pytest.approx((200.0, 100.0), abs=2.0)
    assert rect[10:13] == ["", "", ""]

    assert circle[1:4] == ["area_2", "circle", "0"]
    assert_bbox([float(v) for v in circle[4:8]], (400, 100, 520, 220))
    assert (float(circle[10]), float(circle[11]), float(circle[12])) == pytest.approx(
        (460.0, 160.0, 60.0), abs=2.0
    )


# ---------------------------------------------------------------------------
# D7 -- the save pickers apply their extension before the overwrite check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caption,suffix,trigger", [
    ("Save JSON", "json", lambda win, qtbot: key(qtbot, win, Qt.Key.Key_S, CTRL)),
    ("Export CSV", "csv",
     lambda win, qtbot: menu_action(win, "File", "Export CSV").trigger()),
    ("Save Screenshot", "png",
     lambda win, qtbot: menu_action(win, "File", "Save Screenshot…").trigger()),
])
def test_d7_save_dialogs_set_a_default_suffix(loaded_win, qtbot, tmp_path,
                                              no_file_dialogs, caption, suffix,
                                              trigger):
    """Qt's own overwrite prompt only fires for the name the dialog holds, so
    the extension has to be set on the dialog -- not bolted on afterwards."""
    win = loaded_win
    draw(qtbot, win, (100, 100, 300, 200))

    typed = tmp_path / "typed_without_extension"      # what a user types
    no_file_dialogs.save_paths = [str(typed)]
    trigger(win, qtbot)

    assert no_file_dialogs.save_calls == [("save", caption)]
    assert no_file_dialogs.save_suffixes == [suffix]
    assert typed.with_suffix("." + suffix).is_file()
    assert not typed.exists()


# ---------------------------------------------------------------------------
# D8 -- View > Theme
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("index,mode", list(enumerate(("auto", "light", "dark"))))
def test_d8_theme_menu_ticks_the_chosen_entry(win, index, mode):
    """Picking a theme leaves exactly that entry ticked.

    Regression: "Auto" resolves to light or dark, and the menu used to be
    re-ticked from the RESOLVED theme -- so choosing Auto unchecked itself and
    ticked Light or Dark instead.
    """
    win.theme_actions[index].trigger()

    assert win.theme_mode == mode
    assert [a.isChecked() for a in win.theme_actions] == [i == index for i in range(3)]
    assert win.current_theme in ("light", "dark")
    if mode != "auto":
        assert win.current_theme == mode
    assert win.styleSheet() == (areaDef.DARK_STYLE if win.current_theme == "dark"
                                else areaDef.LIGHT_STYLE)


def test_d8_toggle_theme_stops_following_the_system(win, qtbot):
    """T after Auto is an explicit choice: the menu follows it off Auto."""
    win.theme_actions[0].trigger()
    assert win.theme_mode == "auto"

    key(qtbot, win, Qt.Key.Key_T)

    assert win.theme_mode == win.current_theme
    assert win.theme_actions[0].isChecked() is False
    assert sum(a.isChecked() for a in win.theme_actions) == 1


# ---------------------------------------------------------------------------
# D9 -- every guarded action on an empty window: one warning, no crash
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,trigger", [
    ("L (detect lines)", lambda w, b: key(b, w, Qt.Key.Key_L)),
    ("B (detect checkboxes)", lambda w, b: key(b, w, Qt.Key.Key_B)),
    ("X (detect text)", lambda w, b: key(b, w, Qt.Key.Key_X)),
    ("G (detect high contrast)", lambda w, b: key(b, w, Qt.Key.Key_G)),
    ("M (detect markers)", lambda w, b: key(b, w, Qt.Key.Key_M)),
    ("K (detect barcodes)", lambda w, b: key(b, w, Qt.Key.Key_K)),
    ("N (normalize)", lambda w, b: key(b, w, Qt.Key.Key_N)),
    ("Ctrl+S (save JSON)", lambda w, b: key(b, w, Qt.Key.Key_S, CTRL)),
    ("Export CSV", lambda w, b: menu_action(w, "File", "Export CSV").trigger()),
    ("Save Screenshot", lambda w, b: menu_action(w, "File", "Save Screenshot…").trigger()),
])
def test_d9_guarded_actions_on_an_empty_window(win, qtbot, auto_answer_messagebox,
                                               no_file_dialogs, label, trigger):
    """No document open: each action says so once and changes nothing. None of
    them may crash, open a file picker, or create a shape."""
    trigger(win, qtbot)

    assert len(auto_answer_messagebox.messages) == 1, (label, auto_answer_messagebox.messages)
    assert auto_answer_messagebox.messages[0][0] in ("warning", "information"), label
    assert no_file_dialogs.calls == [], label
    assert win.shapes == []
    assert win.isVisible() is True
