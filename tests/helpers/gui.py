"""Real-event helpers for the pytest-qt GUI tests.

Everything here drives the app the way a user does: synthesized mouse/key events
delivered to ``win.canvas.viewport()`` (or a side-panel widget). No helper here
calls the handler under test directly.

Facts about ``CanvasView`` (areaDef.py ~1845-2350) these helpers depend on:

* Mouse handlers read ``self.mapToScene(event.pos())``, i.e. **viewport**
  coordinates -- so events must be sent to ``canvas.viewport()``, and scene
  coordinates must be mapped with ``canvas.mapFromScene`` (:func:`canvas_point`).
* There is no drag threshold of its own: ``mousePressEvent`` sets ``drag_mode``
  immediately (draw / move / resize / marquee / pan).
* Move and resize geometry is applied in ``mouseMoveEvent`` **only**
  (areaDef.py:2040-2062); the release just commits the deferred undo snapshot.
  A drag therefore needs real intermediate move events -- see :func:`drag`.
* Draw and marquee, by contrast, recompute from press + release positions, so
  their result is exact even though the preview is driven by the moves.
* Modifiers are read at press time: Ctrl = toggle multi-selection,
  Shift on empty canvas = marquee. Middle button = pan.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QPointF, Qt, QTimer
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication

#: GUI bbox tolerance from the global constraints (px).
BBOX_TOL = 2.0

NO_MOD = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
LEFT = Qt.MouseButton.LeftButton


def flush_timers(qtbot, ms: int = 25) -> None:
    """Pump the event loop until a timer scheduled *now* has fired.

    ``load_image()`` and ``_set_pdf_page()`` defer ``canvas.fit_to_view()`` with
    ``QTimer.singleShot(10, ...)``. A single-shot armed later cannot expire
    before it, so waiting on ours guarantees any pending fit has been applied.
    Race-free whether or not a fit is actually pending (unlike polling the
    transform), and uses only ``qtbot.waitUntil`` -- no sleeps.
    """
    fired: list[bool] = []
    QTimer.singleShot(ms, lambda: fired.append(True))
    qtbot.waitUntil(lambda: bool(fired), timeout=5000)


def show_window(win, qtbot):
    """Show and activate the window. Required: the app's shortcuts are
    ``QShortcut``/``QAction`` with the default WindowShortcut context, which only
    fire while their window is the active one."""
    with qtbot.waitExposed(win):
        win.show()
    win.activateWindow()
    win.canvas.setFocus()
    assert QApplication.activeWindow() is win, (
        "window is not active; window-context shortcuts will not fire"
    )
    return win


def settle_view(win, qtbot) -> None:
    """Flush the deferred fit and pin the canvas at 1:1, centred on the image.

    Two reasons this must happen before any coordinate-based interaction:
    a pending ``fit_to_view`` would otherwise fire from inside ``QTest``'s own
    ``processEvents()`` in the middle of a drag and move the mapping under us,
    and at 1:1 the scene<->viewport round trip is exact.
    """
    flush_timers(qtbot)
    canvas = win.canvas
    canvas.resetTransform()
    width, height = canvas.image_size
    canvas.centerOn(QPointF(width / 2.0, height / 2.0))
    assert canvas.transform().m11() == 1.0


def load_media(win, qtbot, path) -> None:
    """Test *setup*: open a file without going through the dialog. The
    dialog-driven route is what test_gui_dialogs.py::D1 and G8 cover."""
    assert win.load_image(str(path)) is True, f"load_image({path}) reported failure"
    settle_view(win, qtbot)


def canvas_point(win, x: float, y: float) -> QPoint:
    """Scene (image-pixel) coordinates -> viewport coordinates for events."""
    point = win.canvas.mapFromScene(QPointF(x, y))
    rect = win.canvas.viewport().rect()
    assert rect.contains(point), (
        f"scene point ({x}, {y}) maps to {point}, outside the viewport {rect}"
    )
    return point


def click(qtbot, win, x: float, y: float, modifier=NO_MOD) -> None:
    """Left-click the canvas at a scene position."""
    qtbot.mouseClick(win.canvas.viewport(), LEFT, modifier, canvas_point(win, x, y))


def right_click(win, x: float, y: float) -> None:
    """Ask for the context menu at a scene position.

    QTest has no context-menu gesture (the platform, not QTest, turns a right
    button press into one), so the request is delivered as the QContextMenuEvent
    the platform would send. It still enters through the widget's own
    ``contextMenuEvent`` -- the viewport forwards it to CanvasView unchanged.
    """
    point = canvas_point(win, x, y)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point,
                              win.canvas.viewport().mapToGlobal(point))
    QApplication.sendEvent(win.canvas.viewport(), event)


def drag(qtbot, win, start, end, modifier=NO_MOD, steps: int = 4) -> None:
    """Press, ``steps`` intermediate moves, release -- all in scene coordinates.

    The intermediate moves are load-bearing for move/resize (see the module
    docstring). ``qtbot.mouseMove`` delivers reliably offscreen *provided*
    :func:`settle_view` has already run.
    """
    viewport = win.canvas.viewport()
    p1 = canvas_point(win, *start)
    p2 = canvas_point(win, *end)
    qtbot.mousePress(viewport, LEFT, modifier, p1)
    for i in range(1, steps + 1):
        qtbot.mouseMove(viewport, QPoint(
            p1.x() + (p2.x() - p1.x()) * i // steps,
            p1.y() + (p2.y() - p1.y()) * i // steps,
        ))
    qtbot.mouseRelease(viewport, LEFT, modifier, p2)


def key(qtbot, win, key_, modifier=NO_MOD) -> None:
    """Send a keystroke to the canvas (the widget a user has focused)."""
    qtbot.keyClick(win.canvas.viewport(), key_, modifier)


def draw(qtbot, win, box, tool_key=Qt.Key.Key_R) -> None:
    """Pick a tool with its single-letter shortcut, then drag out ``box``
    (x1, y1, x2, y2) in scene coordinates."""
    key(qtbot, win, tool_key)
    drag(qtbot, win, box[:2], box[2:])


def menu_action(win, menu_title: str, action_text: str):
    """The QAction behind a menu entry. The app keeps these as locals in
    ``_build_menus()``, so they are looked up by their text (which may carry a
    ``\\t``-separated shortcut hint)."""
    for menu_entry in win.menuBar().actions():
        if menu_entry.text() != menu_title:
            continue
        for action in menu_entry.menu().actions():
            if action.text().split("\t")[0] == action_text:
                return action
    raise AssertionError(f"no {menu_title!r} > {action_text!r} menu action")


def assert_bbox(actual, expected, tol: float = BBOX_TOL) -> None:
    deltas = [abs(a - e) for a, e in zip(actual, expected)]
    assert len(actual) == len(expected) == 4 and max(deltas) <= tol, (
        f"bbox {tuple(actual)} != {tuple(expected)} (deltas {deltas}, tol {tol})"
    )
