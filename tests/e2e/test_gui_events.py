"""GUI event tests: the canvas driven by real synthesized mouse and key input.

Every interaction under test enters through the widget's own event handler --
``qtbot.mousePress``/``mouseMove``/``mouseRelease``/``keyClick``/``keyClicks`` on
``win.canvas.viewport()`` (or a side-panel widget). No test calls the handler it
is exercising. Assertions are on the model (``win.shapes``, the selection, the
list widget), never on pixels.

Coordinates: tests speak scene (image-pixel) coordinates; ``helpers.gui`` maps
them with ``canvas.mapFromScene`` after pinning the view at 1:1 (see
``settle_view`` for why the deferred ``fit_to_view`` has to be flushed first).

Snapping is deliberately left at its default (on): no detector is ever run here,
so ``_snap_bbox`` short-circuits on an empty line/text cache and every drawn box
is exactly where the drag put it.
"""

from __future__ import annotations

import threading

import pytest
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QApplication

import areaDef
from helpers.gui import (
    BBOX_TOL,
    CTRL,
    SHIFT,
    assert_bbox,
    canvas_point,
    click,
    drag,
    draw,
    key,
    load_media,
    right_click,
    settle_view,
    show_window,
)
from helpers.images import FORM_H_RULES, FORM_SIZE, FORM_V_RULES
from helpers.pdfs import SAMPLE_PDF_PAGES

pytestmark = [pytest.mark.e2e, pytest.mark.gui]

#: The app binds Undo/Redo to QKeySequence.StandardKey, so the redo chord is the
#: platform's -- Ctrl+Y on X11, *not* the Ctrl+Shift+Z the task brief assumed.
#: G5 pins this so a platform change fails loudly instead of silently skipping.
REDO_CHORD = (Qt.Key.Key_Y, CTRL)
UNDO_CHORD = (Qt.Key.Key_Z, CTRL)


@pytest.fixture
def win(annotator_window, qtbot, fixtures_dir):
    """A shown, activated window with the 800x600 form loaded and the view
    pinned at 1:1. Opening the file is setup here (``load_image``); the
    dialog-driven open is covered by G8 and test_gui_dialogs.py::D1."""
    window = show_window(annotator_window, qtbot)
    load_media(window, qtbot, fixtures_dir / "form_800x600.png")
    assert window.canvas.image_size == FORM_SIZE
    return window


def shape_state(win):
    """The model state undo/redo must restore exactly."""
    return [(s.sid, s.kind, s.name, s.bbox(), s.page) for s in win.shapes]


# ---------------------------------------------------------------------------
# G1 -- draw a rectangle with a mouse drag
# ---------------------------------------------------------------------------

def test_g1_draw_rect_with_mouse_drag(win, qtbot):
    # Pick the tool the way a user does: click its radio button.
    qtbot.mouseClick(win.rect_radio, Qt.MouseButton.LeftButton)
    assert win.current_tool == "rect"

    drag(qtbot, win, (100, 100), (300, 200))

    assert len(win.shapes) == 1
    shape = win.shapes[0]
    assert shape.kind == "rect"
    assert shape.name == "area_1"
    assert shape.page == 0
    assert_bbox(shape.bbox(), (100, 100, 300, 200))
    # The draw also selects the new shape and shows it in the side panel.
    assert win.selected_sid == shape.sid
    assert win.areas_list.count() == 1
    assert win.areas_list.item(0).text().endswith("area_1")
    assert win._has_unsaved_changes is True


def test_g1_drag_below_minimum_size_creates_nothing(win, qtbot):
    """MIN_SHAPE_SIZE_PX (6 px) rejects a stray click-drag."""
    qtbot.mouseClick(win.rect_radio, Qt.MouseButton.LeftButton)
    drag(qtbot, win, (200, 200), (203, 203))
    assert win.shapes == []


# ---------------------------------------------------------------------------
# G2 -- circle and chamfer rect via the tool shortcuts
# ---------------------------------------------------------------------------

def test_g2_draw_circle_and_chamfer_via_tool_shortcuts(win, qtbot):
    key(qtbot, win, Qt.Key.Key_C)
    assert win.current_tool == "circle"
    assert win.circle_radio.isChecked()
    # A circle is constrained square to max(|dx|, |dy|) -> 120 wide, not 100.
    drag(qtbot, win, (400, 100), (500, 220))

    key(qtbot, win, Qt.Key.Key_H)
    assert win.current_tool == "chamfer"
    assert win.chamfer_radio.isChecked()
    drag(qtbot, win, (100, 300), (260, 400))

    assert [s.kind for s in win.shapes] == ["circle", "chamfer"]
    circle, chamfer = win.shapes
    assert_bbox(circle.bbox(), (400, 100, 520, 220))
    x1, y1, x2, y2 = circle.bbox()
    assert abs((x2 - x1) - (y2 - y1)) <= BBOX_TOL, "circle must stay square"
    assert_bbox(chamfer.bbox(), (100, 300, 260, 400))
    assert chamfer.chamfer == float(win.chamfer_spin.value()) == 20.0

    # Back to the select tool by shortcut.
    key(qtbot, win, Qt.Key.Key_V)
    assert win.current_tool == "select"


# ---------------------------------------------------------------------------
# G3 -- selection: click, Ctrl+click, marquee, click-empty
# ---------------------------------------------------------------------------

def test_g3_selection_click_ctrl_click_marquee_and_clear(win, qtbot):
    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 100, 600, 200))
    draw(qtbot, win, (100, 300, 300, 400))
    top_left, top_right, bottom_left = (s.sid for s in win.shapes)
    key(qtbot, win, Qt.Key.Key_V)

    click(qtbot, win, 200, 150)
    assert win.selected_sid == top_left
    assert win.selected_sids == [top_left]

    # Ctrl+click adds; the newest click becomes the key object.
    click(qtbot, win, 500, 150, modifier=CTRL)
    assert win.selected_sids == [top_left, top_right]
    assert win.selected_sid == top_right

    # Ctrl+click again removes it.
    click(qtbot, win, 500, 150, modifier=CTRL)
    assert win.selected_sids == [top_left]
    assert win.selected_sid == top_left

    # Shift+drag on empty canvas marquee-selects everything it touches.
    click(qtbot, win, 700, 500)
    assert win.selected_sids == []
    drag(qtbot, win, (60, 60), (700, 250), modifier=SHIFT)
    assert win.selected_sids == [top_left, top_right]
    assert bottom_left not in win.selected_sids

    # A plain click on empty canvas clears the selection.
    click(qtbot, win, 700, 500)
    assert win.selected_sid is None
    assert win.selected_sids == []


# ---------------------------------------------------------------------------
# G4 -- drag-move and handle-resize
# ---------------------------------------------------------------------------

def test_g4_drag_move_and_resize_by_handle(win, qtbot):
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    shape = win.shapes[0]
    click(qtbot, win, 200, 150)
    assert win.selected_sid == shape.sid
    start = shape.bbox()

    # Move: grab the interior and drag by (+50, +30).
    drag(qtbot, win, (200, 150), (250, 180))
    moved = shape.bbox()
    assert_bbox(moved, tuple(v + d for v, d in zip(start, (50, 30, 50, 30))))

    # Resize: grab the SE handle (drawn at the bbox corner, 8 px grab radius at
    # 1:1) and drag it out by (+40, +20). Only x2/y2 may move.
    drag(qtbot, win, (moved[2], moved[3]), (moved[2] + 40, moved[3] + 20))
    resized = shape.bbox()
    assert_bbox(resized, (moved[0], moved[1], moved[2] + 40, moved[3] + 20))
    assert len(win.shapes) == 1


# ---------------------------------------------------------------------------
# G5 -- undo / redo through real keystrokes
# ---------------------------------------------------------------------------

def test_g5_undo_redo_via_keystrokes(win, qtbot):
    assert QKeySequence(QKeySequence.StandardKey.Redo).toString() == "Ctrl+Y"
    assert QKeySequence(QKeySequence.StandardKey.Undo).toString() == "Ctrl+Z"

    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 100, 600, 200))
    both = shape_state(win)
    assert len(both) == 2
    one = [both[0]]

    key(qtbot, win, *UNDO_CHORD)
    assert shape_state(win) == one
    key(qtbot, win, *UNDO_CHORD)
    assert shape_state(win) == []
    # Undo past the beginning is a no-op, not a crash.
    key(qtbot, win, *UNDO_CHORD)
    assert shape_state(win) == []

    key(qtbot, win, *REDO_CHORD)
    assert shape_state(win) == one
    key(qtbot, win, *REDO_CHORD)
    assert shape_state(win) == both
    assert win.areas_list.count() == 2


# ---------------------------------------------------------------------------
# G6 -- arrow-key nudge and Delete
# ---------------------------------------------------------------------------

def test_g6_arrow_nudge_and_delete(win, qtbot):
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    shape = win.shapes[0]
    assert win.selected_sid == shape.sid
    base = shape.bbox()

    key(qtbot, win, Qt.Key.Key_Right)
    assert shape.bbox() == tuple(v + d for v, d in zip(base, (1, 0, 1, 0)))
    key(qtbot, win, Qt.Key.Key_Up)
    assert shape.bbox() == tuple(v + d for v, d in zip(base, (1, -1, 1, -1)))

    key(qtbot, win, Qt.Key.Key_Down, SHIFT)
    assert shape.bbox() == tuple(v + d for v, d in zip(base, (1, 9, 1, 9)))
    key(qtbot, win, Qt.Key.Key_Left, SHIFT)
    assert shape.bbox() == tuple(v + d for v, d in zip(base, (-9, 9, -9, 9)))

    key(qtbot, win, Qt.Key.Key_Delete)
    assert win.shapes == []
    assert win.selected_sid is None
    assert win.selected_sids == []
    assert win.areas_list.count() == 0


def test_g6_nudge_burst_is_a_single_undo_step(win, qtbot):
    """Auto-repeat sends a nudge per key repeat. They coalesce into ONE history
    entry, so a single Ctrl+Z puts the shape back where the burst started."""
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    sid = win.shapes[0].sid
    base = win.shapes[0].bbox()

    for _ in range(8):                       # a held arrow key, in one burst
        key(qtbot, win, Qt.Key.Key_Right)
    assert win.shapes[0].bbox() == tuple(v + d for v, d in zip(base, (8, 0, 8, 0)))
    assert len(win.undo_manager.undo_stack) == 2, "one entry for the draw, one for the burst"

    key(qtbot, win, *UNDO_CHORD)
    assert win.get_shape(sid).bbox() == base

    # A burst is closed by quiet: nudging again after the coalesce window is a
    # separate history entry, not an extension of the first.
    key(qtbot, win, *REDO_CHORD)
    qtbot.wait(int(areaDef.NUDGE_COALESCE_MS * 1.5))
    key(qtbot, win, Qt.Key.Key_Right)
    key(qtbot, win, *UNDO_CHORD)
    assert win.get_shape(sid).bbox() == tuple(v + d for v, d in zip(base, (8, 0, 8, 0)))


def test_g6_page_change_ends_a_nudge_burst(annotator_window, qtbot, fixtures_dir):
    """A burst must not span a page change: the shape it was moving is not even
    on screen any more, so the next nudge has to start its own undo step."""
    win = show_window(annotator_window, qtbot)
    load_media(win, qtbot, fixtures_dir / "sample_form.pdf")
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    key(qtbot, win, Qt.Key.Key_Right)
    assert win._nudge_timer.isActive(), "a nudge opens a burst"

    key(qtbot, win, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    assert win._current_pdf_page == 1
    assert not win._nudge_timer.isActive(), "the page change must close the burst"
    settle_view(win, qtbot)

    key(qtbot, win, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
    settle_view(win, qtbot)
    win.set_selected(win.shapes[0].sid)
    depth = len(win.undo_manager.undo_stack)
    key(qtbot, win, Qt.Key.Key_Right)
    assert len(win.undo_manager.undo_stack) == depth + 1
    assert win._has_unsaved_changes is True


# ---------------------------------------------------------------------------
# G7 -- side-panel name edit
# ---------------------------------------------------------------------------

def test_g7_side_panel_name_edit(win, qtbot):
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    click(qtbot, win, 200, 150)
    shape = win.shapes[0]
    assert win.name_edit.text() == "area_1"

    qtbot.mouseClick(win.name_edit, Qt.MouseButton.LeftButton)
    qtbot.keyClick(win.name_edit, Qt.Key.Key_A, CTRL)  # select all, then retype
    qtbot.keyClicks(win.name_edit, "invoice_total")
    qtbot.keyClick(win.name_edit, Qt.Key.Key_Return)

    assert win.name_edit.text() == "invoice_total"
    assert shape.name == "invoice_total"
    assert win.areas_list.count() == 1
    assert win.areas_list.item(0).text().endswith("invoice_total")
    # 'v', 'c', 't', 'n' and 'l' are all tool/detection shortcuts: typing them in
    # the name field must not fire those (areaDef.py:_guarded).
    assert win.current_tool == "select"
    assert win.shapes == [shape]


# ---------------------------------------------------------------------------
# G8 -- PDF paging by shortcut (Alt+Left / Alt+Right)
# ---------------------------------------------------------------------------

def test_g8_pdf_paging_by_shortcut(annotator_window, qtbot, fixtures_dir,
                                   no_file_dialogs):
    win = show_window(annotator_window, qtbot)
    # Open through the real File > Open path (Ctrl+O) with a scripted dialog --
    # the same seam D1 uses.
    no_file_dialogs.open_paths = [str(fixtures_dir / "sample_form.pdf")]
    key(qtbot, win, Qt.Key.Key_O, CTRL)
    assert win._is_pdf is True
    assert win._pdf_page_count == SAMPLE_PDF_PAGES
    assert win._current_pdf_page == 0
    settle_view(win, qtbot)

    draw(qtbot, win, (100, 100, 300, 200))
    page0 = shape_state(win)
    assert len(page0) == 1 and page0[0][4] == 0

    key(qtbot, win, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    assert win._current_pdf_page == 1
    assert win.shapes == []
    assert win.areas_list.count() == 0
    settle_view(win, qtbot)

    draw(qtbot, win, (100, 300, 300, 400))
    page1 = shape_state(win)
    assert len(page1) == 1 and page1[0][4] == 1

    key(qtbot, win, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
    assert win._current_pdf_page == 0
    assert shape_state(win) == page0
    assert win.areas_list.count() == 1
    settle_view(win, qtbot)

    key(qtbot, win, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    assert win._current_pdf_page == 1
    assert shape_state(win) == page1

    # Paging past the last page is a no-op.
    key(qtbot, win, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    key(qtbot, win, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    assert win._current_pdf_page == SAMPLE_PDF_PAGES - 1


def test_g8_canvas_point_maps_scene_to_viewport(win):
    """The mapping the drag helpers rely on: 1:1 and centred, so a scene point
    and its viewport point differ only by the scroll offset."""
    a = canvas_point(win, 100, 100)
    b = canvas_point(win, 300, 200)
    assert (b.x() - a.x(), b.y() - a.y()) == (200, 100)


# ---------------------------------------------------------------------------
# G10 -- selection restyles the canvas in place (no scene rebuild)
# ---------------------------------------------------------------------------

def test_g10_selection_click_restyles_instead_of_rebuilding(win, qtbot):
    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 100, 600, 200))
    left, right = (s.sid for s in win.shapes)
    key(qtbot, win, Qt.Key.Key_V)

    def snapshot():
        # Re-read: a full rebuild replaces the dict itself, so holding on to it
        # would hide exactly the thing under test.
        items = win.canvas._shape_items
        return items, {sid: id(rec["item"]) for sid, rec in items.items()}

    click(qtbot, win, 200, 150)
    assert win.selected_sid == left
    items, identity = snapshot()
    assert len(items[left]["handles"]) == 4
    assert items[right]["handles"] == []

    click(qtbot, win, 500, 150)
    assert win.selected_sid == right
    items, after = snapshot()
    # Same QGraphicsItem objects: the click restyled them, it did not rebuild.
    assert after == identity
    assert items[left]["handles"] == []
    assert len(items[right]["handles"]) == 4

    # ...and the restyle really did repaint the selection (pen width 3/2).
    assert items[right]["item"].pen().width() == 3
    assert items[left]["item"].pen().width() == 2

    # A click on empty canvas clears the selection, still without a rebuild.
    click(qtbot, win, 700, 500)
    items, after = snapshot()
    assert after == identity
    assert all(rec["handles"] == [] for rec in items.values())
    assert all(rec["item"].pen().width() == 2 for rec in items.values())


def test_g10_align_button_moves_the_canvas_item(win, qtbot):
    """align_selected edits bboxes and then only re-syncs the selection, so the
    in-place path is what carries the new geometry to the canvas."""
    from PyQt6.QtWidgets import QPushButton

    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 300, 600, 400))
    key(qtbot, win, Qt.Key.Key_V)
    click(qtbot, win, 200, 150)
    click(qtbot, win, 500, 350, modifier=CTRL)      # second click = key object
    assert len(win.selected_sids) == 2

    button = next(b for b in win.findChildren(QPushButton) if b.text() == "Left")
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    for shape in win.shapes:
        x1, y1, x2, y2 = shape.bbox()
        rect = win.canvas._shape_items[shape.sid]["item"].rect()
        assert (rect.x(), rect.y(), rect.width(), rect.height()) == (x1, y1, x2 - x1, y2 - y1)
    assert win.shapes[0].bbox()[0] == win.shapes[1].bbox()[0], "left edges aligned"


# ---------------------------------------------------------------------------
# G9 -- off-thread detection (the interactive path; every other test here runs
# the synchronous one, which is what `async_detection = False` selects)
# ---------------------------------------------------------------------------

def test_g9_async_detection_runs_off_the_gui_thread_and_applies(win, qtbot, monkeypatch):
    """With `async_detection` on, pressing L runs the detector on a POOL thread
    and the result still lands in the page cache.

    The thread-ident assertion is the point: without it this test passes on the
    synchronous path too, so deleting `async_detection = True` from main() would
    go unnoticed."""
    win.async_detection = True
    assert win._line_cache == {}

    idents = []
    real = areaDef.detect_document_lines

    def spy(image, **kwargs):
        idents.append(threading.get_ident())
        return real(image, **kwargs)

    monkeypatch.setattr(areaDef, "detect_document_lines", spy)

    key(qtbot, win, Qt.Key.Key_L)
    qtbot.waitUntil(lambda: win._line_cache != {}, timeout=10000)

    assert len(idents) == 1
    assert idents[0] != threading.get_ident(), "detector ran on the GUI thread"
    h_lines, v_lines = win._current_lines()
    assert len(h_lines) == len(FORM_H_RULES)
    assert len(v_lines) == len(FORM_V_RULES)
    # The wait cursor is pushed before the worker starts; it must be popped when
    # the result is applied, or the app is left stuck on an hourglass.
    qtbot.waitUntil(lambda: not win._detection_busy, timeout=10000)
    assert QApplication.overrideCursor() is None
    assert win.show_guides is True


def test_g9_stale_result_is_dropped_when_the_document_changes(
        win, qtbot, monkeypatch, fixtures_dir):
    """Open another document while a pass is in flight: the result was computed
    from the old page and must be thrown away, not written to the new one."""
    win.async_detection = True
    gate = threading.Event()
    real = areaDef.detect_document_lines

    def gated(image, **kwargs):
        assert gate.wait(20), "test never released the worker"
        return real(image, **kwargs)

    monkeypatch.setattr(areaDef, "detect_document_lines", gated)

    key(qtbot, win, Qt.Key.Key_L)
    assert win._detection_busy is True

    load_media(win, qtbot, fixtures_dir / "checkboxes_600x400.png")
    gate.set()
    qtbot.waitUntil(lambda: not win._detection_busy, timeout=20000)

    assert win._line_cache == {}, "the previous document's rules leaked into this one"
    assert win._snap_cache == {}
    assert QApplication.overrideCursor() is None


def test_g9_detection_is_ignored_while_one_is_running(win, qtbot):
    """The re-entrancy guard: a second shortcut press during a pass is dropped
    rather than queueing a duplicate (or a second wait cursor)."""
    win._detection_busy = True
    key(qtbot, win, Qt.Key.Key_L)
    assert win._line_cache == {}
    assert QApplication.overrideCursor() is None

    win._detection_busy = False
    key(qtbot, win, Qt.Key.Key_L)
    assert win._current_lines()[0], "detection must resume once the guard clears"


# ---------------------------------------------------------------------------
# G11 -- Ctrl+D duplicate
# ---------------------------------------------------------------------------

def test_g11_ctrl_d_duplicates_the_selection(win, qtbot):
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    original = win.shapes[0]
    win._has_unsaved_changes = False

    key(qtbot, win, Qt.Key.Key_D, CTRL)

    assert len(win.shapes) == 2
    clone = win.shapes[1]
    assert clone.name == f"{original.name}_copy"
    assert clone.sid != original.sid, "the clone needs its own id"
    assert clone.kind == original.kind
    assert clone.page == original.page
    assert_bbox(clone.bbox(), tuple(v + 20 for v in original.bbox()))
    assert win.selected_sid == clone.sid, "the clone becomes the selection"
    assert win.areas_list.count() == 2
    assert win._has_unsaved_changes is True


def test_g11_duplicate_at_the_edge_stays_on_the_canvas(win, qtbot):
    """The +20 offset must not push the clone off the page: it is clamped back
    in at full size (keep-size translate, like a nudge against the edge)."""
    width, height = FORM_SIZE
    draw(qtbot, win, (600, height - 100, 700, height - 10))   # 10px off the bottom
    key(qtbot, win, Qt.Key.Key_V)
    ox1, oy1, ox2, oy2 = win.shapes[0].bbox()

    key(qtbot, win, Qt.Key.Key_D, CTRL)

    x1, y1, x2, y2 = win.shapes[1].bbox()
    assert x2 <= width and y2 <= height, "the clone hangs off the page"
    assert x1 >= 0.0 and y1 >= 0.0
    assert_bbox((x2 - x1, y2 - y1, 0, 0), (ox2 - ox1, oy2 - oy1, 0, 0))  # same size
    assert_bbox((x1, y1, x2, y2), (ox1 + 20, height - (oy2 - oy1), ox2 + 20, height))


# ---------------------------------------------------------------------------
# G13 -- Z zooms to the selection
# ---------------------------------------------------------------------------

def test_g13_z_zooms_to_the_selection(win, qtbot):
    """Z fits the view to the selected shape; with nothing selected it does
    nothing at all (and must not throw from the shortcut)."""
    draw(qtbot, win, (100, 100, 300, 200))
    key(qtbot, win, Qt.Key.Key_V)
    click(qtbot, win, 600, 500)                  # empty canvas -> deselect
    assert win.selected_sid is None

    scale_before = win.canvas.transform().m11()
    key(qtbot, win, Qt.Key.Key_Z)
    assert win.canvas.transform().m11() == scale_before, "no selection, no zoom"

    click(qtbot, win, 200, 150)                  # select the shape
    assert win.selected_sid == win.shapes[0].sid
    key(qtbot, win, Qt.Key.Key_Z)

    assert win.canvas.transform().m11() > scale_before, "Z did not zoom in"
    # The shape (plus its padding) is what the viewport now shows.
    visible = win.canvas.mapToScene(win.canvas.viewport().rect()).boundingRect()
    x1, y1, x2, y2 = win.shapes[0].bbox()
    assert visible.contains(QRectF(x1, y1, x2 - x1, y2 - y1))
    assert visible.width() < FORM_SIZE[0]
    settle_view(win, qtbot)


# ---------------------------------------------------------------------------
# G12 -- canvas context menu (right-click)
# ---------------------------------------------------------------------------

def test_g12_context_menu_delete_duplicate_and_zoom(win, qtbot, scripted_menu):
    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 300, 500, 400))
    key(qtbot, win, Qt.Key.Key_V)

    # Right-clicking a shape selects it and offers the shape actions. Dismissing
    # the menu (Escape) leaves the selection and changes nothing else.
    before = shape_state(win)
    right_click(win, 200, 150)
    assert win.selected_sid == win.shapes[0].sid, "right-click selects what it hit"
    assert scripted_menu.last == ["Rename…", "Duplicate", "",
                                  "Merge Selected", "Split", "",
                                  "Zoom to Selection", "", "Delete"]
    assert shape_state(win) == before

    scripted_menu.choice = "Duplicate"
    right_click(win, 200, 150)
    assert len(win.shapes) == 3
    assert win.shapes[2].name.endswith("_copy")
    assert win.selected_sid == win.shapes[2].sid

    # Zoom to Selection really changes the view scale.
    scale_before = win.canvas.transform().m11()
    scripted_menu.choice = "Zoom to Selection"
    right_click(win, 450, 350)
    assert win.selected_sid == win.shapes[1].sid
    assert win.canvas.transform().m11() != scale_before
    settle_view(win, qtbot)

    # Delete removes the shape the menu was opened on, and only that one.
    survivors = [s.sid for s in win.shapes if s is not win.shapes[1]]
    scripted_menu.choice = "Delete"
    right_click(win, 450, 350)
    assert [s.sid for s in win.shapes] == survivors
    assert win.selected_sid is None

    # Right-clicking empty canvas pops up nothing.
    popups = len(scripted_menu.menus)
    right_click(win, 700, 550)
    assert len(scripted_menu.menus) == popups
