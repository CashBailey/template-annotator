"""GUI e2e for irregular (merged) areas: draw -> Ctrl+M -> hit-test -> nudge ->
undo/redo -> Split, driven end to end by real synthesized input.

House rules from test_gui_events.py apply unchanged: every interaction under test
enters through the widget's own event handler (``qtbot.mouseClick``/``keyClick``
on ``win.canvas.viewport()``, or a ``QContextMenuEvent`` for the right-click), and
no test calls ``merge_selected``/``split_selected``/``_nudge_selected`` itself.
Assertions are on the model -- the multirect's ``kind``, its ``rects`` pieces and
their envelope -- never on pixels.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence

from helpers.gui import (
    CTRL,
    assert_bbox,
    click,
    draw,
    key,
    load_media,
    right_click,
    show_window,
)
from helpers.images import FORM_SIZE

pytestmark = [pytest.mark.e2e, pytest.mark.gui]

#: Undo/Redo are bound to QKeySequence.StandardKey, so the redo chord is the
#: platform's -- Ctrl+Y on X11 (pinned by test_gui_events.py::G5).
UNDO_CHORD = (Qt.Key.Key_Z, CTRL)
REDO_CHORD = (Qt.Key.Key_Y, CTRL)

#: Two overlapping boxes, and the points that tell the merged area apart from
#: its envelope: NOTCH is inside the envelope but in neither piece.
#: B is dragged bottom-right -> top-left so the *press* lands on empty canvas:
#: a press over an existing shape moves it whatever tool is active
#: (CanvasView.mousePressEvent checks for a shape hit before the tool).
RECT_A = (100.0, 100.0, 300.0, 200.0)
DRAG_B = (450.0, 320.0, 250.0, 150.0)
RECT_B = (250.0, 150.0, 450.0, 320.0)
ENVELOPE = (100.0, 100.0, 450.0, 320.0)
IN_A = (150.0, 130.0)          # piece A only
IN_B = (400.0, 280.0)          # piece B only
NOTCH = (400.0, 120.0)         # right of A, above B, inside the envelope


@pytest.fixture
def win(annotator_window, qtbot, fixtures_dir):
    """A shown, activated window with the 800x600 form loaded, view pinned 1:1."""
    window = show_window(annotator_window, qtbot)
    load_media(window, qtbot, fixtures_dir / "form_800x600.png")
    assert window.canvas.image_size == FORM_SIZE
    return window


def pieces_of(shape) -> list:
    return [tuple(piece) for piece in shape.rects]


# ---------------------------------------------------------------------------
# M1 -- the merge journey
# ---------------------------------------------------------------------------

def test_m1_merge_journey(win, qtbot, scripted_menu):
    """Draw two overlapping rects, Ctrl+click both, Ctrl+M, then live with the
    merged area: hit-test its notch, nudge it, undo/redo the merge, split it."""
    assert QKeySequence(QKeySequence.StandardKey.Redo).toString() == "Ctrl+Y"

    draw(qtbot, win, RECT_A)
    draw(qtbot, win, DRAG_B)
    key(qtbot, win, Qt.Key.Key_V)
    assert [s.kind for s in win.shapes] == ["rect", "rect"]
    before = [(s.sid, s.name, s.bbox()) for s in win.shapes]
    (sid_a, name_a, box_a), (sid_b, _name_b, box_b) = before
    assert_bbox(box_a, RECT_A)
    assert_bbox(box_b, RECT_B)

    # --- select both and merge with the real shortcut --------------------------
    click(qtbot, win, *IN_A)
    click(qtbot, win, *IN_B, modifier=CTRL)
    assert win.selected_sids == [sid_a, sid_b]

    key(qtbot, win, Qt.Key.Key_M, CTRL)

    assert len(win.shapes) == 1
    merged = win.shapes[0]
    assert merged.kind == "multirect"
    assert merged.name == name_a, "the first-selected shape names the merged area"
    assert len(merged.rects) == 2
    assert_bbox(merged.rects[0], box_a)
    assert_bbox(merged.rects[1], box_b)
    assert_bbox(merged.bbox(), ENVELOPE)
    assert win.selected_sid == merged.sid
    assert win.areas_list.count() == 1

    # --- the notch is not part of the shape ------------------------------------
    click(qtbot, win, *NOTCH)
    assert win.selected_sid is None, "the notch is inside the envelope, not the area"
    assert win.selected_sids == []

    click(qtbot, win, *IN_B)
    assert win.selected_sid == merged.sid, "a click in a piece selects the area"

    # --- a nudge moves every piece, not just the envelope ----------------------
    pieces = pieces_of(merged)
    key(qtbot, win, Qt.Key.Key_Right)
    assert pieces_of(merged) == [(x1 + 1, y1, x2 + 1, y2) for x1, y1, x2, y2 in pieces]
    assert merged.bbox() == (ENVELOPE[0] + 1, ENVELOPE[1], ENVELOPE[2] + 1, ENVELOPE[3])

    # --- undo the nudge, then the merge itself ---------------------------------
    key(qtbot, win, *UNDO_CHORD)
    assert pieces_of(win.shapes[0]) == pieces

    key(qtbot, win, *UNDO_CHORD)
    assert [(s.sid, s.name, s.bbox()) for s in win.shapes] == before
    assert [s.kind for s in win.shapes] == ["rect", "rect"]
    assert all(s.rects is None for s in win.shapes)

    key(qtbot, win, *REDO_CHORD)
    assert [s.kind for s in win.shapes] == ["multirect"]
    assert pieces_of(win.shapes[0]) == pieces

    # --- split it back from the canvas context menu ----------------------------
    click(qtbot, win, *IN_A)
    scripted_menu.choice = "Split"
    right_click(win, *IN_A)
    assert "Split" in scripted_menu.last and "Merge Selected" in scripted_menu.last

    assert [s.kind for s in win.shapes] == ["rect", "rect"]
    assert [s.name for s in win.shapes] == [f"{name_a}_1", f"{name_a}_2"]
    for shape, piece in zip(win.shapes, pieces):
        assert shape.bbox() == piece
        assert shape.rects is None
    assert win.areas_list.count() == 2


# ---------------------------------------------------------------------------
# M2 -- the guard: only rects and merged areas can be merged
# ---------------------------------------------------------------------------

def test_m2_ctrl_m_refuses_a_circle(win, qtbot):
    draw(qtbot, win, (100, 100, 300, 200))
    draw(qtbot, win, (400, 100, 500, 200), tool_key=Qt.Key.Key_C)
    key(qtbot, win, Qt.Key.Key_V)
    before = [(s.sid, s.kind, s.name, s.bbox()) for s in win.shapes]
    assert [s.kind for s in win.shapes] == ["rect", "circle"]

    click(qtbot, win, 200, 150)
    click(qtbot, win, 450, 150, modifier=CTRL)
    assert len(win.selected_sids) == 2

    key(qtbot, win, Qt.Key.Key_M, CTRL)

    assert [(s.sid, s.kind, s.name, s.bbox()) for s in win.shapes] == before
    assert all(s.rects is None for s in win.shapes)
    assert "Cannot merge" in win.status_bar.currentMessage()
