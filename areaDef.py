#!/usr/bin/env python3
"""
Template Annotator (PyQt6 Version)

Draw and name regions on top of an image, then export the region definitions as coordinates.
Supports JPG/PNG/BMP/etc images and the first page of PDF files.

Tools:
- Select/Move/Resize (with corner handles)
- Rectangle (for text regions, etc.)
- Circle (enforced circle)
- Chamfer Rect (rectangle with cut corners; useful for pill-style boxes)

Exports (pixel coordinates in the ORIGINAL image coordinate system):
- JSON (recommended): image metadata + list of shapes with bbox coords
- CSV (optional): flat table of coords per shape

Dependencies:
- Python 3.9+
- PyQt6: pip install PyQt6
- Pillow: pip install pillow

Run:
  python areaDef.py --image template.png
or:
  python areaDef.py
CLI tools:
  python areaDef.py info path/to/file
  python areaDef.py render path/to/doc.pdf --out-dir ./pdf_previews
  python areaDef.py normalize-json path/to/project.json -o path/to/out.json

Keyboard Shortcuts:
  V - Select/Move/Resize tool
  R - Rectangle tool
  C - Circle tool
  H - Chamfer Rect tool
  N - Normalize/align shapes
  L - Detect document lines (snap fields to rules/underlines)
  B - Detect check-mark boxes (adds a field per box)
  X - Detect text strings (boxes where text is)
  G - Detect high-contrast regions (solid bars/headers; edges snap)
  M - Detect markers (QR codes / AprilTag / ArUco; one field each)
  K - Detect barcodes (1D; one field each)
  Arrow keys - Nudge selected shape (Shift = 10 px)
  Z - Zoom to selection
  Delete - Delete selected shape
  Ctrl+Z - Undo
  Ctrl+Y / Ctrl+Shift+Z - Redo
  Ctrl+D - Duplicate selected shape
  Ctrl+S - Save JSON
  Ctrl+O - Open image
  F1 - Show keyboard shortcuts
  T - Toggle light/dark theme
  (single-letter shortcuts pause while a text field has focus)

Navigation:
  Ctrl+MouseWheel - Zoom in/out (at cursor position)
  Middle Mouse Button - Pan/drag view

Selection & Align (Adobe-style):
  Ctrl+Click - Add/remove a shape from the selection (last = key/anchor)
  Shift+Drag - Marquee-select every shape the rubber-band touches
  Align panel - Align edges/centers or match size of the selection to the key
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import tempfile
import traceback
from collections import OrderedDict
from pathlib import Path
from dataclasses import dataclass, field
from typing import (Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence,
                    Tuple, TypedDict)

from PyQt6.QtCore import (
    Qt, QObject, QPointF, QRectF, QRunnable, QThreadPool, QTimer, pyqtSignal
)
from PyQt6.QtGui import (
    QAction, QActionGroup, QBrush, QColor, QKeySequence, QPainter,
    QPainterPath, QPen, QPixmap, QPolygonF, QWheelEvent, QMouseEvent,
    QShortcut, QImage
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsItem,
    QGraphicsRectItem, QGraphicsEllipseItem, QGraphicsPolygonItem,
    QPushButton, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QRadioButton, QCheckBox, QGroupBox, QListWidget, QListWidgetItem,
    QFileDialog, QMessageBox, QInputDialog, QStatusBar,
    QButtonGroup, QMenu, QScrollArea, QSplitter
)

try:
    from PIL import Image
except ImportError as e:
    raise SystemExit("Missing dependency Pillow. Install with: pip install pillow") from e

try:
    import fitz
except ImportError:
    fitz = None

#: Fallback for `--version` when the package was never pip-installed (running
#: straight from a checkout is the normal case). Keep in step with
#: pyproject.toml's [project] version.
__version__ = "1.1.0"


def _package_version() -> str:
    """The installed distribution's version, or the source-tree fallback."""
    try:
        from importlib.metadata import version
        return version("template-annotator")
    except Exception:
        # PackageNotFoundError (not installed) -- and never let a metadata
        # problem take down a --version call.
        return f"{__version__} (source)"

# Optional marker/barcode detection backends. Each is independent: any subset
# may be missing and the ensemble simply narrows its coverage (it never errors).
# cv2 + numpy: QR, ArUco/AprilTag, 1D barcodes. pyzbar: QR + 1D barcodes (needs
# the system libzbar). pupil_apriltags: AprilTags.
#
# Imported on FIRST USE, not at module import: together they cost ~200 ms of
# startup, which every pure-JSON CLI call (and the GUI launch) would otherwise
# pay for a feature it may never touch. The globals below hold `_UNSET` until the first attempt,
# then the module (available) or None (not installed) -- so the `is None` checks
# throughout still mean "unavailable" and never "not loaded yet". Tests may
# assign a global directly to force a backend on or off; the loaders honour that.
_UNSET = object()

_np = _UNSET
_cv2 = _UNSET
_pyzbar = _UNSET
_ZBarSymbol = _UNSET
_AprilTagDetector = _UNSET


def _get_np():
    global _np
    if _np is _UNSET:
        try:
            import numpy
        except ImportError:
            numpy = None
        _np = numpy
    return _np


def _get_cv2():
    global _cv2
    if _cv2 is _UNSET:
        try:
            import cv2
        except ImportError:
            cv2 = None
        _cv2 = cv2
    return _cv2


def _get_pyzbar():
    global _pyzbar, _ZBarSymbol
    if _pyzbar is _UNSET:
        try:
            from pyzbar import pyzbar
            from pyzbar.pyzbar import ZBarSymbol
        except Exception:
            pyzbar = None
            ZBarSymbol = None
        _pyzbar = pyzbar
        _ZBarSymbol = ZBarSymbol
    return _pyzbar


def _get_apriltag_detector():
    global _AprilTagDetector
    if _AprilTagDetector is _UNSET:
        try:
            from pupil_apriltags import Detector
        except Exception:
            Detector = None
        _AprilTagDetector = Detector
    return _AprilTagDetector


class Line(NamedTuple):
    """A snap target: a detected rule, or a synthetic edge of a text/high-contrast
    region. `pos` is the coordinate on the line's own axis (y for a horizontal
    rule, x for a vertical one) and [start, end] the span it covers on the other.

    `half_thickness` is half the ink band's width, so an edge can be snapped to
    the side of the rule the field sits on instead of onto the ink. Synthetic
    edges have none. Plain 3-tuples are still accepted everywhere a Line is (see
    _snap_edge)."""
    pos: float
    start: float
    end: float
    half_thickness: float = 0.0


class Detection(TypedDict):
    """One decoded marker/barcode: its bbox, the backend's name for what it
    found ("qr", "apriltag", "barcode", ...) and the decoded payload ("" when
    the code was located but not decoded)."""
    bbox: Tuple[float, float, float, float]
    kind: str
    payload: str


#: Theme choices, in View > Theme order. "auto" resolves to light or dark.
THEME_MODES = ("auto", "light", "dark")

MIN_SHAPE_SIZE_PX = 6.0
MAX_UNDO_HISTORY = 50

# Arrow-key nudges within this many ms of each other are one undo step, so a
# held arrow key produces one history entry instead of one per key repeat.
NUDGE_COALESCE_MS = 300

# Scale at which PDF pages are rasterized. The GUI stores shape coordinates in
# this rendered-pixel space, so any code that reports/uses PDF dimensions must
# apply the same factor (see _load_media_dimensions / _cli_print_info).
PDF_RENDER_SCALE = 2.0

# Cap the longest rendered PDF side so a single oversized page cannot allocate
# an enormous bitmap.
MAX_PDF_RENDER_PX = 6000

# Memory budget for the rendered-page cache. A fixed count of pages says nothing
# about memory: five A4 pages at PDF_RENDER_SCALE are ~50 MB, five large-format
# drawings are well over a gigabyte. The most recent page is always kept, even
# if it alone exceeds the budget.
MAX_PDF_CACHE_BYTES = 100 * 1024 * 1024

# Per-band lookup table for _invert_image: R, G and B inverted, alpha unchanged.
_INVERT_RGBA_LUT = [255 - v for v in range(256)] * 3 + list(range(256))

# pupil_apriltags (native) segfaults non-deterministically on large frames, so
# its input is downscaled to at most this longest side and the detected corners
# are scaled back to full resolution.
APRILTAG_MAX_DIM = 1280

# Zoom bounds (canvas scale factor). Clamping prevents zooming the page down to a
# sub-pixel speck or up to a memory-thrashing extreme.
MIN_ZOOM = 0.05
MAX_ZOOM = 40.0

VALID_KINDS = ("rect", "circle", "chamfer", "multirect")


def _running_mean_clusters(items, key, tol):
    """Group items into 1-D clusters: an item joins the current cluster when its
    key is within `tol` of the running mean of that cluster's keys, otherwise it
    starts a new cluster. Shared by the GUI and CLI normalization paths so the
    column-detection behavior stays identical everywhere.
    """
    items = list(items)
    if not items:
        return []
    tol = max(0.0, float(tol))
    ordered = sorted(items, key=key)
    clusters = []
    cur = []
    cur_vals = []
    for it in ordered:
        v = float(key(it))
        if not cur:
            cur = [it]
            cur_vals = [v]
            continue
        m = sum(cur_vals) / len(cur_vals)
        if abs(v - m) <= tol:
            cur.append(it)
            cur_vals.append(v)
        else:
            clusters.append(cur)
            cur = [it]
            cur_vals = [v]
    if cur:
        clusters.append(cur)
    return clusters


# Default distance (in image pixels) within which a field edge snaps to a line.
LINE_SNAP_TOL_PX = 12.0


# ============================================================================
# Detector primitives
# ============================================================================
# Every detector below binarizes the page once and then works on ink runs or
# connected components. Each primitive has a vectorized implementation (numpy,
# plus cv2 for component labeling) and a pure-Python fallback for installs
# without them. The two paths are held to BIT-IDENTICAL output -- same values in
# the same order -- because downstream grouping breaks ties by encounter order
# (see tests/test_detector_parity.py, which runs both paths on the committed
# fixtures and on randomized dense/1-px-corner images).


def _binarize(image, threshold):
    """Binarize `image` once: ink is any pixel darker than `threshold`.

    Returns (arr, binary, w, h). With numpy installed `arr` is a bool ink mask
    (True = ink) and `binary` is None; otherwise `arr` is None and `binary` is
    the PIL ink=255 image the pure-Python primitives scan. Callers must check
    w/h for the empty-image case before using either.
    """
    gray = image.convert("L")
    w, h = gray.size
    if w == 0 or h == 0:
        return None, None, w, h
    np = _get_np()
    if np is not None:
        return np.asarray(gray, dtype=np.uint8) < threshold, None, w, h
    return None, gray.point(lambda v: 255 if v < threshold else 0, mode="L"), w, h


def _ink_runs_np(arr, max_gap: int):
    """Vectorized row-wise ink runs: (rows, x0, x1, ink) int arrays in raster
    order, where blank gaps of at most `max_gap` px are bridged into one run and
    `ink` counts the actual ink pixels inside [x0, x1].

    Runs are found by flattening the mask with `max_gap + 1` blank columns
    appended per row (so no run can bridge two rows) and splitting the ink
    indices wherever consecutive ink is more than `max_gap` apart -- the same
    segmentation the pure-Python scanner performs one pixel at a time.
    """
    np = _get_np()
    h, w = arr.shape
    pad = max_gap + 1
    padded = np.zeros((h, w + pad), dtype=bool)
    padded[:, :w] = arr
    idx = np.flatnonzero(padded.ravel())
    if idx.size == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, empty, empty
    brk = np.flatnonzero(np.diff(idx) > pad)
    s_i = np.concatenate((np.zeros(1, dtype=np.int64), brk + 1))
    e_i = np.concatenate((brk, np.full(1, idx.size - 1, dtype=np.int64)))
    start = idx[s_i]
    end = idx[e_i]
    stride = w + pad
    rows = start // stride
    return rows, start - rows * stride, end - rows * stride, e_i - s_i + 1


def _scan_runs_np(arr, min_len: int, max_gap: int, min_fill: float) -> List[Tuple[int, int, int]]:
    """numpy equivalent of _scan_horizontal_runs (same runs, same order)."""
    rows, x0, x1, ink = _ink_runs_np(arr, max_gap)
    if rows.size == 0:
        return []
    span = x1 - x0 + 1
    keep = (span >= min_len) & (ink / span >= min_fill)
    return list(zip(rows[keep].tolist(), x0[keep].tolist(), x1[keep].tolist()))


def _run_is_thin_np(arr, run: Tuple[int, int, int], max_thickness: int, core_fill: float) -> bool:
    """numpy equivalent of _run_is_thin. Only the +/- max_thickness rows around
    the run can matter: one more contiguous row than that already fails."""
    y, x0, x1 = run
    span = x1 - x0 + 1
    if span <= 0:
        return False
    lo = max(0, y - max_thickness)
    hi = min(arr.shape[0], y + max_thickness + 1)
    fills = arr[lo:hi, x0:x1 + 1].sum(axis=1) / span
    here = y - lo
    thickness = 1
    i = here - 1
    while i >= 0 and fills[i] >= core_fill:
        thickness += 1
        if thickness > max_thickness:
            return False
        i -= 1
    i = here + 1
    while i < fills.size and fills[i] >= core_fill:
        thickness += 1
        if thickness > max_thickness:
            return False
        i += 1
    return thickness <= max_thickness


def _thin_runs(arr, buf, w: int, h: int, min_len: int, max_gap: int, min_fill: float,
               max_thickness: int, core_fill: float) -> List[Tuple[int, int, int]]:
    """Ink runs that are long/solid enough to be a rule and thin enough not to
    be a glyph body. Vectorized when numpy is available, else pure Python."""
    if arr is not None:
        runs = _scan_runs_np(arr, min_len, max_gap, min_fill)
        return [r for r in runs if _run_is_thin_np(arr, r, max_thickness, core_fill)]
    runs = _scan_horizontal_runs(buf, w, h, min_len, max_gap, min_fill)
    return [r for r in runs if _run_is_thin(buf, w, h, r, max_thickness, core_fill)]


def _scan_horizontal_runs(
    buf: bytes, w: int, h: int, min_len: int, max_gap: int, min_fill: float = 0.7
) -> List[Tuple[int, int, int]]:
    """Scan a binary (ink=255) row-major byte buffer for horizontal runs of ink.

    Returns (row, x_start, x_end) for each run that is at least `min_len` long
    AND at least `min_fill` actual ink (ink pixels / span). The fill requirement
    rejects rows of text whose inter-glyph gaps get bridged into a long but
    mostly-empty "run" — a real rule is nearly solid, text is not.
    """
    runs: List[Tuple[int, int, int]] = []
    for y in range(h):
        base = y * w
        x = 0
        while x < w:
            if buf[base + x] >= 128:
                x0 = x
                xe = x
                gap = 0
                ink = 1
                x += 1
                while x < w:
                    if buf[base + x] >= 128:
                        xe = x
                        gap = 0
                        ink += 1
                    else:
                        gap += 1
                        if gap > max_gap:
                            break
                    x += 1
                span = xe - x0 + 1
                if span >= min_len and (ink / span) >= min_fill:
                    runs.append((y, x0, xe))
            else:
                x += 1
    return runs


def _run_is_thin(buf: bytes, w: int, h: int, run: Tuple[int, int, int],
                 max_thickness: int, core_fill: float) -> bool:
    """True if the ink band at this run is thin (a rule), not a tall block (text
    glyph bodies, headings, or filled bars).

    Thickness = the number of contiguous rows around the run's row whose ink fill
    over the run's span stays >= core_fill. A 1-3px rule has thickness <= a few;
    a heading stroke or filled region spans many rows.
    """
    y, x0, x1 = run
    span = x1 - x0 + 1
    if span <= 0:
        return False

    def row_fill(yy: int) -> float:
        if yy < 0 or yy >= h:
            return 0.0
        base = yy * w
        ink = 0
        for x in range(x0, x1 + 1):
            if buf[base + x] >= 128:
                ink += 1
        return ink / span

    thickness = 1
    yy = y - 1
    while yy >= 0 and row_fill(yy) >= core_fill:
        thickness += 1
        yy -= 1
        if thickness > max_thickness:
            return False
    yy = y + 1
    while yy < h and row_fill(yy) >= core_fill:
        thickness += 1
        yy += 1
        if thickness > max_thickness:
            return False
    return thickness <= max_thickness


def _merge_line_runs(runs: List[Tuple[int, int, int]], axis_tol: float, gap_tol: float) -> List[Tuple[float, float, float, float]]:
    """Merge runs that lie on nearly the same perpendicular position and overlap
    along their length into single lines:
    (position, span_start, span_end, half_thickness).

    `half_thickness` is half the perpendicular spread of the merged run-rows, so
    a 1px rule reports 0 while a multi-pixel rule reports its half-width. Fields
    snap to the rule's outer edge (position +/- half_thickness) rather than its
    centre, so a box hugs the rule without covering the ink."""
    merged: List[Dict] = []
    for p, a0, a1 in sorted(runs):
        placed = False
        for L in merged:
            if abs(L["p"] - p) <= axis_tol and not (a1 < L["a0"] - gap_tol or a0 > L["a1"] + gap_tol):
                L["ps"].append(p)
                L["a0"] = min(L["a0"], a0)
                L["a1"] = max(L["a1"], a1)
                L["p"] = sum(L["ps"]) / len(L["ps"])
                placed = True
                break
        if not placed:
            merged.append({"p": float(p), "ps": [p], "a0": float(a0), "a1": float(a1)})
    return [
        Line(round(L["p"], 2), L["a0"], L["a1"],
             round((max(L["ps"]) - min(L["ps"])) / 2.0, 2))
        for L in merged
    ]


def detect_document_lines(
    image,
    *,
    threshold: int = 150,
    max_gap: int = 4,
    axis_tol: float = 4.0,
    min_len_frac: float = 0.05,
    min_len_abs: int = 40,
    min_fill: float = 0.7,
    max_thickness: int = 6,
    core_fill: float = 0.6,
    min_v_len_abs: int = 16,
    v_max_gap: int = 6,
) -> Tuple[List[Line], List[Line]]:
    """Detect horizontal and vertical rules/underlines in a page image.

    Works on the rendered bitmap, so it handles vector PDFs, scanned PDFs, and
    raster images uniformly. Returns (h_lines, v_lines) where:
      h_lines = [(y, x_start, x_end, half_thickness), ...]   horizontal rules
      v_lines = [(x, y_start, y_end, half_thickness), ...]   vertical rules
    all in the image's own pixel coordinate space.

    Text is rejected by two requirements that real rules satisfy but text does
    not: `min_fill` (the run must be mostly solid ink, not bridged whitespace
    between glyphs) and `max_thickness` (the ink band must be thin, not a tall
    block of glyph bodies, headings, or shaded fills).

    Vertical rules use a lower length floor (`min_v_len_abs`) and a wider gap
    tolerance (`v_max_gap`) than horizontals: form verticals are routinely short
    (pill-box sides, stub margin rules) and broken by rounded corners, so the
    horizontal defaults would miss them.
    """
    if image is None:
        return [], []
    arr, binary, w, h = _binarize(image, threshold)
    if w == 0 or h == 0:
        return [], []

    hbuf = None if binary is None else binary.tobytes()
    min_h = max(min_len_abs, int(w * min_len_frac))
    h_runs = _thin_runs(arr, hbuf, w, h, min_h, max_gap, min_fill, max_thickness, core_fill)
    h_lines = _merge_line_runs(h_runs, axis_tol, gap_tol=max(20.0, w * 0.02))

    # Transpose so vertical rules become horizontal runs, then map back: a run at
    # transposed-row r spanning cols [c0, c1] is a vertical line at x=r, y=[c0,c1].
    if arr is not None:
        t_arr, tbuf, tw, th = arr.T, None, h, w
    else:
        transposed = binary.transpose(Image.Transpose.TRANSPOSE)
        t_arr, tbuf = None, transposed.tobytes()
        tw, th = transposed.size
    min_v = max(min_v_len_abs, int(h * min_len_frac))
    v_runs = _thin_runs(t_arr, tbuf, tw, th, min_v, v_max_gap, min_fill, max_thickness, core_fill)
    v_lines = _merge_line_runs(v_runs, axis_tol, gap_tol=max(20.0, h * 0.02))

    return h_lines, v_lines


def _box_border_fill(orig: bytes, w: int, h: int, minx: int, miny: int,
                     maxx: int, maxy: int, band: int) -> float:
    """Minimum ink fill of the four bbox borders, each measured over a `band`-px
    strip and taking the best row/col in the strip (robust to anti-aliasing and a
    1-2px inset border). A complete square frame -> ~1.0; a glyph -> well below.
    """
    bw = maxx - minx + 1
    bh = maxy - miny + 1
    if bw <= 0 or bh <= 0:
        return 0.0

    def row_fill(y: int) -> float:
        base = y * w
        return sum(1 for x in range(minx, maxx + 1) if orig[base + x] >= 128) / bw

    def col_fill(x: int) -> float:
        return sum(1 for y in range(miny, maxy + 1) if orig[y * w + x] >= 128) / bh

    top = max(row_fill(miny + k) for k in range(min(band, bh)))
    bot = max(row_fill(maxy - k) for k in range(min(band, bh)))
    left = max(col_fill(minx + k) for k in range(min(band, bw)))
    right = max(col_fill(maxx - k) for k in range(min(band, bw)))
    return min(top, bot, left, right)


def _box_border_fill_np(arr, minx: int, miny: int, maxx: int, maxy: int, band: int) -> float:
    """numpy equivalent of _box_border_fill (identical value)."""
    bw = maxx - minx + 1
    bh = maxy - miny + 1
    if bw <= 0 or bh <= 0:
        return 0.0
    box = arr[miny:maxy + 1, minx:maxx + 1]
    rows = box.sum(axis=1)
    cols = box.sum(axis=0)
    nb_h = min(band, bh)
    nb_w = min(band, bw)
    top = max(int(rows[k]) for k in range(nb_h)) / bw
    bot = max(int(rows[bh - 1 - k]) for k in range(nb_h)) / bw
    left = max(int(cols[k]) for k in range(nb_w)) / bh
    right = max(int(cols[bw - 1 - k]) for k in range(nb_w)) / bh
    return min(top, bot, left, right)


def _connected_components(buf: bytearray, w: int, h: int) -> List[Tuple[int, int, int, int, int]]:
    """Find 4-connected ink components in a binary (ink>=128) buffer. Mutates
    `buf` (visited pixels are zeroed). Returns (minx, miny, maxx, maxy, count)
    for each component."""
    comps: List[Tuple[int, int, int, int, int]] = []
    for sy in range(h):
        rowbase = sy * w
        for sx in range(w):
            if buf[rowbase + sx] < 128:
                continue
            minx = maxx = sx
            miny = maxy = sy
            count = 0
            stack = [(sx, sy)]
            buf[rowbase + sx] = 0
            while stack:
                cx, cy = stack.pop()
                count += 1
                if cx < minx:
                    minx = cx
                elif cx > maxx:
                    maxx = cx
                if cy < miny:
                    miny = cy
                elif cy > maxy:
                    maxy = cy
                b = cy * w
                if cx > 0 and buf[b + cx - 1] >= 128:
                    buf[b + cx - 1] = 0
                    stack.append((cx - 1, cy))
                if cx < w - 1 and buf[b + cx + 1] >= 128:
                    buf[b + cx + 1] = 0
                    stack.append((cx + 1, cy))
                if cy > 0 and buf[b - w + cx] >= 128:
                    buf[b - w + cx] = 0
                    stack.append((cx, cy - 1))
                if cy < h - 1 and buf[b + w + cx] >= 128:
                    buf[b + w + cx] = 0
                    stack.append((cx, cy + 1))
            comps.append((minx, miny, maxx, maxy, count))
    return comps


def _components_cv2(arr) -> List[Tuple[int, int, int, int, int]]:
    """4-connected components via OpenCV, reordered to the pure-Python flood
    fill's order (by each component's first pixel in raster order)."""
    np = _get_np()
    cv2 = _get_cv2()
    n, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        arr.view(np.uint8), connectivity=4)
    if n <= 1:
        return []
    # Smallest flat index per label: assigning in reverse raster order leaves
    # the FIRST occurrence in place (numpy fancy assignment keeps the last write).
    idx = np.flatnonzero(arr.ravel())
    lab = labels.ravel()[idx]
    first = np.zeros(n, dtype=np.int64)
    first[lab[::-1]] = idx[::-1]
    order = np.argsort(first[1:], kind="stable") + 1   # label 0 is background
    s = stats[order]
    return list(zip(s[:, 0].tolist(), s[:, 1].tolist(),
                    (s[:, 0] + s[:, 2] - 1).tolist(),
                    (s[:, 1] + s[:, 3] - 1).tolist(),
                    s[:, 4].tolist()))


def _components_np(arr) -> List[Tuple[int, int, int, int, int]]:
    """4-connected components with numpy but no OpenCV: extract per-row ink runs
    (vectorized), then union runs that overlap in x on adjacent rows. Union
    always keeps the LOWER run index as the root, so a component's root is its
    first run in raster order and sorting by root reproduces the flood fill's
    component order."""
    np = _get_np()
    rows, x0, x1, _ink = _ink_runs_np(arr, 0)
    n = rows.size
    if n == 0:
        return []
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    rl = rows.tolist()
    a0 = x0.tolist()
    a1 = x1.tolist()
    groups = [0] + (np.flatnonzero(np.diff(rows)) + 1).tolist() + [n]
    for gi in range(1, len(groups) - 1):
        prev, cur, end = groups[gi - 1], groups[gi], groups[gi + 1]
        if rl[cur] - rl[prev] != 1:
            continue                        # rows not adjacent: nothing to join
        i, j = cur, prev
        while i < end and j < cur:
            if a1[j] < a0[i]:
                j += 1
            elif a1[i] < a0[j]:
                i += 1
            else:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
                if a1[i] <= a1[j]:
                    i += 1
                else:
                    j += 1
    root = np.array([find(i) for i in range(n)], dtype=np.int64)
    inv = np.unique(root, return_inverse=True)[1].ravel()
    order = np.argsort(inv, kind="stable")
    starts = np.concatenate((np.zeros(1, dtype=np.int64),
                             np.flatnonzero(np.diff(inv[order])) + 1))
    minx = np.minimum.reduceat(x0[order], starts)
    maxx = np.maximum.reduceat(x1[order], starts)
    miny = np.minimum.reduceat(rows[order], starts)
    maxy = np.maximum.reduceat(rows[order], starts)
    count = np.add.reduceat((x1 - x0 + 1)[order], starts)
    return list(zip(minx.tolist(), miny.tolist(), maxx.tolist(),
                    maxy.tolist(), count.tolist()))


def _find_components(arr, binary, w: int, h: int) -> List[Tuple[int, int, int, int, int]]:
    """(minx, miny, maxx, maxy, count) per 4-connected ink component, ordered by
    each component's first pixel in raster order. OpenCV > numpy > pure Python;
    all three produce the identical list."""
    if arr is None:
        return _connected_components(bytearray(binary.tobytes()), w, h)
    if _get_cv2() is not None:
        return _components_cv2(arr)
    return _components_np(arr)


def detect_checkbox_squares(
    image,
    *,
    threshold: int = 185,
    min_size: int = 14,
    max_size: int = 60,
    min_aspect: float = 0.7,
    max_aspect: float = 1.4,
    min_border_fill: float = 0.8,
    max_interior_fill: float = 0.55,
    border_band: int = 2,
) -> List[Tuple[float, float, float, float]]:
    """Detect check-mark boxes (small empty squares) in a page image.

    Finds connected ink components and keeps those that look like a checkbox: a
    small, roughly-square bounding box, mostly-hollow interior, and four nearly
    complete straight borders. The border-completeness test is what separates a
    real box from letters/loops (B, D, O, 0, 8) — on real forms boxes score a
    border fill of ~0.85-1.0 while text glyphs stay below ~0.7.

    A higher `threshold` than the line detector (185 vs 150) is deliberate: some
    forms render box outlines as faint/anti-aliased gray, and the lighter cutoff
    captures those edges so faint boxes are not missed. The strong border-and-
    hollowness test keeps text from sneaking in at the lighter threshold.

    Returns a list of (x1, y1, x2, y2) bounding boxes in image pixel space.
    """
    if image is None:
        return []
    arr, binary, w, h = _binarize(image, threshold)
    if w == 0 or h == 0:
        return []

    orig = None if binary is None else binary.tobytes()
    boxes: List[Tuple[float, float, float, float]] = []
    for (minx, miny, maxx, maxy, count) in _find_components(arr, binary, w, h):
        bw = maxx - minx + 1
        bh = maxy - miny + 1
        if not (min_size <= bw <= max_size and min_size <= bh <= max_size):
            continue
        if not (min_aspect <= bw / bh <= max_aspect):
            continue
        if count / (bw * bh) > max_interior_fill:
            continue
        border = (_box_border_fill_np(arr, minx, miny, maxx, maxy, border_band)
                  if arr is not None else
                  _box_border_fill(orig, w, h, minx, miny, maxx, maxy, border_band))
        if border >= min_border_fill:
            boxes.append((float(minx), float(miny), float(maxx), float(maxy)))

    return boxes


def detect_high_contrast_regions(
    image,
    *,
    threshold: int = 150,
    min_thin: int = 10,
    min_long: int = 40,
    min_fill: float = 0.5,
) -> List[Tuple[float, float, float, float]]:
    """Detect large, solid high-contrast blocks: filled bars and white-on-black
    headers (e.g. a DESCRIPTION / Qty / Rate / TOTAL row).

    These are the inverse of a checkbox: instead of a small, hollow, four-sided
    frame, a high-contrast block is a big, mostly-FILLED ink component. Knockout
    (white) text inside the bar just punches holes in it, so the bar stays one
    solid component with high fill. A thin rule fails `min_thin` (too skinny on
    its short axis) and ordinary text fails `min_fill` (glyphs are sparse), so
    neither is reported.

    The block must be at least `min_thin` px on its short axis (thicker than a
    rule), at least `min_long` px on its long axis, and at least `min_fill`
    filled. Returns (x1, y1, x2, y2) bounding boxes in image pixel space; the
    edges become snap targets (no shapes are created).
    """
    if image is None:
        return []
    arr, binary, w, h = _binarize(image, threshold)
    if w == 0 or h == 0:
        return []

    regions: List[Tuple[float, float, float, float]] = []
    for (minx, miny, maxx, maxy, count) in _find_components(arr, binary, w, h):
        bw = maxx - minx + 1
        bh = maxy - miny + 1
        if min(bw, bh) < min_thin or max(bw, bh) < min_long:
            continue                       # a thin rule or something too small
        if count / (bw * bh) < min_fill:
            continue                       # hollow box / sparse text, not a solid block
        regions.append((float(minx), float(miny), float(maxx), float(maxy)))

    return regions


# ============================================================================
# Marker & barcode detection (optional ensemble backends)
# ============================================================================
# Each detection is a dict: {"bbox": (x1,y1,x2,y2), "kind": str, "payload": str}.
# Several backends may find the same code; overlapping hits are merged, keeping
# the one that decoded a payload.


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _dedupe_detections(dets: List[Detection], iou_thresh: float = 0.4) -> List[Detection]:
    """Merge detections whose boxes overlap (IoU >= threshold), keeping the one
    that decoded a payload over a blank duplicate from another backend."""
    kept: List[Detection] = []
    for d in dets:
        for i, k in enumerate(kept):
            if _iou(d["bbox"], k["bbox"]) >= iou_thresh:
                if d.get("payload") and not k.get("payload"):
                    kept[i] = d
                break
        else:
            kept.append(d)
    return kept


def _bbox_from_points(pts) -> Tuple[float, float, float, float]:
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _pil_to_gray_np(image):
    np = _get_np()
    if np is None:
        return None
    return np.ascontiguousarray(np.array(image.convert("L")))


def _markers_pyzbar(image, qr_only: bool) -> List[Detection]:
    """Decode with ZBar. qr_only restricts to QR; otherwise returns the 1D
    barcode symbologies (QR/2D excluded)."""
    pyzbar = _get_pyzbar()
    if pyzbar is None:
        return []
    # _ZBarSymbol is resolved by _get_pyzbar(); normalize in case only _pyzbar
    # was pinned from outside.
    symbol = None if _ZBarSymbol is _UNSET else _ZBarSymbol
    two_d = {"QRCODE", "PDF417", "DATAMATRIX", "AZTEC", "MAXICODE"}
    try:
        if qr_only and symbol is not None:
            results = pyzbar.decode(image, symbols=[symbol.QRCODE])
        else:
            results = pyzbar.decode(image)
    except Exception:
        return []
    dets: List[Detection] = []
    for r in results:
        kind = str(getattr(r, "type", "") or "")
        if qr_only:
            if kind != "QRCODE":
                continue
        elif kind in two_d:
            continue
        rect = r.rect
        x1, y1 = float(rect.left), float(rect.top)
        bbox = (x1, y1, x1 + float(rect.width), y1 + float(rect.height))
        try:
            payload = r.data.decode("utf-8", "replace")
        except Exception:
            payload = ""
        dets.append({"bbox": bbox, "kind": ("qr" if kind == "QRCODE" else kind.lower()),
                     "payload": payload})
    return dets


def _markers_cv2_qr(gray) -> List[Detection]:
    try:
        det = _get_cv2().QRCodeDetector()
        res = det.detectAndDecodeMulti(gray)
    except Exception:
        return []
    ok, infos, points = res[0], res[1], res[2]
    if not ok or points is None:
        return []
    dets: List[Detection] = []
    for info, quad in zip(infos, points):
        dets.append({"bbox": _bbox_from_points(quad), "kind": "qr", "payload": info or ""})
    return dets


def _markers_aruco(gray) -> List[Detection]:
    aruco = getattr(_get_cv2(), "aruco", None)
    if aruco is None:
        return []
    dict_names = ("DICT_APRILTAG_36h11", "DICT_APRILTAG_25h9", "DICT_APRILTAG_16h5",
                  "DICT_ARUCO_ORIGINAL", "DICT_4X4_50", "DICT_5X5_50", "DICT_6X6_50")
    dets: List[Detection] = []
    for name in dict_names:
        if not hasattr(aruco, name):
            continue
        try:
            d = aruco.getPredefinedDictionary(getattr(aruco, name))
            if hasattr(aruco, "ArucoDetector"):
                detector = aruco.ArucoDetector(d, aruco.DetectorParameters())
                corners, ids, _rej = detector.detectMarkers(gray)
            else:
                corners, ids, _rej = aruco.detectMarkers(gray, d)
        except Exception:
            continue
        if ids is None:
            continue
        fam = name.replace("DICT_", "").lower()
        for quad, mid in zip(corners, ids.flatten()):
            dets.append({"bbox": _bbox_from_points(quad.reshape(-1, 2)),
                         "kind": "aruco", "payload": f"{fam}:{int(mid)}"})
    return dets


def _markers_apriltag(gray) -> List[Detection]:
    # pupil_apriltags is a native C extension that SIGSEGVs non-deterministically
    # on very large frames (a crash no Python try/except can catch). Cap the
    # longest side, detect, then scale the corners back to full resolution.
    h, w = gray.shape[:2]
    scale = 1.0
    det_gray = gray
    if max(h, w) > APRILTAG_MAX_DIM:
        scale = APRILTAG_MAX_DIM / float(max(h, w))
        small = Image.fromarray(gray).resize(
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))))
        np = _get_np()
        det_gray = np.ascontiguousarray(np.asarray(small))
    try:
        detector = _get_apriltag_detector()(families="tag36h11")
        results = detector.detect(det_gray)
    except Exception:
        return []
    inv = 1.0 / scale
    dets = []
    for r in results:
        corners = [(float(px) * inv, float(py) * inv) for (px, py) in r.corners]
        dets.append({"bbox": _bbox_from_points(corners), "kind": "apriltag",
                     "payload": f"tag36h11:{int(r.tag_id)}"})
    return dets


def _barcodes_cv2(gray) -> List[Detection]:
    bc = getattr(_get_cv2(), "barcode", None)
    if bc is None or not hasattr(bc, "BarcodeDetector"):
        return []
    try:
        res = bc.BarcodeDetector().detectAndDecode(gray)
    except Exception:
        return []
    if not res:
        return []
    points = res[-1]
    if points is None or len(points) == 0:
        return []
    infos = None
    for part in res[:-1]:
        if isinstance(part, (list, tuple)) and part and isinstance(part[0], str):
            infos = part
            break
    dets: List[Detection] = []
    for i, quad in enumerate(points):
        pts = quad.reshape(-1, 2) if hasattr(quad, "reshape") else quad
        payload = infos[i] if (infos and i < len(infos)) else ""
        dets.append({"bbox": _bbox_from_points(pts), "kind": "barcode", "payload": payload or ""})
    return dets


def detect_markers(image) -> List[Detection]:
    """Locate QR codes and AprilTag/ArUco fiducials, decoding payloads where
    possible. Runs every installed backend (pyzbar QR, OpenCV QR, OpenCV ArUco
    AprilTag families, pupil-apriltags) and merges overlapping hits, preferring
    a decoded value. Returns a list of {"bbox", "kind", "payload"} dicts; empty
    if no backend is installed or nothing is found."""
    if image is None:
        return []
    dets: List[Detection] = []
    dets += _markers_pyzbar(image, qr_only=True)
    gray = _pil_to_gray_np(image)
    if gray is not None and _get_cv2() is not None:
        dets += _markers_cv2_qr(gray)
        dets += _markers_aruco(gray)
    if gray is not None and _get_apriltag_detector() is not None:
        dets += _markers_apriltag(gray)
    return _dedupe_detections(dets)


def detect_barcodes(image) -> List[Detection]:
    """Locate and decode 1D barcodes (EAN/UPC/Code128/Code39/...). Runs pyzbar
    (ZBar) and OpenCV's barcode detector and merges overlapping hits. Returns a
    list of {"bbox", "kind", "payload"} dicts."""
    if image is None:
        return []
    dets: List[Detection] = []
    dets += _markers_pyzbar(image, qr_only=False)
    gray = _pil_to_gray_np(image)
    if gray is not None and _get_cv2() is not None:
        dets += _barcodes_cv2(gray)
    return _dedupe_detections(dets)


def detect_text_boxes(
    image,
    *,
    threshold: int = 160,
    min_glyph_h: int = 8,
    max_glyph_h: int = 55,
    max_glyph_w: int = 150,
    max_glyph_area: int = 6000,
    line_overlap: float = 0.6,
    gap_factor: float = 1.5,
    pad: int = 2,
    min_mark_area: int = 3,
    max_mark_w: int = 25,
) -> List[Tuple[float, float, float, float]]:
    """Box each run ("string") of characters so text regions are identified.

    Pipeline: connected ink components -> keep glyph-sized ones (drop rules,
    big blocks, and noise) -> group glyphs on a shared text line -> split a line
    into strings wherever the horizontal gap exceeds `gap_factor` x the line's
    glyph height (so word/label runs stay together but the wide blanks that mark
    fill-in fields break the run). Returns (x1, y1, x2, y2) per string.

    Sub-glyph-height components (a colon's dots, periods, commas, quotes,
    hyphens) are kept as "satellite" marks: each is attached to the nearest text
    line when it sits within that line's vertical band and close to one of its
    glyphs, so the string box grows to include the punctuation instead of
    dropping it. `min_mark_area` rejects single-pixel speckle and `max_mark_w`
    keeps marks punctuation-sized (so stray rule fragments are not absorbed).
    """
    if image is None:
        return []
    arr, binary, w, h = _binarize(image, threshold)
    if w == 0 or h == 0:
        return []

    glyphs: List[Tuple[int, int, int, int]] = []
    marks: List[Tuple[int, int, int, int]] = []
    for (x0, y0, x1, y1, count) in _find_components(arr, binary, w, h):
        bw = x1 - x0 + 1
        bh = y1 - y0 + 1
        if bh > max_glyph_h:
            continue                       # too tall to be text
        if bw > max_glyph_w:
            continue                       # too wide (a rule or merged blob)
        if bw * bh > max_glyph_area:
            continue                       # too big to be a glyph
        if bw >= 80 and bh <= 6:
            continue                       # long thin -> a rule
        if bh < min_glyph_h:
            # Below text height: a punctuation mark, not a glyph. Keep it as a
            # satellite if it is not speckle and is narrow enough to be punctuation.
            if count >= min_mark_area and bw <= max_mark_w:
                marks.append((x0, y0, x1, y1))
            continue
        glyphs.append((x0, y0, x1, y1))

    if not glyphs:
        return []

    heights = sorted(g[3] - g[1] + 1 for g in glyphs)
    med_h = heights[len(heights) // 2]

    # Group glyphs into text lines by vertical-center proximity.
    lines: List[Dict] = []
    for g in sorted(glyphs, key=lambda g: (g[1] + g[3]) / 2.0):
        cy = (g[1] + g[3]) / 2.0
        placed = False
        for L in lines:
            if abs(L["cy"] - cy) <= line_overlap * med_h:
                L["items"].append(g)
                L["cy"] = (L["cy"] * (len(L["items"]) - 1) + cy) / len(L["items"])
                placed = True
                break
        if not placed:
            lines.append({"cy": cy, "items": [g]})

    # Attach satellite punctuation marks to the nearest text line so the string
    # box grows to cover them. A mark joins a line only when it sits within the
    # line's vertical band (with slack) AND within a normal space of one of that
    # line's glyphs — distance is measured to the glyphs (snapshotted up front),
    # never to the line's full x-span, so a stray dot in a wide blank between two
    # strings is dropped rather than turned into its own box.
    if marks:
        v_slack = 0.6 * med_h
        h_gap = gap_factor * med_h
        for L in lines:
            base = list(L["items"])
            L["base"] = base
            # The band is a property of the line, not of the mark being tested:
            # computing it per (mark, line) pair made this O(marks x glyphs).
            L["lo"] = min(it[1] for it in base) - v_slack
            L["hi"] = max(it[3] for it in base) + v_slack
        for mx0, my0, mx1, my1 in marks:
            mcy = (my0 + my1) / 2.0
            best = None
            best_d = None
            for L in lines:
                if not (L["lo"] <= mcy <= L["hi"]):
                    continue
                base = L["base"]
                hd = min(
                    0.0 if (mx0 <= it[2] and mx1 >= it[0])
                    else (it[0] - mx1 if it[0] > mx1 else mx0 - it[2])
                    for it in base
                )
                if hd > h_gap:
                    continue
                d = hd + abs(mcy - L["cy"])
                if best_d is None or d < best_d:
                    best_d = d
                    best = L
            if best is not None:
                best["items"].append((mx0, my0, mx1, my1))

    # Within each line, break into strings at gaps wider than a normal space.
    gap = gap_factor * med_h
    boxes: List[Tuple[float, float, float, float]] = []
    for L in lines:
        items = sorted(L["items"], key=lambda g: g[0])
        run = [items[0]]
        for g in items[1:]:
            if g[0] - run[-1][2] <= gap:
                run.append(g)
            else:
                boxes.append(_string_bbox(run, pad))
                run = [g]
        boxes.append(_string_bbox(run, pad))
    return boxes


def _string_bbox(run: List[Tuple[int, int, int, int]], pad: int) -> Tuple[float, float, float, float]:
    return (
        float(min(g[0] for g in run) - pad),
        float(min(g[1] for g in run) - pad),
        float(max(g[2] for g in run) + pad),
        float(max(g[3] for g in run) + pad),
    )


def _spans_overlap(a0: float, a1: float, b0: float, b1: float, slack: float = 0.0) -> bool:
    return not (a1 < b0 - slack or a0 > b1 + slack)


def _snap_edge(value: float, lines: List[Line], tol: float,
               span_lo: float, span_hi: float, high_edge: bool = False) -> Tuple[float, bool]:
    """Snap a single field edge to the nearest line whose span overlaps the
    field's perpendicular extent [span_lo, span_hi]. Returns (value, snapped).

    Every line areaDef produces is a :class:`Line` (4 fields), but a plain
    3-tuple is accepted too: this is the seam CLI callers and scripts hand
    their own lines to, so the 4th field stays optional. The edge snaps to the
    rule boundary on the FIELD's side so the box hugs the rule without covering
    the ink: a high field edge (x2/y2, body on the low side) lands on the rule's
    low boundary (pos - half_thickness); a low field edge (x1/y1, body on the
    high side) lands on the rule's high boundary (pos + half_thickness).
    Zero-thickness lines (e.g. text edges) snap to the position regardless."""
    best = value
    best_d = tol
    snapped = False
    for line in lines:
        pos = line[0]
        half_t = line[3] if len(line) > 3 else 0.0
        target = (pos - half_t) if high_edge else (pos + half_t)
        d = abs(target - value)
        # Distance first: it rejects nearly every line with plain arithmetic,
        # where the span test costs a call. The two filters are independent, so
        # testing them in either order selects the same lines.
        if d > best_d:
            continue
        if not _spans_overlap(span_lo, span_hi, line[1], line[2], slack=tol):
            continue
        best_d = d
        best = target
        snapped = True
    return best, snapped


def _snap_bbox_to_lines(
    x1: float, y1: float, x2: float, y2: float,
    h_lines: List[Line],
    v_lines: List[Line],
    tol: float,
    keep_size: bool = False,
) -> Tuple[Tuple[float, float, float, float], bool]:
    """Snap a bbox to detected lines.

    keep_size=False: snap each of the four edges independently (place + size a
    new/resized field to its surrounding cell).
    keep_size=True: translate the whole bbox by the best single offset on each
    axis (used while moving so the field sticks to lines without resizing).
    """
    if keep_size:
        # Pick the smallest offset that snaps either edge to a line.
        def axis_offset(lo, hi, lines, span_lo, span_hi):
            best_off = 0.0
            best_d = tol
            for edge, high in ((lo, False), (hi, True)):
                pos, snapped = _snap_edge(edge, lines, tol, span_lo, span_hi, high_edge=high)
                if snapped and abs(pos - edge) <= best_d:
                    best_d = abs(pos - edge)
                    best_off = pos - edge
            return best_off
        dx = axis_offset(x1, x2, v_lines, y1, y2)
        dy = axis_offset(y1, y2, h_lines, x1, x2)
        if dx == 0.0 and dy == 0.0:
            return (x1, y1, x2, y2), False
        return (x1 + dx, y1 + dy, x2 + dx, y2 + dy), True

    nx1, s1 = _snap_edge(x1, v_lines, tol, y1, y2, high_edge=False)
    nx2, s2 = _snap_edge(x2, v_lines, tol, y1, y2, high_edge=True)
    ny1, s3 = _snap_edge(y1, h_lines, tol, x1, x2, high_edge=False)
    ny2, s4 = _snap_edge(y2, h_lines, tol, x1, x2, high_edge=True)
    return (nx1, ny1, nx2, ny2), (s1 or s2 or s3 or s4)


def _text_boxes_to_lines(
    text_boxes: List[Tuple[float, float, float, float]],
) -> Tuple[List[Line], List[Line]]:
    """Turn detected text-region boxes into snap lines so the green text edges
    behave like document rules: each box yields two vertical lines (its left and
    right edges, spanning its height) and two horizontal lines (top and bottom,
    spanning its width). Returns (h_lines, v_lines) in the same format as
    `detect_document_lines`."""
    h_lines: List[Line] = []
    v_lines: List[Line] = []
    for box in text_boxes:
        x0, y0, x1, y1 = box
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        v_lines.append(Line(x0, y0, y1))
        v_lines.append(Line(x1, y0, y1))
        h_lines.append(Line(y0, x0, x1))
        h_lines.append(Line(y1, x0, x1))
    return h_lines, v_lines


def _exclude_text_overlap(
    x1: float, y1: float, x2: float, y2: float,
    text_boxes: List[Tuple[float, float, float, float]],
    min_size: float = MIN_SHAPE_SIZE_PX,
    min_overlap: float = 0.5,
) -> Tuple[Tuple[float, float, float, float], bool]:
    """Clip the bbox so it shares no area with any text region.

    A field marks a blank to be filled in; it must not cover printed text. For
    each text box the field still overlaps, pull back the single field edge whose
    move costs the least (smallest penetration), provided the field stays at least
    `min_size` on that axis. Overlaps no larger than `min_overlap` px on either
    axis are treated as touching, not sharing area. Returns (bbox, changed)."""
    changed = False
    for box in text_boxes:
        tx0, ty0, tx1, ty1 = box
        if tx1 < tx0:
            tx0, tx1 = tx1, tx0
        if ty1 < ty0:
            ty0, ty1 = ty1, ty0
        ox = min(x2, tx1) - max(x1, tx0)
        oy = min(y2, ty1) - max(y1, ty0)
        if ox <= min_overlap or oy <= min_overlap:
            continue  # no meaningful shared area
        bw = x2 - x1
        bh = y2 - y1
        # Cost = AREA the field loses to clear the box on each side. Area (not raw
        # displacement) is the right objective: trimming a label's width off one
        # end costs far less than a thin full-width strip, so a field on the same
        # row as a label is shortened (height kept) while a field merely grazing a
        # box above/below is trimmed by that small overlap instead.
        cands = []  # (area_lost, side)
        if (x2 - tx1) >= min_size:
            cands.append(((tx1 - x1) * bh, "L"))   # field starts at text's right edge
        if (tx0 - x1) >= min_size:
            cands.append(((x2 - tx0) * bh, "R"))    # field ends at text's left edge
        if (y2 - ty1) >= min_size:
            cands.append(((ty1 - y1) * bw, "T"))    # field starts below the text
        if (ty0 - y1) >= min_size:
            cands.append(((y2 - ty0) * bw, "B"))    # field ends above the text
        if not cands:
            continue  # cannot clip without destroying the field; leave it
        cands.sort(key=lambda c: c[0])
        side = cands[0][1]
        if side == "L":
            x1 = tx1
        elif side == "R":
            x2 = tx0
        elif side == "T":
            y1 = ty1
        else:
            y2 = ty0
        changed = True
    return (x1, y1, x2, y2), changed


def snap_bbox_to_lines_and_text(
    x1: float, y1: float, x2: float, y2: float,
    h_lines: List[Line],
    v_lines: List[Line],
    text_boxes: List[Tuple[float, float, float, float]],
    tol: float,
    keep_size: bool = False,
    min_size: float = MIN_SHAPE_SIZE_PX,
) -> Tuple[Tuple[float, float, float, float], bool]:
    """Snap a bbox to document rules AND text-region edges, then (when sizing,
    i.e. keep_size=False) clip it out of any text region so the field never
    shares area with printed text. Text edges act as additional snap lines."""
    th, tv = _text_boxes_to_lines(text_boxes or [])
    return _snap_and_exclude(
        x1, y1, x2, y2,
        list(h_lines) + th, list(v_lines) + tv,
        text_boxes, tol, keep_size=keep_size, min_size=min_size,
    )


def _snap_and_exclude(
    x1: float, y1: float, x2: float, y2: float,
    h_lines: List[Line],
    v_lines: List[Line],
    text_boxes: List[Tuple[float, float, float, float]],
    tol: float,
    keep_size: bool = False,
    min_size: float = MIN_SHAPE_SIZE_PX,
) -> Tuple[Tuple[float, float, float, float], bool]:
    """snap_bbox_to_lines_and_text with the snap lines already derived.

    The GUI caches the derived lists per page (they change only when a detector
    runs), so re-deriving text/high-contrast edges on every mouse-move would be
    pure waste -- but the snapping itself must stay one implementation."""
    (nx1, ny1, nx2, ny2), snapped = _snap_bbox_to_lines(
        x1, y1, x2, y2, h_lines, v_lines, tol, keep_size=keep_size,
    )
    excluded = False
    if not keep_size and text_boxes:
        (nx1, ny1, nx2, ny2), excluded = _exclude_text_overlap(
            nx1, ny1, nx2, ny2, text_boxes, min_size=min_size
        )
    return (nx1, ny1, nx2, ny2), (snapped or excluded)


# ============================================================================
# Geometry normalization (pure; shared by the GUI and the CLI)
# ============================================================================

def _mean(vals: Iterable[float]) -> float:
    vals = list(vals)
    return sum(vals) / len(vals) if vals else 0.0


def _clamp_bbox_wh(x1: float, y1: float, x2: float, y2: float,
                   w: float, h: float, keep_size: bool) -> Tuple[float, float, float, float]:
    """Clamp a bbox to a w x h canvas. keep_size translates the box to stay in
    bounds; otherwise each edge is clamped independently."""
    if w <= 0 or h <= 0:
        return x1, y1, x2, y2
    w = float(w)
    h = float(h)
    if keep_size:
        bx1, bx2 = (x1, x2) if x1 <= x2 else (x2, x1)
        by1, by2 = (y1, y2) if y1 <= y2 else (y2, y1)
        bw = bx2 - bx1
        bh = by2 - by1
        bx1 = max(0.0, min(bx1, w - bw))
        by1 = max(0.0, min(by1, h - bh))
        return bx1, by1, bx1 + bw, by1 + bh
    return (
        max(0.0, min(float(x1), w)),
        max(0.0, min(float(y1), h)),
        max(0.0, min(float(x2), w)),
        max(0.0, min(float(y2), h)),
    )


def apply_snapped_bbox(shape, bbox, w: float = 0.0, h: float = 0.0,
                       min_size: float = MIN_SHAPE_SIZE_PX) -> bool:
    """Commit a recomputed bbox onto `shape` with the per-kind fix-ups.

    Snapping and aligning work on plain rectangles, so whatever they hand back
    has to be reconciled with the shape's kind before it is stored: a circle
    stays a circle (square bbox, grown to the larger axis so it never shrinks
    below what was asked for) and a chamfer never keeps a corner cut larger than
    half its box. A box that came back smaller than `min_size` on either axis is
    an over-aggressive snap, not an edit: it is rejected and nothing is written
    (pass min_size=0 to accept any size). `w`/`h`, when positive, clamp the
    result to the canvas. Returns whether the shape was changed. A multirect is
    never changed: writing back a reshaped envelope would leave it describing
    pieces that are no longer inside it (split it to reshape)."""
    x1, y1, x2, y2 = bbox
    if shape.kind == "multirect":
        return False
    if (x2 - x1) < min_size or (y2 - y1) < min_size:
        return False
    if shape.kind == "circle":
        size = max(x2 - x1, y2 - y1)
        if w > 0 and h > 0:
            size = min(size, w, h)   # a circle can't be bigger than the canvas
        x2, y2 = x1 + size, y1 + size
    if w > 0 and h > 0:
        # A circle translates back into bounds (keep_size) instead of having
        # each edge clamped: clamping one edge alone would leave a "circle"
        # with a non-square bbox, i.e. an ellipse whose radius no longer
        # matches its box (and exports as one).
        x1, y1, x2, y2 = _clamp_bbox_wh(x1, y1, x2, y2, w, h, shape.kind == "circle")
    shape.set_bbox(x1, y1, x2, y2)
    if shape.kind == "chamfer":
        shape.chamfer = min(float(shape.chamfer), (x2 - x1) / 2.0, (y2 - y1) / 2.0)
    return True


def normalize_rect_columns(shapes: List, w: float, h: float) -> int:
    """Align rectangles into columns by similar (normalized) left edge, in place.

    Returns how many rectangles were committed to a column (i.e. sat in a cluster
    of 2+), which is what the CLI reports as "Normalized N rectangle zone(s)"."""
    zones = [s for s in shapes if s.kind == "rect"]
    if len(zones) < 2 or w <= 0 or h <= 0:
        return 0
    w = float(w)
    h = float(h)
    tol = 0.02

    def normalized_bbox(shape):
        x1, y1, x2, y2 = shape.bbox()
        return (x1 / w, y1 / h, x2 / w, y2 / h)

    zone_tuples = [(s, *normalized_bbox(s)) for s in zones]
    clusters = _running_mean_clusters(zone_tuples, key=lambda item: item[1], tol=tol)

    changed = 0

    def commit(shape, nx1, ny1, nx2, ny2):
        cx1, cy1, cx2, cy2 = _clamp_bbox_wh(nx1 * w, ny1 * h, nx2 * w, ny2 * h, w, h, False)
        shape.set_bbox(cx1, cy1, cx2, cy2)

    for col in clusters:
        if len(col) < 2:
            continue
        lefts = [item[1] for item in col]
        rights = [item[3] for item in col]
        L = _mean(lefts)
        align_right = (max(rights) - min(rights)) <= tol
        if align_right:
            R = _mean(rights)
            for shape, x1, y1, _x2, y2 in col:
                commit(shape, L, y1, R, y2)
        else:
            for shape, x1, y1, x2, y2 in col:
                dx = L - x1
                commit(shape, x1 + dx, y1, x2 + dx, y2)
        changed += len(col)
    return changed


def normalize_circle_columns(shapes: List, w: float, h: float) -> None:
    """Align circles per column: snap centers to the column mean X and unify the
    radius to the column mean, in place."""
    circles = [s for s in shapes if s.kind == "circle"]
    if len(circles) < 2 or w <= 0:
        return
    W = float(w)
    tol_x = max(2.0, W * 0.02)

    def center_x(shp):
        x1, _y1, x2, _y2 = shp.bbox()
        return (x1 + x2) / 2.0

    for col in _running_mean_clusters(circles, key=center_x, tol=tol_x):
        if len(col) < 2:
            continue
        cxs, rs = [], []
        for s in col:
            x1, y1, x2, y2 = s.bbox()
            cxs.append((x1 + x2) / 2.0)
            rs.append(((x2 - x1) + (y2 - y1)) / 4.0)
        CX = _mean(cxs)
        R = _mean(rs)
        for s in col:
            x1, y1, x2, y2 = s.bbox()
            cy = (y1 + y2) / 2.0
            s.set_bbox(*_clamp_bbox_wh(CX - R, cy - R, CX + R, cy + R, w, h, True))


def normalize_chamfer_columns(shapes: List, w: float, h: float) -> None:
    """Align chamfer rects into columns (horizontal alignment only), in place."""
    chamfers = [s for s in shapes if s.kind == "chamfer"]
    if len(chamfers) < 2 or w <= 0 or h <= 0:
        return
    W = float(w)
    tol_x = max(2.0, W * 0.01)

    cvals = [float(s.chamfer) for s in chamfers]
    if len(cvals) >= 2 and (max(cvals) - min(cvals)) <= max(1.0, W * 0.001):
        C = _mean(cvals)
        for s in chamfers:
            s.chamfer = float(C)

    col_clusters = _running_mean_clusters(chamfers, key=lambda s: s.bbox()[0], tol=tol_x)
    for col in col_clusters:
        if len(col) < 2:
            continue
        lefts = [s.bbox()[0] for s in col]
        rights = [s.bbox()[2] for s in col]
        L = _mean(lefts)
        align_right = (max(rights) - min(rights)) <= tol_x
        if align_right:
            R = _mean(rights)
            for s in col:
                _x1, y1, _x2, y2 = s.bbox()
                s.set_bbox(L, y1, R, y2)
        else:
            for s in col:
                x1, y1, x2, y2 = s.bbox()
                dx = L - x1
                s.set_bbox(x1 + dx, y1, x2 + dx, y2)
        for s in col:
            x1, y1, x2, y2 = s.bbox()
            s.chamfer = min(float(s.chamfer), (x2 - x1) / 2.0, (y2 - y1) / 2.0)


def normalize_shapes(shapes: List, w: float, h: float) -> Dict[str, int]:
    """Run column normalization for every shape kind (rect/circle/chamfer), in
    place. Returns the per-kind counts that were eligible."""
    rects = sum(1 for s in shapes if s.kind == "rect")
    circles = sum(1 for s in shapes if s.kind == "circle")
    chamfers = sum(1 for s in shapes if s.kind == "chamfer")
    if rects >= 2:
        normalize_rect_columns(shapes, w, h)
    if circles >= 2:
        normalize_circle_columns(shapes, w, h)
    if chamfers >= 2:
        normalize_chamfer_columns(shapes, w, h)
    return {"rect": rects, "circle": circles, "chamfer": chamfers}


ALIGN_OPS = (
    "left", "right", "top", "bottom", "center_h", "center_v",
    "match_w", "match_h", "match_both",
)


def align_shapes(shapes: List, key, op: str) -> int:
    """Align/resize every shape in `shapes` relative to `key` (the anchor, which
    is never moved), in place — the Adobe Acrobat "Align" model.

    Edge ops (left/right/top/bottom) move a shape to share that edge with the
    key, preserving its size. center_h/center_v line up the horizontal/vertical
    centres on the key's centre. match_w/match_h/match_both resize a shape (from
    its top-left) to the key's width/height/both. Unknown ops are a no-op.

    Returns how many shapes were skipped: a multirect cannot be resized, so the
    match ops pass over it (callers report the count). Every other op is a pure
    translation and goes through Shape.translate, which keeps a multirect's
    pieces under its envelope instead of moving the envelope alone.
    """
    if op not in ALIGN_OPS:
        return 0
    skipped = 0
    kx1, ky1, kx2, ky2 = key.bbox()
    kw, kh = kx2 - kx1, ky2 - ky1
    kcx, kcy = (kx1 + kx2) / 2.0, (ky1 + ky2) / 2.0
    resizing = op in ("match_w", "match_h", "match_both")
    for s in shapes:
        if s is key:
            continue
        if resizing and s.kind == "multirect":
            skipped += 1
            continue
        ox1, oy1, x2, y2 = s.bbox()
        x1, y1 = ox1, oy1
        w, h = x2 - x1, y2 - y1
        if op == "left":
            x1, x2 = kx1, kx1 + w
        elif op == "right":
            x1, x2 = kx2 - w, kx2
        elif op == "top":
            y1, y2 = ky1, ky1 + h
        elif op == "bottom":
            y1, y2 = ky2 - h, ky2
        elif op == "center_h":
            x1, x2 = kcx - w / 2.0, kcx + w / 2.0
        elif op == "center_v":
            y1, y2 = kcy - h / 2.0, kcy + h / 2.0
        elif op == "match_w":
            x2 = x1 + kw
        elif op == "match_h":
            y2 = y1 + kh
        elif op == "match_both":
            x2, y2 = x1 + kw, y1 + kh
        if resizing:
            s.set_bbox(x1, y1, x2, y2)
        else:
            s.translate(x1 - ox1, y1 - oy1)
    return skipped


def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Write text to `path` atomically.

    The content is serialized to a temp file in the same directory, flushed and
    fsync'd, then os.replace'd onto the target so an existing file is never left
    half-overwritten if the process dies mid-write.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        # mkstemp() creates the temp file 0600 and os.replace carries that mode
        # onto the target, so every save silently made the project owner-only.
        # Restore the file's own mode, or the umask default for a new file.
        if hasattr(os, "fchmod"):
            try:
                mode = os.stat(path).st_mode & 0o7777
            except OSError:
                # ponytail: read-modify-write of the process umask; the save
                # paths are single-threaded. os.umask has no read-only form.
                umask = os.umask(0o022)
                os.umask(umask)
                mode = 0o666 & ~umask
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ensure_suffix(path: str, suffix: str) -> str:
    """Append `suffix` (e.g. '.json') if `path` has no real extension."""
    root, ext = os.path.splitext(path)
    if not ext or ext == ".":
        return root + suffix
    return path


# ============================================================================
# Theme Stylesheets
# ============================================================================

DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #2d2d2d;
    color: #ddd;
}
QMenuBar {
    background-color: #3a3a3a;
    color: #ddd;
    border-bottom: 1px solid #555;
}
QMenuBar::item {
    background-color: transparent;
    padding: 4px 10px;
}
QMenuBar::item:selected {
    background-color: #555;
}
QMenu {
    background-color: #3a3a3a;
    color: #ddd;
    border: 1px solid #555;
}
QMenu::item:selected {
    background-color: #555;
}
QMenu::separator {
    height: 1px;
    background-color: #555;
    margin: 4px 0;
}
QPushButton {
    background-color: #555;
    color: #ddd;
    border: 1px solid #666;
    padding: 5px 12px;
    border-radius: 3px;
}
QPushButton:hover {
    background-color: #666;
}
QPushButton:pressed {
    background-color: #444;
}
QLineEdit {
    background-color: #3a3a3a;
    color: #ddd;
    border: 1px solid #666;
    padding: 4px;
    border-radius: 3px;
}
QSpinBox {
    background-color: #3a3a3a;
    color: #ddd;
    border: 1px solid #666;
    padding: 4px;
    padding-right: 20px;
    border-radius: 3px;
}
QSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid #555;
    border-bottom: 1px solid #555;
    background-color: #4a4a4a;
}
QSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border-left: 1px solid #555;
    background-color: #4a4a4a;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: #5a5a5a;
}
QSpinBox::up-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #ccc;
}
QSpinBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #ccc;
}
QGroupBox {
    border: 2px solid #555;
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: #ddd;
}
/* Radio buttons - circle with inner dot when checked */
QRadioButton {
    color: #ddd;
    spacing: 8px;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 9px;
}
QRadioButton::indicator:unchecked {
    border: 2px solid #888;
    background-color: #3a3a3a;
}
QRadioButton::indicator:checked {
    border: 2px solid #4a9eff;
    background-color: #3a3a3a;
}
QRadioButton::indicator:checked {
    border: 5px solid #4a9eff;
    background-color: #4a9eff;
}
/* Checkboxes - square with visible checkmark */
QCheckBox {
    color: #ddd;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 3px;
}
QCheckBox::indicator:unchecked {
    border: 2px solid #888;
    background-color: #3a3a3a;
}
QCheckBox::indicator:checked {
    border: 2px solid #4a9eff;
    background-color: #4a9eff;
}
QListWidget {
    background-color: #3a3a3a;
    color: #ddd;
    border: 1px solid #666;
    border-radius: 3px;
}
QListWidget::item:selected {
    background-color: #4a6a8a;
}
QListWidget::item:hover {
    background-color: #444;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background-color: #3a3a3a;
    border: none;
}
QScrollBar:vertical {
    width: 14px;
}
QScrollBar:horizontal {
    height: 14px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background-color: #666;
    border-radius: 4px;
    min-height: 20px;
    min-width: 20px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background-color: #777;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
    width: 0;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}
QStatusBar {
    background-color: #3a3a3a;
    color: #ddd;
    border-top: 1px solid #555;
}
QGraphicsView {
    background-color: #2d2d2d;
    border: 2px solid #555;
}
QToolTip {
    background-color: #3a3a3a;
    color: #ddd;
    border: 1px solid #666;
    padding: 4px;
}
QLabel {
    color: #ccc;
}
"""

LIGHT_STYLE = """
QMainWindow, QWidget {
    background-color: #f0f0f0;
    color: #111;
}
QMenuBar {
    background-color: #f0f0f0;
    color: #111;
    border-bottom: 1px solid #ccc;
}
QMenuBar::item {
    background-color: transparent;
    padding: 4px 10px;
}
QMenuBar::item:selected {
    background-color: #ddd;
}
QMenu {
    background-color: #fff;
    color: #111;
    border: 1px solid #ccc;
}
QMenu::item:selected {
    background-color: #0078d7;
    color: #fff;
}
QMenu::separator {
    height: 1px;
    background-color: #ccc;
    margin: 4px 0;
}
QPushButton {
    background-color: #e0e0e0;
    color: #111;
    border: 1px solid #999;
    padding: 5px 12px;
    border-radius: 3px;
}
QPushButton:hover {
    background-color: #d0d0d0;
}
QPushButton:pressed {
    background-color: #c0c0c0;
}
QLineEdit {
    background-color: #fff;
    color: #111;
    border: 1px solid #999;
    padding: 4px;
    border-radius: 3px;
}
QSpinBox {
    background-color: #fff;
    color: #111;
    border: 1px solid #999;
    padding: 4px;
    padding-right: 20px;
    border-radius: 3px;
}
QSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    border-left: 1px solid #ccc;
    border-bottom: 1px solid #ccc;
    background-color: #e8e8e8;
}
QSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    border-left: 1px solid #ccc;
    background-color: #e8e8e8;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: #d8d8d8;
}
QSpinBox::up-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #444;
}
QSpinBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #444;
}
QGroupBox {
    border: 2px solid #bbb;
    border-radius: 4px;
    margin-top: 10px;
    padding-top: 10px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: #111;
}
/* Radio buttons - circle with inner dot when checked */
QRadioButton {
    color: #111;
    spacing: 8px;
}
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    border-radius: 9px;
}
QRadioButton::indicator:unchecked {
    border: 2px solid #666;
    background-color: #fff;
}
QRadioButton::indicator:checked {
    border: 5px solid #0078d7;
    background-color: #0078d7;
}
/* Checkboxes - square with visible checkmark */
QCheckBox {
    color: #111;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 3px;
}
QCheckBox::indicator:unchecked {
    border: 2px solid #666;
    background-color: #fff;
}
QCheckBox::indicator:checked {
    border: 2px solid #0078d7;
    background-color: #0078d7;
}
QListWidget {
    background-color: #fff;
    color: #111;
    border: 1px solid #999;
    border-radius: 3px;
}
QListWidget::item:selected {
    background-color: #0078d7;
    color: #fff;
}
QListWidget::item:hover {
    background-color: #e8e8e8;
}
QScrollBar:vertical, QScrollBar:horizontal {
    background-color: #e0e0e0;
    border: none;
}
QScrollBar:vertical {
    width: 14px;
}
QScrollBar:horizontal {
    height: 14px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background-color: #b0b0b0;
    border-radius: 4px;
    min-height: 20px;
    min-width: 20px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background-color: #909090;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
    width: 0;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}
QStatusBar {
    background-color: #f0f0f0;
    color: #111;
    border-top: 1px solid #ccc;
}
QGraphicsView {
    background-color: #f8f8f8;
    border: 2px solid #bbb;
}
QToolTip {
    background-color: #fff;
    color: #111;
    border: 1px solid #999;
    padding: 4px;
}
QLabel {
    color: #333;
}
"""


def detect_system_theme() -> str:
    """Detect the OS light/dark preference. Returns 'light' or 'dark'.

    Supports Windows (registry), macOS (`defaults`), and Linux desktops that
    expose a freedesktop/GNOME color-scheme hint. Falls back to 'dark'.
    """
    platform = sys.platform
    try:
        if platform.startswith("win"):
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            )
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if value == 1 else "dark"

        import subprocess

        if platform == "darwin":
            # `AppleInterfaceStyle` is set to "Dark" only in dark mode; the key
            # is absent (non-zero exit) in light mode.
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
            )
            return "dark" if "dark" in result.stdout.strip().lower() else "light"

        # Linux / other: query the freedesktop color-scheme portal via gsettings.
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2,
        )
        out = result.stdout.strip().lower()
        if out:
            if "dark" in out:
                return "dark"
            if "light" in out or "default" in out:
                return "light"
    except Exception:
        pass
    return "dark"


# ============================================================================
# Data Classes
# ============================================================================

def _sorted_rect(rect) -> Tuple[float, float, float, float]:
    """A 4-number box as floats with x1 <= x2 and y1 <= y2."""
    x1, y1, x2, y2 = (float(v) for v in rect)
    if x1 > x2:
        x1, x2 = x2, x1
    if y1 > y2:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


def multirect_envelope(rects: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    """The bounding box of a multirect's pieces (min/max over already-sorted pieces)."""
    if not rects:
        raise ValueError("a multirect needs at least one piece")
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


@dataclass
class Shape:
    sid: int
    kind: str  # "rect" | "circle" | "chamfer" | "multirect"
    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    chamfer: float = 0.0
    page: int = 0
    # Pieces of a multirect (>= 2, each sorted); None for every other kind. The
    # shape's own x1/y1/x2/y2 stay the derived envelope so that every bbox-reading
    # consumer (export, CSV, align, screenshot) keeps working unchanged.
    rects: Optional[List[Tuple[float, float, float, float]]] = None

    def set_rects(self, rects: Sequence[Sequence[float]]) -> None:
        """Store the pieces (each sorted) and re-sync the envelope coords."""
        pieces = [_sorted_rect(r) for r in rects]
        if not all(math.isfinite(v) for p in pieces for v in p):
            raise ValueError(f"multirect pieces must be finite, got {pieces}")
        self.rects = pieces
        self.x1, self.y1, self.x2, self.y2 = multirect_envelope(pieces)

    def translate(self, dx: float, dy: float) -> None:
        """Move the whole shape by (dx, dy) — every piece of a multirect, so the
        envelope can never drift away from the geometry it describes."""
        if self.rects:
            self.set_rects([(x1 + dx, y1 + dy, x2 + dx, y2 + dy)
                            for x1, y1, x2, y2 in self.rects])
        else:
            x1, y1, x2, y2 = self.bbox()
            self.set_bbox(x1 + dx, y1 + dy, x2 + dx, y2 + dy)

    def clamp_into(self, w: float, h: float) -> None:
        """Translate the whole shape back inside a w x h canvas, at full size.

        The keep-size clamp every mover already uses, expressed as a translation
        so it works on a multirect too: `apply_snapped_bbox` refuses those (it
        commits a bbox, which would leave the envelope describing pieces that
        moved with it), leaving nothing to clamp an aligned merged area."""
        x1, y1, x2, y2 = self.bbox()
        nx1, ny1, _nx2, _ny2 = _clamp_bbox_wh(x1, y1, x2, y2, w, h, keep_size=True)
        if (nx1, ny1) != (x1, y1):
            self.translate(nx1 - x1, ny1 - y1)

    def bbox(self) -> Tuple[float, float, float, float]:
        x1, x2 = (self.x1, self.x2) if self.x1 <= self.x2 else (self.x2, self.x1)
        y1, y2 = (self.y1, self.y2) if self.y1 <= self.y2 else (self.y2, self.y1)
        return x1, y1, x2, y2

    def set_bbox(self, x1: float, y1: float, x2: float, y2: float) -> None:
        vals = (float(x1), float(y1), float(x2), float(y2))
        if not all(math.isfinite(v) for v in vals):
            raise ValueError(f"bbox coordinates must be finite, got {vals}")
        self.x1, self.y1, self.x2, self.y2 = vals

    def as_export_dict(self) -> Dict:
        x1, y1, x2, y2 = self.bbox()
        d = {
            "id": self.sid,
            "name": self.name,
            "kind": self.kind,
            "bbox": [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)],
            "page": self.page,
        }
        if self.kind == "circle":
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            r = (x2 - x1) / 2.0
            d["center"] = [round(cx, 3), round(cy, 3)]
            d["radius"] = round(r, 3)
        if self.kind == "chamfer":
            # A chamfer can never exceed half the box; clamp so exported JSON
            # never carries a geometrically-invalid value (e.g. from hand-edited
            # or externally-produced input loaded via load_json).
            max_c = max(0.0, min((x2 - x1) / 2.0, (y2 - y1) / 2.0))
            d["chamfer"] = round(min(max(0.0, float(self.chamfer)), max_c), 3)
        if self.kind == "multirect" and self.rects:
            d["rects"] = [[round(v, 3) for v in r] for r in self.rects]
        return d

    def clone(self) -> "Shape":
        return Shape(
            sid=self.sid, kind=self.kind, name=self.name,
            x1=self.x1, y1=self.y1, x2=self.x2, y2=self.y2,
            chamfer=self.chamfer,
            page=self.page,
            rects=list(self.rects) if self.rects is not None else None,
        )


def merge_shapes(shapes: List[Shape], sid: int) -> Shape:
    """Fuse 2+ rects/multirects into one multirect area (pure: no Qt, no Project).

    The pieces of a merged multirect are flattened in, so merging never nests.
    The new shape takes the first-selected shape's name and page; its coords are
    the envelope of all pieces."""
    if len(shapes) < 2:
        raise ValueError(f"merge needs at least 2 shapes, got {len(shapes)}")
    bad = sorted({s.kind for s in shapes} - {"rect", "multirect"})
    if bad:
        raise ValueError(f"cannot merge {', '.join(bad)} shapes; only rect/multirect")
    pages = sorted({s.page for s in shapes})
    if len(pages) > 1:
        raise ValueError(f"cannot merge shapes spanning pages {pages}")
    pieces: List[Tuple[float, float, float, float]] = []
    for s in shapes:
        pieces.extend(s.rects if s.kind == "multirect" and s.rects else [s.bbox()])
    merged = Shape(sid, "multirect", shapes[0].name, 0.0, 0.0, 0.0, 0.0,
                   page=shapes[0].page)
    merged.set_rects(pieces)
    return merged


def split_shape(shape: Shape, first_sid: int) -> List[Shape]:
    """Break a multirect back into one plain rect per piece (pure), named
    `<name>_1..n` and numbered from `first_sid`."""
    if shape.kind != "multirect":
        raise ValueError(f"only a multirect can be split, got {shape.kind!r}")
    return [Shape(first_sid + i, "rect", f"{shape.name}_{i + 1}", x1, y1, x2, y2,
                  page=shape.page)
            for i, (x1, y1, x2, y2) in enumerate(shape.rects or [])]


def splice_shapes(shapes: List[Shape], sources: List[Shape],
                  replacements: List[Shape]) -> List[Shape]:
    """`shapes` with every shape in `sources` swapped out for `replacements`,
    placed at the earliest source's slot. Shared by merge and split, on both the
    CLI (Project) and the GUI side, so a merged/split area keeps its position in
    the areas list instead of jumping to the end."""
    doomed = {id(s) for s in sources}
    out: List[Shape] = []
    placed = False
    for s in shapes:
        if id(s) not in doomed:
            out.append(s)
        elif not placed:
            out.extend(replacements)
            placed = True
    return out


def _clean_name(value: str) -> str:
    """Drop control/non-printable characters from a name that came from outside
    (a project file, a decoded barcode payload). Such characters break the list
    widget, the CSV, and the JSON round trip while being invisible to the user;
    spaces and any printable Unicode are kept as typed."""
    return "".join(ch for ch in value if ch.isprintable())


def _valid_rect_pieces(raw) -> List[Tuple[float, float, float, float]]:
    """The usable pieces of a multirect's `rects` value, each validated exactly
    like a bbox (4 finite numbers) and sorted. Anything else is dropped."""
    pieces = []
    if not isinstance(raw, (list, tuple)):
        return pieces
    for piece in raw:
        if not isinstance(piece, (list, tuple)) or len(piece) != 4:
            continue
        try:
            vals = _sorted_rect(piece)
        except (TypeError, ValueError, OverflowError):
            continue
        if all(math.isfinite(v) for v in vals):
            pieces.append(vals)
    return pieces


def _parse_shapes(raw_shapes: List) -> Tuple[List[Shape], int, int]:
    """Validate and coerce a raw shapes list. Returns (shapes, skipped_count,
    coerced_kind_count). Malformed entries are skipped rather than raising."""
    parsed: List[Shape] = []
    skipped = 0
    coerced_kinds = 0
    for item in raw_shapes:
        if not isinstance(item, dict):
            skipped += 1
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            skipped += 1
            continue
        try:
            # OverflowError: json accepts int literals of any size, and
            # float(10**400) raises rather than returning inf.
            x1, y1, x2, y2 = [float(v) for v in bbox]
        except (TypeError, ValueError, OverflowError):
            skipped += 1
            continue
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
            skipped += 1
            continue

        def _coerce(value, caster, default):
            try:
                return caster(value)
            except (TypeError, ValueError, OverflowError):
                # json.loads accepts Infinity/-Infinity, and int(inf) raises
                # OverflowError -- which used to escape into the Qt slot.
                return default

        sid = _coerce(item.get("id", 0), int, 0)
        page = _coerce(item.get("page", 0), int, 0)
        cham = _coerce(item.get("chamfer", 0.0), float, 0.0)
        if not math.isfinite(cham):
            cham = 0.0
        raw_kind = item.get("kind")
        kind = str(raw_kind) if raw_kind is not None else "rect"
        if kind not in VALID_KINDS:
            kind = "rect"
            coerced_kinds += 1
        pieces = _valid_rect_pieces(item.get("rects")) if kind == "multirect" else []
        if kind == "multirect" and len(pieces) < 2:
            # A multirect IS its pieces; with fewer than two usable ones there is
            # no area to restore, so it is malformed like a bad bbox.
            skipped += 1
            continue
        raw_name = item.get("name")
        name = _clean_name(str(raw_name)) if raw_name is not None else f"area_{sid}"
        shp = Shape(sid, kind, name, x1, y1, x2, y2, chamfer=cham, page=page)
        if pieces:
            shp.set_rects(pieces)   # the stored bbox is ignored: envelope is derived
        parsed.append(shp)

    _dedupe_shape_ids(parsed)
    return parsed, skipped, coerced_kinds


def _dedupe_shape_ids(shapes: List) -> None:
    """Guarantee unique (page, sid) identity, renumbering in place.

    CLI mutators address a shape by (page, sid), so the pair has to be unique.
    A missing/invalid id parses to 0; multiple such shapes (or any collision) on
    the same page would otherwise be indistinguishable. Cross-page sid repeats
    (the normal multi-page case) are preserved. Idempotent, so a caller that
    moves shapes between pages can re-run it."""
    seen = set()
    max_sid = max((s.sid for s in shapes), default=0)
    for s in shapes:
        if s.sid <= 0 or (s.page, s.sid) in seen:
            max_sid += 1
            s.sid = max_sid
        seen.add((s.page, s.sid))


@dataclass
class AppState:
    shapes: List[Shape] = field(default_factory=list)
    next_sid: int = 1
    selected_sid: Optional[int] = None


class UndoManager:
    def __init__(self, max_history: int = MAX_UNDO_HISTORY) -> None:
        self.max_history = max_history
        self.undo_stack: List[AppState] = []
        self.redo_stack: List[AppState] = []

    # Every method here takes OWNERSHIP of the state it is handed and gives
    # away ownership of the state it returns. Callers already pass a freshly
    # captured snapshot (AnnotatorWindow._snapshot_state), so cloning again
    # inside meant deep-copying every shape two or three times per edit.

    def save_state(self, state: AppState) -> None:
        self.undo_stack.append(state)
        if len(self.undo_stack) > self.max_history:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self, current_state: AppState) -> Optional[AppState]:
        if not self.undo_stack:
            return None
        self.redo_stack.append(current_state)
        return self.undo_stack.pop()

    def redo(self, current_state: AppState) -> Optional[AppState]:
        if not self.redo_stack:
            return None
        self.undo_stack.append(current_state)
        return self.redo_stack.pop()

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()


# ============================================================================
# Canvas View
# ============================================================================

class CanvasView(QGraphicsView):
    """Custom QGraphicsView for image display and shape drawing."""

    def __init__(self, parent: "AnnotatorWindow") -> None:
        super().__init__()
        self.main_window = parent
        self.scene = QGraphicsScene()
        self.setScene(self.scene)

        # Enable scroll bars
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Image
        self.pixmap_item: Optional[QGraphicsPixmapItem] = None
        self.image_size = (0, 0)

        # Interaction state
        self.drag_mode: Optional[str] = None  # "draw" | "move" | "resize" | "pan"
        self.drag_start: QPointF = QPointF()
        self.drag_start_bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
        self.active_handle: Optional[str] = None
        self.pan_start: Optional[QPointF] = None
        self._pending_undo: Optional[AppState] = None  # deferred until a real drag

        # Temp drawing
        self.temp_item: Optional[QGraphicsItem] = None
        self.marquee_item: Optional[QGraphicsItem] = None

        # Persistent per-shape graphics, keyed by shape id (sid).
        self._shape_items: Dict[int, dict] = {}

        # Enable mouse tracking
        self.setMouseTracking(True)

        # Rendering hints
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Pointer-anchored zoom is done manually in zoom_at_point (so it works
        # for the wheel AND the menu, and is independent of the live cursor), so
        # the view itself must NOT also anchor — use NoAnchor for transforms and
        # keep the centre stable on resize.
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)

    def set_image(self, pixmap: QPixmap) -> None:
        """Set the background image."""
        self.scene.clear()
        # scene.clear() deleted every item, so drop the now-dangling refs before
        # anything can read them.
        self._shape_items = {}
        self.pixmap_item = self.scene.addPixmap(pixmap)
        w, h = pixmap.width(), pixmap.height()
        self.image_size = (w, h)
        # Give the scene a one-image-size margin on every side so the view can
        # scroll past the image edges. Without this, zooming out near an edge
        # cannot keep the cursor point fixed (the scroll clamps at the edge).
        self.setSceneRect(-w, -h, 3 * w, 3 * h)
        self.main_window.redraw_shapes()

    def fit_to_view(self) -> None:
        """Fit the image to the view."""
        if self.pixmap_item:
            self.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_at_point(self, factor: float, viewport_pos: QPointF) -> None:
        """Zoom by `factor`, keeping the scene point under `viewport_pos` (a
        position in viewport pixels) fixed — pointer-anchored zoom. Works for the
        wheel (event position) and the menu (viewport centre) alike, and does not
        depend on the live cursor, so it is deterministic and testable."""
        current = self.transform().m11()
        target = current * factor
        if target > MAX_ZOOM:
            factor = MAX_ZOOM / current
        elif target < MIN_ZOOM:
            factor = MIN_ZOOM / current
        if abs(factor - 1.0) < 1e-9:
            return
        old = self.mapToScene(viewport_pos.toPoint())
        self.scale(factor, factor)
        new = self.mapToScene(viewport_pos.toPoint())
        delta = new - old
        self.translate(delta.x(), delta.y())

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Handle mouse wheel for zooming."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.1 if event.angleDelta().y() > 0 else 1 / 1.1
            self.zoom_at_point(factor, event.position())
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press."""
        if not self.pixmap_item:
            return

        scene_pos = self.mapToScene(event.pos())
        mw = self.main_window

        # Middle button - pan
        if event.button() == Qt.MouseButton.MiddleButton:
            self.drag_mode = "pan"
            self.pan_start = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        tool = mw.current_tool

        # Check for handle hit first
        if mw.selected_sid is not None:
            handle = self._hit_handle(scene_pos)
            if handle:
                # Capture a pre-edit snapshot but only commit it to the undo
                # stack on release if the shape actually changed (no spurious
                # undo entries / dirty flag from a click that doesn't drag).
                self._pending_undo = mw._snapshot_state()
                self.drag_mode = "resize"
                self.active_handle = handle
                shp = mw.get_shape(mw.selected_sid)
                if shp:
                    self.drag_start_bbox = shp.bbox()
                self.drag_start = scene_pos
                return

        # Check for shape hit
        sid = self._hit_shape(scene_pos)
        if sid is not None:
            # Ctrl+click adds/removes the shape from the multi-selection (the
            # most recent becomes the key object) instead of moving it.
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                mw.toggle_selection(sid)
                return
            mw.set_selected(sid)
            self._pending_undo = mw._snapshot_state()
            self.drag_mode = "move"
            shp = mw.get_shape(sid)
            if shp:
                self.drag_start_bbox = shp.bbox()
            self.drag_start = scene_pos
            return

        # Empty space
        if tool == "select":
            # Shift+drag draws a marquee that selects every shape it touches.
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.drag_mode = "marquee"
                self.drag_start = scene_pos
                self._create_marquee_item(scene_pos)
                return
            mw.set_selected(None)
            return

        # Drawing mode
        mw.set_selected(None)
        self.drag_mode = "draw"
        self.drag_start = scene_pos
        self._create_temp_item(scene_pos, tool)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move."""
        scene_pos = self.mapToScene(event.pos())
        mw = self.main_window

        # Update status bar coordinates
        if self.pixmap_item:
            x = max(0, min(scene_pos.x(), self.image_size[0]))
            y = max(0, min(scene_pos.y(), self.image_size[1]))
            mw.coords_label.setText(f"X: {x:.1f}  Y: {y:.1f}")

        if self.drag_mode == "pan" and self.pan_start:
            delta = event.pos() - self.pan_start
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            self.pan_start = event.pos()
            return

        if self.drag_mode == "draw" and self.temp_item:
            self._update_temp_item(scene_pos)
            return

        if self.drag_mode == "marquee" and self.marquee_item is not None:
            x1, y1 = self.drag_start.x(), self.drag_start.y()
            self.marquee_item.setRect(min(x1, scene_pos.x()), min(y1, scene_pos.y()),
                                      abs(scene_pos.x() - x1), abs(scene_pos.y() - y1))
            return

        if self.drag_mode == "move" and mw.selected_sid is not None:
            shp = mw.get_shape(mw.selected_sid)
            if shp:
                dx = scene_pos.x() - self.drag_start.x()
                dy = scene_pos.y() - self.drag_start.y()
                ox1, oy1, ox2, oy2 = self.drag_start_bbox
                bx1, by1, bx2, by2 = ox1 + dx, oy1 + dy, ox2 + dx, oy2 + dy
                # Live-snap the moving box to nearby lines (keep its size).
                bx1, by1, bx2, by2 = mw.maybe_snap_live(bx1, by1, bx2, by2, keep_size=True)
                # A move is a translation of the whole unit, so it goes through
                # translate(): a multirect's pieces come with its envelope.
                cx1, cy1, _cx2, _cy2 = shp.bbox()
                shp.translate(bx1 - cx1, by1 - cy1)
                # Update only the dragged shape's items, not the whole scene.
                mw.update_active_graphics(shp)
            return

        if self.drag_mode == "resize" and mw.selected_sid is not None:
            shp = mw.get_shape(mw.selected_sid)
            if shp and self.active_handle:
                self._do_resize(shp, scene_pos)
                if mw.snap_to_lines and shp.kind != "circle":
                    bx1, by1, bx2, by2 = mw.maybe_snap_live(*shp.bbox(), keep_size=False)
                    shp.set_bbox(bx1, by1, bx2, by2)
                mw.update_active_graphics(shp)
            return

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release."""
        mw = self.main_window

        if event.button() == Qt.MouseButton.MiddleButton:
            if self.drag_mode == "pan":
                self.drag_mode = None
            self.pan_start = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self.drag_mode == "marquee":
            scene_pos = self.mapToScene(event.pos())
            if self.marquee_item is not None:
                self.scene.removeItem(self.marquee_item)
                self.marquee_item = None
            mw.select_in_region(self.drag_start.x(), self.drag_start.y(),
                                scene_pos.x(), scene_pos.y())
            self.drag_mode = None
            return

        if self.drag_mode == "draw":
            self._finish_drawing(event)
        elif self.drag_mode in ("move", "resize"):
            # Commit the deferred undo snapshot only if geometry actually changed.
            shp = mw.get_shape(mw.selected_sid) if mw.selected_sid is not None else None
            changed = shp is not None and shp.bbox() != self.drag_start_bbox
            if changed and self._pending_undo is not None:
                mw.commit_undo_snapshot(self._pending_undo)
            self._pending_undo = None
            if shp is not None:
                mw._populate_geometry(shp)
            # The shape set did not change, so re-sync the existing items rather
            # than rebuilding the scene on every click and drag release.
            mw.restyle_selection()

        # Defensive: drop any leftover temp drawing item.
        if self.temp_item is not None:
            self.scene.removeItem(self.temp_item)
            self.temp_item = None

        self.drag_mode = None
        self.active_handle = None

    def contextMenuEvent(self, event) -> None:
        """Right-click on a shape selects it and opens the shape actions menu."""
        if not self.pixmap_item:
            return
        scene_pos = self.mapToScene(event.pos())
        sid = self._hit_shape(scene_pos)
        if sid is None:
            return
        self.main_window._select_for_context_menu(sid)
        self.main_window._popup_shape_menu(event.globalPos())

    def _hit_handle(self, pos: QPointF) -> Optional[str]:
        """Check if pos hits a resize handle."""
        mw = self.main_window
        if mw.selected_sid is None:
            return None
        shp = mw.get_shape(mw.selected_sid)
        if not shp:
            return None
        if shp.kind == "multirect":
            return None   # no handles are drawn for a merged area (Split to reshape)

        x1, y1, x2, y2 = shp.bbox()
        handles = {
            "nw": (x1, y1), "ne": (x2, y1),
            "se": (x2, y2), "sw": (x1, y2)
        }
        # The handle is drawn at a fixed on-screen size, so its grab tolerance
        # must be expressed in scene units that shrink as the view zooms in.
        scale = abs(self.transform().m11()) or 1.0
        tol = 8.0 / scale
        for name, (hx, hy) in handles.items():
            if abs(pos.x() - hx) < tol and abs(pos.y() - hy) < tol:
                return name
        return None

    def _hit_shape(self, pos: QPointF) -> Optional[int]:
        """Check if pos hits a shape, honoring the actual shape geometry so that
        empty corners of circles/chamfers do not register as hits."""
        mw = self.main_window
        for shp in reversed(mw.shapes):
            x1, y1, x2, y2 = shp.bbox()
            if not (x1 <= pos.x() <= x2 and y1 <= pos.y() <= y2):
                continue
            if shp.kind == "circle":
                rx = (x2 - x1) / 2.0
                ry = (y2 - y1) / 2.0
                if rx <= 0 or ry <= 0:
                    continue
                cx = (x1 + x2) / 2.0
                cy = (y1 + y2) / 2.0
                if ((pos.x() - cx) / rx) ** 2 + ((pos.y() - cy) / ry) ** 2 <= 1.0:
                    return shp.sid
            elif shp.kind == "chamfer":
                poly = self._make_chamfer_polygon(x1, y1, x2, y2, shp.chamfer)
                if poly.containsPoint(pos, Qt.FillRule.OddEvenFill):
                    return shp.sid
            elif shp.kind == "multirect":
                # Point-in-any-piece: a click in the notch of an L is inside the
                # envelope but hits nothing.
                if any(px1 <= pos.x() <= px2 and py1 <= pos.y() <= py2
                       for px1, py1, px2, py2 in (shp.rects or [])):
                    return shp.sid
            else:
                return shp.sid
        return None

    def _create_temp_item(self, start: QPointF, tool: str) -> None:
        """Create temporary item for drawing."""
        mw = self.main_window
        pen = QPen(QColor("#00aa00" if mw.current_theme == "light" else "#44dd44"))
        pen.setWidth(2)

        if tool == "rect":
            self.temp_item = self.scene.addRect(
                start.x(), start.y(), 1, 1, pen
            )
        elif tool == "circle":
            self.temp_item = self.scene.addEllipse(
                start.x(), start.y(), 1, 1, pen
            )
        elif tool == "chamfer":
            self.temp_item = self.scene.addPolygon(
                QPolygonF(), pen
            )

    def _create_marquee_item(self, start: QPointF) -> None:
        """Create the dashed rubber-band rectangle for a Shift+drag selection."""
        mw = self.main_window
        pen = QPen(QColor("#1166cc" if mw.current_theme == "light" else "#4499ff"))
        pen.setWidth(0)
        pen.setStyle(Qt.PenStyle.DashLine)
        self.marquee_item = self.scene.addRect(start.x(), start.y(), 1, 1, pen)

    def _update_temp_item(self, pos: QPointF) -> None:
        """Update temporary item during drawing."""
        mw = self.main_window
        x1, y1 = self.drag_start.x(), self.drag_start.y()
        x2, y2 = pos.x(), pos.y()

        tool = mw.current_tool

        if tool == "circle":
            dx = x2 - x1
            dy = y2 - y1
            size = max(abs(dx), abs(dy))
            x2 = x1 + (size if dx >= 0 else -size)
            y2 = y1 + (size if dy >= 0 else -size)

        rect = QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))

        if isinstance(self.temp_item, QGraphicsRectItem):
            self.temp_item.setRect(rect)
        elif isinstance(self.temp_item, QGraphicsEllipseItem):
            self.temp_item.setRect(rect)
        elif isinstance(self.temp_item, QGraphicsPolygonItem):
            chamfer = mw.chamfer_spin.value()
            polygon = self._make_chamfer_polygon(
                rect.x(), rect.y(),
                rect.x() + rect.width(), rect.y() + rect.height(),
                chamfer
            )
            self.temp_item.setPolygon(polygon)

    def _finish_drawing(self, event: QMouseEvent) -> None:
        """Finish drawing and create shape."""
        mw = self.main_window

        if self.temp_item:
            self.scene.removeItem(self.temp_item)
            self.temp_item = None

        # Use the release event's position (same source that drove the preview)
        # rather than the global cursor, which can drift under HiDPI scaling.
        pos = self.mapToScene(event.pos())
        x1, y1 = self.drag_start.x(), self.drag_start.y()
        x2, y2 = pos.x(), pos.y()

        tool = mw.current_tool

        if tool == "circle":
            dx = x2 - x1
            dy = y2 - y1
            size = max(abs(dx), abs(dy))
            x2 = x1 + (size if dx >= 0 else -size)
            y2 = y1 + (size if dy >= 0 else -size)

        # Normalize
        bx1, bx2 = (x1, x2) if x1 <= x2 else (x2, x1)
        by1, by2 = (y1, y2) if y1 <= y2 else (y2, y1)

        # Snap the new box to detected document lines (place + size to the cell).
        # Circles keep their square constraint, so they snap as a translation.
        if mw.snap_to_lines:
            (bx1, by1, bx2, by2), _ = mw._snap_bbox(
                bx1, by1, bx2, by2, keep_size=(tool == "circle")
            )

        if (bx2 - bx1) < MIN_SHAPE_SIZE_PX or (by2 - by1) < MIN_SHAPE_SIZE_PX:
            return

        mw.save_for_undo()
        name = mw.name_edit.text().strip() or f"area_{mw.next_sid}"

        if tool == "rect":
            shp = Shape(
                mw.next_sid, "rect", name, bx1, by1, bx2, by2,
                page=mw._current_pdf_page if mw._is_pdf else 0
            )
        elif tool == "circle":
            size = max(bx2 - bx1, by2 - by1)
            shp = Shape(
                mw.next_sid, "circle", name, bx1, by1, bx1 + size, by1 + size,
                page=mw._current_pdf_page if mw._is_pdf else 0
            )
        elif tool == "chamfer":
            cham = float(mw.chamfer_spin.value())
            cham = max(0.0, min(cham, (bx2 - bx1) / 2.0, (by2 - by1) / 2.0))
            shp = Shape(
                mw.next_sid, "chamfer", name, bx1, by1, bx2, by2,
                chamfer=cham, page=mw._current_pdf_page if mw._is_pdf else 0
            )
        else:
            shp = Shape(
                mw.next_sid, "rect", name, bx1, by1, bx2, by2,
                page=mw._current_pdf_page if mw._is_pdf else 0
            )

        mw.shapes.append(shp)
        mw.next_sid += 1
        mw.update_list()
        mw.set_selected(shp.sid)
        mw.status_bar.showMessage(f"Created {shp.kind}: {shp.name}")

    def _do_resize(self, shp: Shape, pos: QPointF) -> None:
        """Resize shape by handle."""
        ox1, oy1, ox2, oy2 = self.drag_start_bbox
        x1, y1, x2, y2 = ox1, oy1, ox2, oy2

        h = self.active_handle
        if h == "nw":
            x1, y1 = pos.x(), pos.y()
        elif h == "ne":
            x2, y1 = pos.x(), pos.y()
        elif h == "se":
            x2, y2 = pos.x(), pos.y()
        elif h == "sw":
            x1, y2 = pos.x(), pos.y()

        if shp.kind == "circle":
            fx, fy = {"nw": (ox2, oy2), "ne": (ox1, oy2), "se": (ox1, oy1), "sw": (ox2, oy1)}[h]
            dx = pos.x() - fx
            dy = pos.y() - fy
            size = max(abs(dx), abs(dy))
            sx = 1.0 if dx >= 0 else -1.0
            sy = 1.0 if dy >= 0 else -1.0
            x1, y1 = fx, fy
            x2, y2 = fx + sx * size, fy + sy * size

        shp.set_bbox(x1, y1, x2, y2)

        if shp.kind == "chamfer":
            bx1, by1, bx2, by2 = shp.bbox()
            shp.chamfer = min(float(shp.chamfer), (bx2 - bx1) / 2.0, (by2 - by1) / 2.0)

    def _make_chamfer_polygon(self, x1: float, y1: float, x2: float, y2: float, chamfer: float) -> QPolygonF:
        """Create chamfer rectangle polygon."""
        ix1, ix2 = (x1, x2) if x1 <= x2 else (x2, x1)
        iy1, iy2 = (y1, y2) if y1 <= y2 else (y2, y1)

        w = ix2 - ix1
        h = iy2 - iy1
        c = max(0.0, float(chamfer))
        c = min(c, w / 2.0, h / 2.0)

        points = [
            QPointF(ix1 + c, iy1),
            QPointF(ix2 - c, iy1),
            QPointF(ix2, iy1 + c),
            QPointF(ix2, iy2 - c),
            QPointF(ix2 - c, iy2),
            QPointF(ix1 + c, iy2),
            QPointF(ix1, iy2 - c),
            QPointF(ix1, iy1 + c),
        ]
        return QPolygonF(points)

    def _make_multirect_path(self, shp: Shape) -> QPainterPath:
        """The union outline of a merged area's pieces, as one path.

        simplified() removes the seams where pieces overlap, so an L drawn from
        two overlapping boxes paints as a single outline rather than two."""
        path = QPainterPath()
        for x1, y1, x2, y2 in (shp.rects or [shp.bbox()]):
            path.addRect(QRectF(x1, y1, x2 - x1, y2 - y1))
        return path.simplified()


# ============================================================================
# Main Window
# ============================================================================

class _DetectionSignals(QObject):
    #: (result, error) -- exactly one is None. Delivered on the GUI thread.
    finished = pyqtSignal(object, object)


class _DetectionWorker(QRunnable):
    """Runs one detector call on a pool thread. The detectors are pure
    functions of an already-rendered page image, so the worker touches no
    window state: it just hands the result (or the exception) back to the GUI
    thread, which is where every mutation still happens."""

    def __init__(self, work: Callable[[], object]) -> None:
        super().__init__()
        self._work = work
        self.signals = _DetectionSignals()
        # The window keeps the reference; letting the pool delete us could
        # destroy `signals` before its queued emission reaches the GUI thread.
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = self._work()
        except Exception as exc:  # noqa: BLE001 - re-raised on the GUI thread
            self.signals.finished.emit(None, exc)
            return
        self.signals.finished.emit(result, None)


class AnnotatorWindow(QMainWindow):
    def __init__(self, image_path: Optional[str] = None, theme: str = "dark") -> None:
        super().__init__()
        self.setWindowTitle("Template Annotator")
        self.resize(1200, 800)

        # State
        self.image_path: Optional[str] = None
        self._source_image: Optional[Image.Image] = None
        # PDF pages are rendered lazily and cached, so large documents do not
        # rasterize every page up front.
        self._pdf_doc = None  # Optional[fitz.Document]
        self._pdf_page_count: int = 0
        self._pdf_cache: "OrderedDict[int, Image.Image]" = OrderedDict()
        self._pdf_cache_bytes: int = MAX_PDF_CACHE_BYTES
        self._pdf_page_states: Dict[int, AppState] = {}
        self._is_pdf: bool = False
        self._current_pdf_page: int = 0
        # Document inversion defaults to the theme (dark => inverted negative);
        # set by _apply_theme and overridable via the View menu.
        self.invert_document: bool = False
        # Inverted copy of _source_image, keyed by identity (see _inverted_source).
        self._inverted_cache: Optional[Image.Image] = None
        self._inverted_for: Optional[Image.Image] = None

        # Detected document lines (rules/underlines), used to place/size/align
        # fields. Keyed by page index; values are (h_lines, v_lines) in image
        # pixel space. _line_key() resolves the active key for the current view.
        self._line_cache: Dict[int, Tuple[list, list]] = {}
        self.snap_to_lines: bool = True
        self.show_guides: bool = True
        self.line_snap_tol: float = LINE_SNAP_TOL_PX

        # Detected text-string boxes (where printed text is), keyed by page.
        self._text_cache: Dict[int, list] = {}
        self.show_text_regions: bool = True

        # Detected high-contrast regions (solid bars / white-on-black headers),
        # keyed by page. Their edges act as extra snap targets; no shapes are made.
        self._hc_cache: Dict[int, list] = {}
        self.show_hc_regions: bool = True

        # Derived snap lines per page: rules + high-contrast edges + text edges,
        # rebuilt lazily from the three caches above (see _snap_lines).
        self._snap_cache: Dict[int, Tuple[list, list]] = {}

        # Detection threading. Off by default: the CLI and the test suite need
        # the synchronous path (deterministic, no event loop required). main()
        # turns it on for the interactive GUI, where a multi-second pass on a
        # large page would otherwise freeze the window (see _run_detection).
        self.async_detection: bool = False
        self._detection_busy: bool = False
        self._detection_worker: Optional[_DetectionWorker] = None
        self.shapes: List[Shape] = []
        self.next_sid: int = 1
        # selected_sid is the "key object" (anchor) — the last shape selected.
        # selected_sids is the full multi-selection (key included), in click
        # order, used by the Adobe-style align operations.
        self.selected_sid: Optional[int] = None
        self.selected_sids: List[int] = []
        self.current_tool: str = "select"
        self.undo_manager = UndoManager()
        self._has_unsaved_changes = False
        # Arrow-key auto-repeat fires a nudge per key repeat; without this the
        # history filled with one-pixel steps and "undo the move" took dozens of
        # presses. One snapshot per burst: taken when the burst starts, closed
        # after NUDGE_COALESCE_MS of quiet (or by any other edit).
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)

        # Theme. theme_mode is what the user picked (and what the View > Theme
        # menu ticks); current_theme is what that resolves to right now, which
        # for "auto" depends on the system.
        if theme == "auto":
            self.theme_mode = "auto"
            self.current_theme = detect_system_theme()
        elif theme in ("light", "dark"):
            self.theme_mode = self.current_theme = theme
        else:
            self.theme_mode = self.current_theme = "dark"

        # Build UI
        self._build_ui()
        self._build_menus()
        self._setup_shortcuts()

        # Apply theme
        self._apply_theme()

        if image_path:
            self.load_image(image_path)

    def _build_ui(self) -> None:
        """Build the main UI: control panel | canvas, plus the status bar."""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)

        self.canvas = CanvasView(self)

        # A splitter lets the user drag the divider to widen the control panel
        # (revealing anything cut off) or shrink it to give the canvas more room.
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(self._build_left_panel())
        self.main_splitter.addWidget(self.canvas)
        self.main_splitter.setStretchFactor(0, 0)   # panel keeps its width
        self.main_splitter.setStretchFactor(1, 1)    # canvas absorbs extra space
        self.main_splitter.setCollapsible(0, False)
        # Start wide enough to show the whole panel (its content needs ~430px);
        # the user can drag narrower (with a scroll bar) or wider as desired.
        self.main_splitter.setSizes([445, 900])
        main_layout.addWidget(self.main_splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Open an image. Draw shapes, name them, then export JSON/CSV.")
        self.coords_label = QLabel("")
        self.status_bar.addPermanentWidget(self.coords_label)

    def _build_left_panel(self) -> QScrollArea:
        """The control panel: every group, stacked, inside a scroll area.

        It holds many control groups and can be taller than the screen, so it
        lives inside a QScrollArea — otherwise its large minimum height would
        force the whole window taller than the display, pushing the status bar
        and the canvas scroll bar off-screen."""
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_layout.addWidget(self._build_tools_group())
        left_layout.addWidget(self._build_name_group())
        left_layout.addWidget(self._build_geometry_group())
        left_layout.addWidget(self._build_chamfer_group())

        # Display options
        self.show_labels_cb = QCheckBox("Show labels on canvas")
        self.show_labels_cb.setChecked(True)
        self.show_labels_cb.toggled.connect(self.redraw_shapes)
        left_layout.addWidget(self.show_labels_cb)

        self.show_fill_cb = QCheckBox("Show semi-transparent fill")
        self.show_fill_cb.setChecked(True)
        self.show_fill_cb.toggled.connect(self.redraw_shapes)
        left_layout.addWidget(self.show_fill_cb)

        left_layout.addWidget(self._build_detection_group())
        left_layout.addWidget(self._build_align_group())
        left_layout.addWidget(self._build_pdf_nav_group())
        left_layout.addWidget(self._build_areas_group(), 1)
        left_layout.addLayout(self._build_action_buttons())

        left_scroll = QScrollArea()
        left_scroll.setWidget(left_panel)
        left_scroll.setWidgetResizable(True)
        # Resizable (not a fixed width) with a sane lower bound, and a horizontal
        # scroll bar when the panel is dragged narrower than its content.
        left_scroll.setMinimumWidth(120)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        left_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        return left_scroll

    def _build_tools_group(self) -> QGroupBox:
        tools_group = QGroupBox("Tools")
        tools_layout = QVBoxLayout(tools_group)
        self.tool_group = QButtonGroup(self)

        tools = [
            ("Select/Move/Resize (V)", "select"),
            ("Rectangle (R)", "rect"),
            ("Circle (C)", "circle"),
            ("Chamfer Rect (H)", "chamfer"),
        ]
        for text, value in tools:
            rb = QRadioButton(text)
            rb.setChecked(value == "select")
            rb.toggled.connect(lambda checked, v=value: self._on_tool_changed(v) if checked else None)
            self.tool_group.addButton(rb)
            tools_layout.addWidget(rb)
            if value == "select":
                self.select_radio = rb
            elif value == "rect":
                self.rect_radio = rb
            elif value == "circle":
                self.circle_radio = rb
            elif value == "chamfer":
                self.chamfer_radio = rb
        return tools_group

    def _build_name_group(self) -> QGroupBox:
        name_group = QGroupBox("Name (works for ANY tool)")
        name_layout = QVBoxLayout(name_group)
        self.name_edit = QLineEdit()
        self.name_edit.returnPressed.connect(self.apply_name)
        name_layout.addWidget(self.name_edit)

        name_btns = QHBoxLayout()
        apply_btn = QPushButton("Apply to Selected")
        apply_btn.clicked.connect(self.apply_name)
        name_btns.addWidget(apply_btn)
        auto_btn = QPushButton("Auto Name")
        auto_btn.clicked.connect(lambda: self.name_edit.setText(f"area_{self.next_sid}"))
        name_btns.addWidget(auto_btn)
        name_layout.addLayout(name_btns)

        return name_group

    def _build_geometry_group(self) -> QGroupBox:
        """Numeric bbox editing for the selected shape."""
        geom_group = QGroupBox("Geometry (selected shape)")
        geom_layout = QVBoxLayout(geom_group)
        self.geom_spins: Dict[str, QDoubleSpinBox] = {}
        grid = QHBoxLayout()
        for geom_field in ("x1", "y1", "x2", "y2"):
            col = QVBoxLayout()
            col.addWidget(QLabel(geom_field.upper()))
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 100000.0)
            spin.setDecimals(1)
            spin.setKeyboardTracking(False)
            spin.editingFinished.connect(self._on_geometry_edited)
            self.geom_spins[geom_field] = spin
            col.addWidget(spin)
            grid.addLayout(col)
        geom_layout.addLayout(grid)
        self._set_geometry_enabled(False)
        return geom_group

    def _build_chamfer_group(self) -> QGroupBox:
        chamfer_group = QGroupBox("Chamfer (px)")
        chamfer_layout = QHBoxLayout(chamfer_group)
        self.chamfer_spin = QSpinBox()
        self.chamfer_spin.setRange(0, 500)
        self.chamfer_spin.setValue(20)
        chamfer_layout.addWidget(self.chamfer_spin)
        chamfer_layout.addWidget(QLabel("(used for Chamfer Rect tool)"))
        chamfer_layout.addStretch()
        return chamfer_group

    def _build_detection_group(self) -> QGroupBox:
        """Detection: document lines (for snapping), boxes, text, codes."""
        lines_group = QGroupBox("Detection")
        lines_layout = QVBoxLayout(lines_group)
        detect_btn = QPushButton("Detect Lines (L)")
        detect_btn.setToolTip("Find the document's rules/underlines to align fields to")
        detect_btn.clicked.connect(self.detect_lines)
        lines_layout.addWidget(detect_btn)

        detect_cb_btn = QPushButton("Detect Checkboxes (B)")
        detect_cb_btn.setToolTip("Find check-mark boxes and add a field for each")
        detect_cb_btn.clicked.connect(self.detect_checkboxes)
        lines_layout.addWidget(detect_cb_btn)

        detect_text_btn = QPushButton("Detect Text (X)")
        detect_text_btn.setToolTip("Box each string of characters to mark where text is")
        detect_text_btn.clicked.connect(self.detect_text)
        lines_layout.addWidget(detect_text_btn)

        self.show_text_cb = QCheckBox("Show text regions")
        self.show_text_cb.setChecked(self.show_text_regions)
        self.show_text_cb.toggled.connect(self.toggle_show_text_regions)
        lines_layout.addWidget(self.show_text_cb)

        detect_hc_btn = QPushButton("Detect High-Contrast (G)")
        detect_hc_btn.setToolTip("Find solid bars / inverted headers; their edges become snap targets")
        detect_hc_btn.clicked.connect(self.detect_high_contrast)
        lines_layout.addWidget(detect_hc_btn)

        self.show_hc_cb = QCheckBox("Show high-contrast regions")
        self.show_hc_cb.setChecked(self.show_hc_regions)
        self.show_hc_cb.toggled.connect(self.toggle_show_hc_regions)
        lines_layout.addWidget(self.show_hc_cb)

        detect_markers_btn = QPushButton("Detect Markers (M)")
        detect_markers_btn.setToolTip("Find QR codes and AprilTag/ArUco markers; add a field per marker")
        detect_markers_btn.clicked.connect(self.detect_markers)
        lines_layout.addWidget(detect_markers_btn)

        detect_barcodes_btn = QPushButton("Detect Barcodes (K)")
        detect_barcodes_btn.setToolTip("Find 1D barcodes; add a field per barcode")
        detect_barcodes_btn.clicked.connect(self.detect_barcodes)
        lines_layout.addWidget(detect_barcodes_btn)

        self.snap_lines_cb = QCheckBox("Snap fields to lines")
        self.snap_lines_cb.setChecked(self.snap_to_lines)
        self.snap_lines_cb.setToolTip("While drawing/moving/resizing, snap edges to detected lines")
        self.snap_lines_cb.toggled.connect(self.toggle_snap_to_lines)
        lines_layout.addWidget(self.snap_lines_cb)

        self.show_guides_cb = QCheckBox("Show line guides")
        self.show_guides_cb.setChecked(self.show_guides)
        self.show_guides_cb.toggled.connect(self.toggle_show_guides)
        lines_layout.addWidget(self.show_guides_cb)

        snap_now_btn = QPushButton("Snap fields to lines now")
        snap_now_btn.setToolTip("Align the selected field (or all fields) to nearby lines")
        snap_now_btn.clicked.connect(self.snap_fields_to_lines)
        lines_layout.addWidget(snap_now_btn)
        return lines_group

    def _build_align_group(self) -> QGroupBox:
        """Align (Adobe-style): operates on a multi-selection relative to the key."""
        align_group = QGroupBox("Align (to key object)")
        align_outer = QVBoxLayout(align_group)
        align_hint = QLabel("Ctrl+click 2+ shapes; the last clicked is the anchor.")
        align_hint.setWordWrap(True)
        align_outer.addWidget(align_hint)
        align_row1 = QHBoxLayout()
        align_row2 = QHBoxLayout()
        align_row3 = QHBoxLayout()

        def _align_btn(label, op, row, tip):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _checked=False, o=op: self.align_selected(o))
            row.addWidget(b)

        # Three rows of three keeps every label readable at the panel width.
        _align_btn("Left", "left", align_row1, "Align left edges to the key")
        _align_btn("Right", "right", align_row1, "Align right edges to the key")
        _align_btn("Top", "top", align_row1, "Align top edges to the key")
        _align_btn("Bottom", "bottom", align_row2, "Align bottom edges to the key")
        _align_btn("Center H", "center_h", align_row2, "Align horizontal centres to the key")
        _align_btn("Center V", "center_v", align_row2, "Align vertical centres to the key")
        _align_btn("Match W", "match_w", align_row3, "Match width to the key")
        _align_btn("Match H", "match_h", align_row3, "Match height to the key")
        _align_btn("Match WH", "match_both", align_row3, "Match width and height to the key")
        align_outer.addLayout(align_row1)
        align_outer.addLayout(align_row2)
        align_outer.addLayout(align_row3)
        return align_group

    def _build_pdf_nav_group(self) -> QGroupBox:
        """PDF page navigation; hidden until a PDF is open."""
        self.pdf_nav_group = QGroupBox("PDF pages")
        pdf_nav_layout = QHBoxLayout(self.pdf_nav_group)
        self.pdf_prev_btn = QPushButton("Previous")
        self.pdf_prev_btn.setToolTip("Go to previous PDF page")
        self.pdf_prev_btn.clicked.connect(self.prev_pdf_page)
        self.pdf_next_btn = QPushButton("Next")
        self.pdf_next_btn.setToolTip("Go to next PDF page")
        self.pdf_next_btn.clicked.connect(self.next_pdf_page)
        self.pdf_page_label = QLabel("Page 1 / 1")
        pdf_nav_layout.addWidget(self.pdf_prev_btn)
        pdf_nav_layout.addWidget(self.pdf_page_label)
        pdf_nav_layout.addWidget(self.pdf_next_btn)
        self.pdf_nav_group.setVisible(False)
        return self.pdf_nav_group

    def _build_areas_group(self) -> QGroupBox:
        self.areas_group = QGroupBox("Areas")
        areas_layout = QVBoxLayout(self.areas_group)
        self.areas_list = QListWidget()
        # Extended selection so Ctrl/Shift-click in the list builds a multi-
        # selection for the align operations (the current row is the key object).
        self.areas_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.areas_list.itemSelectionChanged.connect(self._on_list_selection)
        self.areas_list.itemDoubleClicked.connect(self._on_list_double_click)
        self.areas_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.areas_list.customContextMenuRequested.connect(self._show_list_context_menu)
        areas_layout.addWidget(self.areas_list)
        return self.areas_group

    def _build_action_buttons(self) -> QHBoxLayout:
        action_layout = QHBoxLayout()
        dup_btn = QPushButton("Duplicate")
        dup_btn.clicked.connect(self.duplicate_selected)
        action_layout.addWidget(dup_btn)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self.delete_selected)
        action_layout.addWidget(del_btn)
        norm_btn = QPushButton("Normalize")
        norm_btn.clicked.connect(self.normalize_annotations)
        action_layout.addWidget(norm_btn)
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self.clear_all)
        action_layout.addWidget(clear_btn)
        return action_layout

    def _build_menus(self) -> None:
        """Build menu bar."""
        menubar = self.menuBar()

        # File menu
        file_menu = menubar.addMenu("File")

        open_action = QAction("Open Image / PDF", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_image_dialog)
        file_menu.addAction(open_action)

        load_json_action = QAction("Load JSON", self)
        load_json_action.triggered.connect(self.load_json_dialog)
        file_menu.addAction(load_json_action)

        file_menu.addSeparator()

        save_action = QAction("Save JSON", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_json_dialog)
        file_menu.addAction(save_action)

        export_csv_action = QAction("Export CSV", self)
        export_csv_action.triggered.connect(self.export_csv_dialog)
        file_menu.addAction(export_csv_action)

        screenshot_action = QAction("Save Screenshot…", self)
        screenshot_action.setShortcut(QKeySequence("Ctrl+Shift+P"))
        screenshot_action.triggered.connect(self.save_screenshot_dialog)
        file_menu.addAction(screenshot_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Edit menu
        edit_menu = menubar.addMenu("Edit")

        undo_action = QAction("Undo", self)
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        undo_action.triggered.connect(self.do_undo)
        edit_menu.addAction(undo_action)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        redo_action.triggered.connect(self.do_redo)
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()

        dup_action = QAction("Duplicate", self)
        dup_action.setShortcut(QKeySequence("Ctrl+D"))
        dup_action.triggered.connect(self.duplicate_selected)
        edit_menu.addAction(dup_action)

        del_action = QAction("Delete", self)
        del_action.setShortcut(QKeySequence.StandardKey.Delete)
        del_action.triggered.connect(self.delete_selected)
        edit_menu.addAction(del_action)

        edit_menu.addSeparator()

        merge_action = QAction("Merge Selected", self)
        merge_action.setShortcut(QKeySequence("Ctrl+M"))
        merge_action.triggered.connect(self.merge_selected)
        edit_menu.addAction(merge_action)

        split_action = QAction("Split", self)
        split_action.triggered.connect(self.split_selected)
        edit_menu.addAction(split_action)

        edit_menu.addSeparator()

        norm_action = QAction("Normalize\tN", self)
        norm_action.triggered.connect(self.normalize_annotations)
        edit_menu.addAction(norm_action)

        edit_menu.addSeparator()

        detect_lines_action = QAction("Detect Lines\tL", self)
        detect_lines_action.triggered.connect(self.detect_lines)
        edit_menu.addAction(detect_lines_action)

        detect_boxes_action = QAction("Detect Checkboxes\tB", self)
        detect_boxes_action.triggered.connect(self.detect_checkboxes)
        edit_menu.addAction(detect_boxes_action)

        detect_text_action = QAction("Detect Text\tX", self)
        detect_text_action.triggered.connect(self.detect_text)
        edit_menu.addAction(detect_text_action)

        detect_hc_action = QAction("Detect High-Contrast\tG", self)
        detect_hc_action.triggered.connect(self.detect_high_contrast)
        edit_menu.addAction(detect_hc_action)

        detect_markers_action = QAction("Detect Markers (QR/AprilTag)\tM", self)
        detect_markers_action.triggered.connect(self.detect_markers)
        edit_menu.addAction(detect_markers_action)

        detect_barcodes_action = QAction("Detect Barcodes\tK", self)
        detect_barcodes_action.triggered.connect(self.detect_barcodes)
        edit_menu.addAction(detect_barcodes_action)

        add_text_fields_action = QAction("Add Text Regions as Fields", self)
        add_text_fields_action.triggered.connect(self.add_text_regions_as_fields)
        edit_menu.addAction(add_text_fields_action)

        snap_lines_action = QAction("Snap Fields to Lines", self)
        snap_lines_action.triggered.connect(self.snap_fields_to_lines)
        edit_menu.addAction(snap_lines_action)

        # Align submenu (operates on a Ctrl-click multi-selection, key = last).
        align_menu = edit_menu.addMenu("Align (to key object)")
        for label, op in (
            ("Align Left", "left"), ("Align Right", "right"),
            ("Align Top", "top"), ("Align Bottom", "bottom"),
            ("Align Centers Horizontal", "center_h"),
            ("Align Centers Vertical", "center_v"),
            ("Match Width", "match_w"), ("Match Height", "match_h"),
            ("Match Width & Height", "match_both"),
        ):
            act = QAction(label, self)
            act.triggered.connect(lambda _checked=False, o=op: self.align_selected(o))
            align_menu.addAction(act)

        edit_menu.addSeparator()

        clear_action = QAction("Clear All", self)
        clear_action.triggered.connect(self.clear_all)
        edit_menu.addAction(clear_action)

        # View menu
        view_menu = menubar.addMenu("View")

        theme_menu = view_menu.addMenu("Theme")

        # One entry per THEME_MODES, exclusive: the group is what keeps exactly
        # one ticked, and _apply_theme ticks the mode the user picked.
        self.theme_actions = []
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for label, mode in zip(("Auto (System)", "Light", "Dark"), THEME_MODES):
            action = QAction(label, self, checkable=True)
            action.setChecked(mode == self.theme_mode)
            action.triggered.connect(lambda _checked=False, m=mode: self._set_theme(m))
            theme_group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions.append(action)

        view_menu.addSeparator()

        toggle_theme_action = QAction("Toggle Theme\tT", self)
        toggle_theme_action.triggered.connect(self.toggle_theme)
        view_menu.addAction(toggle_theme_action)

        self.invert_doc_action = QAction("Invert Document Colors", self, checkable=True)
        self.invert_doc_action.setChecked(self.invert_document)
        self.invert_doc_action.toggled.connect(self.toggle_invert_document)
        view_menu.addAction(self.invert_doc_action)

        view_menu.addSeparator()

        fit_action = QAction("Fit to Window", self)
        fit_action.triggered.connect(self.canvas.fit_to_view)
        view_menu.addAction(fit_action)

        zoom_sel_action = QAction("Zoom to Selection\tZ", self)
        zoom_sel_action.triggered.connect(self.zoom_to_selection)
        view_menu.addAction(zoom_sel_action)

        zoom_in_action = QAction("Zoom In", self)
        zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        zoom_in_action.triggered.connect(lambda: self._zoom_view(1.1))
        view_menu.addAction(zoom_in_action)

        zoom_out_action = QAction("Zoom Out", self)
        zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out_action.triggered.connect(lambda: self._zoom_view(1 / 1.1))
        view_menu.addAction(zoom_out_action)

        reset_zoom_action = QAction("Reset Zoom (100%)", self)
        reset_zoom_action.triggered.connect(lambda: self.canvas.resetTransform())
        view_menu.addAction(reset_zoom_action)

        # Help menu
        help_menu = menubar.addMenu("Help")

        shortcuts_action = QAction("Keyboard Shortcuts", self)
        shortcuts_action.setShortcut(QKeySequence.StandardKey.HelpContents)
        shortcuts_action.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts_action)

        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    @staticmethod
    def _text_input_focused() -> bool:
        """True when a text-entry widget has focus, so single-letter shortcuts
        should yield to typing instead of firing."""
        app = QApplication.instance()
        if app is None:
            return False
        return isinstance(app.focusWidget(), (QLineEdit, QSpinBox, QDoubleSpinBox))

    def _guarded(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Wrap a single-letter shortcut so it no-ops while typing in a field."""
        def wrapper() -> None:
            if self._text_input_focused():
                return
            fn()
        return wrapper

    def _setup_shortcuts(self) -> None:
        """Setup keyboard shortcuts. Single-letter shortcuts are gated on focus
        so they do not steal keystrokes from the Name field or spin boxes."""
        g = self._guarded
        QShortcut(QKeySequence("V"), self, g(lambda: self.select_radio.setChecked(True)))
        QShortcut(QKeySequence("R"), self, g(lambda: self.rect_radio.setChecked(True)))
        QShortcut(QKeySequence("C"), self, g(lambda: self.circle_radio.setChecked(True)))
        QShortcut(QKeySequence("H"), self, g(lambda: self.chamfer_radio.setChecked(True)))
        QShortcut(QKeySequence("N"), self, g(self.normalize_annotations))
        QShortcut(QKeySequence("L"), self, g(self.detect_lines))
        QShortcut(QKeySequence("B"), self, g(self.detect_checkboxes))
        QShortcut(QKeySequence("X"), self, g(self.detect_text))
        QShortcut(QKeySequence("G"), self, g(self.detect_high_contrast))
        QShortcut(QKeySequence("M"), self, g(self.detect_markers))
        QShortcut(QKeySequence("K"), self, g(self.detect_barcodes))
        QShortcut(QKeySequence("T"), self, g(self.toggle_theme))
        QShortcut(QKeySequence("Z"), self, g(self.zoom_to_selection))
        QShortcut(QKeySequence("Alt+Left"), self, self.prev_pdf_page)
        QShortcut(QKeySequence("Alt+Right"), self, self.next_pdf_page)
        QShortcut(QKeySequence("F1"), self, self._show_shortcuts)
        # Arrow-key nudging of the selected shape (Shift = larger step).
        QShortcut(QKeySequence("Left"), self, g(lambda: self._nudge_selected(-1, 0)))
        QShortcut(QKeySequence("Right"), self, g(lambda: self._nudge_selected(1, 0)))
        QShortcut(QKeySequence("Up"), self, g(lambda: self._nudge_selected(0, -1)))
        QShortcut(QKeySequence("Down"), self, g(lambda: self._nudge_selected(0, 1)))
        QShortcut(QKeySequence("Shift+Left"), self, g(lambda: self._nudge_selected(-10, 0)))
        QShortcut(QKeySequence("Shift+Right"), self, g(lambda: self._nudge_selected(10, 0)))
        QShortcut(QKeySequence("Shift+Up"), self, g(lambda: self._nudge_selected(0, -10)))
        QShortcut(QKeySequence("Shift+Down"), self, g(lambda: self._nudge_selected(0, 10)))

    def _nudge_selected(self, dx: float, dy: float) -> None:
        if self.selected_sid is None:
            return
        shp = self.get_shape(self.selected_sid)
        if not shp:
            return
        x1, y1, x2, y2 = shp.bbox()
        nx1, ny1, nx2, ny2 = self._clamp_bbox(
            x1 + dx, y1 + dy, x2 + dx, y2 + dy, keep_size=True
        )
        # Don't push an undo entry / dirty flag when clamping made it a no-op
        # (e.g. holding an arrow against the image edge).
        if (nx1, ny1, nx2, ny2) == (x1, y1, x2, y2):
            return
        # One undo step per burst: the snapshot is taken on the first nudge and
        # the timer (restarted by every nudge) keeps the burst open until the
        # user stops. save_for_undo stops the timer, so the check comes first.
        if not self._nudge_timer.isActive():
            self.save_for_undo()
        self._nudge_timer.start(NUDGE_COALESCE_MS)
        shp.translate(nx1 - x1, ny1 - y1)   # whole unit: a multirect's pieces too
        self.update_active_graphics(shp)
        self._populate_geometry(shp)

    def _zoom_view(self, factor: float) -> None:
        """Zoom the canvas by `factor` about the viewport centre (used by the
        Zoom In/Out menu actions)."""
        if not self.canvas.pixmap_item:
            return
        center = QPointF(self.canvas.viewport().rect().center())
        self.canvas.zoom_at_point(factor, center)

    def zoom_to_selection(self) -> None:
        """Fit the view to the selected shape (with a little padding)."""
        if self.selected_sid is None or not self.canvas.pixmap_item:
            return
        shp = self.get_shape(self.selected_sid)
        if not shp:
            return
        x1, y1, x2, y2 = shp.bbox()
        pad = max(20.0, (x2 - x1) * 0.15, (y2 - y1) * 0.15)
        rect = QRectF(x1 - pad, y1 - pad, (x2 - x1) + 2 * pad, (y2 - y1) + 2 * pad)
        self.canvas.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def _on_tool_changed(self, tool: str) -> None:
        """Handle tool change."""
        self.current_tool = tool

    def _apply_theme(self) -> None:
        """Apply current theme."""
        if self.current_theme == "dark":
            self.setStyleSheet(DARK_STYLE)
        else:
            self.setStyleSheet(LIGHT_STYLE)
        # Document inversion follows the theme by default: dark mode shows the
        # page as a photographic negative (white-on-black) to match the dark
        # chrome. The View > Invert Document Colors toggle overrides this until
        # the next theme change.
        self.invert_document = (self.current_theme == "dark")
        if hasattr(self, "invert_doc_action"):
            self.invert_doc_action.blockSignals(True)
            self.invert_doc_action.setChecked(self.invert_document)
            self.invert_doc_action.blockSignals(False)
        if self._source_image is not None:
            self._set_canvas_image()
        if hasattr(self, "theme_actions"):
            # Tick what the USER chose, not what it resolved to: "Auto" resolves
            # to light or dark, so ticking by current_theme immediately
            # unchecked the Auto entry the click had just checked.
            for action, mode in zip(self.theme_actions, THEME_MODES):
                action.setChecked(mode == self.theme_mode)
        self.redraw_shapes()

    def _set_theme(self, mode: str) -> None:
        """Set theme mode: "auto" (follow the system), "light" or "dark"."""
        self.theme_mode = mode if mode in THEME_MODES else "auto"
        self.current_theme = (detect_system_theme() if self.theme_mode == "auto"
                              else self.theme_mode)
        self._apply_theme()

    def toggle_theme(self) -> None:
        """Toggle between light and dark theme."""
        self.current_theme = "dark" if self.current_theme == "light" else "light"
        # An explicit toggle is an explicit choice; it stops following the system.
        self.theme_mode = self.current_theme
        self._apply_theme()
        self.status_bar.showMessage(f"Theme: {self.current_theme.capitalize()}")

    def toggle_invert_document(self, checked: bool) -> None:
        """Manually override document inversion for the current theme. Dark mode
        defaults to inverted; this lets the user flip it (resets on the next
        theme change)."""
        self.invert_document = bool(checked)
        if self._source_image is not None:
            self._set_canvas_image()
            self.redraw_shapes()
        self.status_bar.showMessage(
            f"Document inversion: {'on' if self.invert_document else 'off'}"
        )

    def _update_title(self) -> None:
        """Reflect the open file, current PDF page, and dirty state in the title."""
        name = os.path.basename(self.image_path) if self.image_path else "Untitled"
        dirty = "* " if self._has_unsaved_changes else ""
        page = f" [page {self._current_pdf_page + 1}/{self._pdf_page_count}]" if self._is_pdf else ""
        self.setWindowTitle(f"{dirty}{name}{page} - Template Annotator")

    # -------------------------
    # Image handling
    # -------------------------

    def open_image_dialog(self) -> None:
        if not self._confirm_discard("opening another file"):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Image / PDF",
            filter="Image and PDF files (*.png *.jpg *.jpeg *.bmp *.gif *.pdf *.PDF);;All files (*.*)"
        )
        if path:
            self.load_image(path)

    def load_image(self, path: str) -> bool:
        """Open an image/PDF, replacing the document. Returns True on success;
        on False the previously loaded document is left untouched (callers that
        chain further state onto the load must check)."""
        self._nudge_timer.stop()   # a new document ends any nudge burst
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".pdf":
                if fitz is None:
                    raise RuntimeError(
                        "Missing dependency for PDF support. Install with: pip install PyMuPDF"
                    )
                # Open and validate into locals first; only commit on success so
                # a failure leaves the previously loaded document intact.
                doc = fitz.open(path)
                if doc.page_count < 1:
                    doc.close()
                    raise ValueError("PDF has no pages.")
                # Force-render page 0 to surface a corrupt document up front.
                probe = self._render_pdf_page(0, doc=doc)

                self._close_pdf()  # closes any prior document and clears the cache
                self._pdf_doc = doc
                self._pdf_page_count = doc.page_count
                self._pdf_cache[0] = probe  # seed cache with the already-rendered page 0
                self._is_pdf = True
                self._source_image = None
                self._pdf_page_states = {}
                self._line_cache.clear()
                self._text_cache.clear()
                self._hc_cache.clear()
                self._invalidate_snap_lines()
                self._current_pdf_page = 0
                self.image_path = os.path.abspath(path)
                self._set_pdf_page(0, reset_state=True)
                return True
            else:
                img = Image.open(path)
                img.load()  # force-decode so truncated files raise here
                source = img.convert("RGBA")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open image: {e}")
            return False

        # Commit raster-image state only after a clean decode.
        self._close_pdf()
        self._is_pdf = False
        self._pdf_page_count = 0
        self._pdf_page_states = {}
        self._line_cache.clear()
        self._text_cache.clear()
        self._hc_cache.clear()
        self._invalidate_snap_lines()
        self._current_pdf_page = 0
        self._update_pdf_controls()

        self.image_path = os.path.abspath(path)

        # Reset state BEFORE the canvas is rebuilt: _set_canvas_image() ends in
        # canvas.set_image(), which redraws self.shapes -- so clearing after it
        # left the previous document's shapes drawn as ghosts over the new image.
        self.shapes.clear()
        self.next_sid = 1
        self.selected_sid = None
        self.selected_sids = []
        self.areas_list.clear()
        self.undo_manager.clear()
        self._has_unsaved_changes = False

        # Release the previous page's inverted bitmap now, rather than on the
        # next _inverted_source() call: it is page-sized and sits outside the
        # PDF cache's byte budget.
        self._inverted_cache = self._inverted_for = None
        self._source_image = source
        self._set_canvas_image()
        self._update_title()

        # Fit to view
        QTimer.singleShot(10, self.canvas.fit_to_view)

        self.status_bar.showMessage(
            f"Loaded: {os.path.basename(path)} ({source.width}x{source.height})"
        )
        return True

    def _close_pdf(self) -> None:
        if self._pdf_doc is not None:
            try:
                self._pdf_doc.close()
            except Exception:
                pass
        self._pdf_doc = None
        self._pdf_cache.clear()

    def _render_pdf_page(self, index: int, doc=None) -> Image.Image:
        """Rasterize a single PDF page to a PIL image at PDF_RENDER_SCALE,
        clamping the scale so an oversized page stays within MAX_PDF_RENDER_PX."""
        if fitz is None:
            raise RuntimeError(
                "Missing dependency for PDF support. Install with: pip install PyMuPDF"
            )
        document = doc if doc is not None else self._pdf_doc
        if document is None:
            raise RuntimeError("No PDF document is open.")

        page = document.load_page(index)
        rect = page.rect
        scale = PDF_RENDER_SCALE
        longest = max(rect.width, rect.height) * scale
        if longest > MAX_PDF_RENDER_PX and longest > 0:
            scale *= MAX_PDF_RENDER_PX / longest
        matrix = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    @staticmethod
    def _image_bytes(image: Image.Image) -> int:
        return image.width * image.height * len(image.getbands())

    def _pdf_page_image(self, index: int) -> Image.Image:
        """Return the rendered image for a PDF page, using an LRU cache bounded
        by total pixel bytes (MAX_PDF_CACHE_BYTES) rather than a page count."""
        cached = self._pdf_cache.get(index)
        if cached is not None:
            self._pdf_cache.move_to_end(index)
            return cached
        image = self._render_pdf_page(index)
        self._pdf_cache[index] = image
        self._pdf_cache.move_to_end(index)
        total = sum(self._image_bytes(im) for im in self._pdf_cache.values())
        while len(self._pdf_cache) > 1 and total > self._pdf_cache_bytes:
            _evicted, dropped = self._pdf_cache.popitem(last=False)
            total -= self._image_bytes(dropped)
        return image

    @staticmethod
    def _invert_image(image: Image.Image) -> Image.Image:
        # One pass over the pixels with a per-band lookup table, instead of the
        # split/merge/invert/split/merge chain (5 full-image copies) this used
        # to do. The table inverts R, G and B and leaves A alone.
        rgba = image if image.mode == "RGBA" else image.convert("RGBA")
        return rgba.point(_INVERT_RGBA_LUT)

    def _inverted_source(self) -> Image.Image:
        """The current page, inverted, computed once per page.

        Cached against the source object itself, so replacing _source_image
        (another document, another PDF page, a re-render) invalidates it with no
        bookkeeping; toggling inversion or the theme back and forth is free."""
        if self._inverted_for is not self._source_image:
            self._inverted_cache = self._invert_image(self._source_image)
            self._inverted_for = self._source_image
        return self._inverted_cache

    def _set_canvas_image(self) -> None:
        if self._source_image is None:
            return

        display = self._inverted_source() if self.invert_document else self._source_image

        # Both sources are already RGBA; convert() to the same mode is a full
        # copy of the page, not a no-op.
        img_rgba = display if display.mode == "RGBA" else display.convert("RGBA")
        # Keep the backing buffer alive for the QImage's lifetime; QImage does
        # not copy the bytes it is constructed from.
        self._qimage_buffer = img_rgba.tobytes("raw", "RGBA")
        qimage = QImage(
            self._qimage_buffer, img_rgba.width, img_rgba.height,
            QImage.Format.Format_RGBA8888,
        )
        pixmap = QPixmap.fromImage(qimage)
        self.canvas.set_image(pixmap)

    def prev_pdf_page(self) -> None:
        if not self._is_pdf:
            return
        self._set_pdf_page(self._current_pdf_page - 1)

    def next_pdf_page(self) -> None:
        if not self._is_pdf:
            return
        self._set_pdf_page(self._current_pdf_page + 1)

    def _set_pdf_page(self, page_index: int, *, reset_state: bool = False) -> None:
        if self._pdf_page_count <= 0:
            return
        if page_index < 0 or page_index >= self._pdf_page_count:
            return
        self._nudge_timer.stop()   # a page change ends any nudge burst

        # Render first. A page that fails to rasterize (corrupt content stream
        # in an otherwise valid PDF) must leave the app FULLY on the current
        # page -- and must not raise inside this slot, which PyQt6 turns into
        # qFatal()/abort(), losing every unsaved annotation.
        try:
            page_image = self._pdf_page_image(page_index).convert("RGBA")
        except Exception as e:
            QMessageBox.critical(
                self, "Error", f"Failed to render PDF page {page_index + 1}: {e}"
            )
            return

        if not reset_state:
            self._pdf_page_states[self._current_pdf_page] = AppState(
                shapes=[s.clone() for s in self.shapes],
                next_sid=self.next_sid,
                selected_sid=self.selected_sid,
            )

        self._current_pdf_page = page_index
        state = self._pdf_page_states.get(self._current_pdf_page)
        if state is None:
            self.shapes = []
            self.next_sid = 1
            self.selected_sid = None
        else:
            self.shapes = [s.clone() for s in state.shapes]
            for s in self.shapes:
                s.page = self._current_pdf_page
            self.next_sid = max(1, state.next_sid)
            self.selected_sid = state.selected_sid
        # The multi-selection belongs to the page we just left; carrying it over
        # would highlight (and align/delete) shapes by id on the new page.
        self.selected_sids = [] if self.selected_sid is None else [self.selected_sid]

        self._inverted_cache = self._inverted_for = None   # page-sized; see load_image
        self._source_image = page_image
        self._set_canvas_image()

        self.areas_list.clear()
        self.update_list()
        self.redraw_shapes()
        # Undo history is per-page; navigating starts a fresh per-page stack.
        self.undo_manager.clear()
        # The unsaved-changes flag is document-level: only a fresh load (or a
        # save) clears it, so edits on a page the user navigated away from still
        # trigger the close-time prompt.
        if reset_state:
            self._has_unsaved_changes = False
        self._update_pdf_controls()
        self._update_title()
        self.status_bar.showMessage(
            f"PDF page {self._current_pdf_page + 1} / {self._pdf_page_count}"
        )
        QTimer.singleShot(10, self.canvas.fit_to_view)

    def _update_pdf_controls(self) -> None:
        if not self._is_pdf:
            self.pdf_nav_group.setVisible(False)
            return

        self.pdf_nav_group.setVisible(True)
        if self._pdf_page_count <= 1:
            self.pdf_prev_btn.setEnabled(False)
            self.pdf_next_btn.setEnabled(False)
            self.pdf_page_label.setText("Page 1 / 1")
            return

        self.pdf_prev_btn.setEnabled(self._current_pdf_page > 0)
        self.pdf_next_btn.setEnabled(self._current_pdf_page < self._pdf_page_count - 1)
        self.pdf_page_label.setText(
            f"Page {self._current_pdf_page + 1} / {self._pdf_page_count}"
        )

    # -------------------------
    # Shape management
    # -------------------------

    def get_shape(self, sid: int) -> Optional[Shape]:
        return next((s for s in self.shapes if s.sid == sid), None)

    def set_selected(self, sid: Optional[int]) -> None:
        self.selected_sid = sid
        # Single selection collapses any multi-selection to just this shape.
        self.selected_sids = [] if sid is None else [sid]
        if sid is None:
            self.name_edit.setText("")
            self.areas_list.clearSelection()
            self._set_geometry_enabled(False)
        else:
            shp = self.get_shape(sid)
            if shp:
                self.name_edit.setText(shp.name)
                # Iterate the data source we index, and suppress the list's
                # selection signal so this doesn't re-enter set_selected.
                self.areas_list.blockSignals(True)
                for i, s in enumerate(self.shapes):
                    if s.sid == sid:
                        self.areas_list.setCurrentRow(i)
                        break
                self.areas_list.blockSignals(False)
                self._populate_geometry(shp)
            else:
                self._set_geometry_enabled(False)
        self.restyle_selection()

    def _reconcile_selection(self) -> None:
        """Collapse the multi-selection to be consistent with selected_sid after a
        structural change (undo/redo, page switch) that may have replaced shapes."""
        if self.selected_sid is not None and self.get_shape(self.selected_sid) is not None:
            self.selected_sids = [self.selected_sid]
        else:
            self.selected_sid = None
            self.selected_sids = []

    def _sync_key_ui(self) -> None:
        """Point the name field / geometry panel / list at the current key object
        (selected_sid), the anchor for align operations."""
        shp = self.get_shape(self.selected_sid) if self.selected_sid is not None else None
        if shp:
            self.name_edit.setText(shp.name)
            self._populate_geometry(shp)
        else:
            self.name_edit.setText("")
            self._set_geometry_enabled(False)
        self.areas_list.blockSignals(True)
        sel = set(self.selected_sids)
        for i, s in enumerate(self.shapes):
            item = self.areas_list.item(i)
            if item is not None:
                item.setSelected(s.sid in sel)
            if s.sid == self.selected_sid:
                self.areas_list.setCurrentRow(i)
        self.areas_list.blockSignals(False)
        self.restyle_selection()

    def toggle_selection(self, sid: Optional[int]) -> None:
        """Add/remove a shape from the multi-selection (Ctrl+click). The most
        recently added shape becomes the key object (anchor)."""
        if sid is None or self.get_shape(sid) is None:
            return
        if sid in self.selected_sids:
            self.selected_sids.remove(sid)
            self.selected_sid = self.selected_sids[-1] if self.selected_sids else None
        else:
            self.selected_sids.append(sid)
            self.selected_sid = sid
        self._sync_key_ui()

    def select_in_region(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """Select every shape whose bbox intersects the rectangle (marquee). The
        last shape in draw order becomes the key object."""
        rx1, rx2 = (x1, x2) if x1 <= x2 else (x2, x1)
        ry1, ry2 = (y1, y2) if y1 <= y2 else (y2, y1)
        hits = []
        for s in self.shapes:
            bx1, by1, bx2, by2 = s.bbox()
            if not (bx2 < rx1 or bx1 > rx2 or by2 < ry1 or by1 > ry2):
                hits.append(s.sid)
        self.selected_sids = hits
        self.selected_sid = hits[-1] if hits else None
        self._sync_key_ui()

    def align_selected(self, op: str) -> None:
        """Align/resize the multi-selection to the key object (Adobe-style)."""
        # Drop any ids that no longer exist (e.g. after a delete/undo).
        self.selected_sids = [s for s in self.selected_sids if self.get_shape(s) is not None]
        if self.selected_sid not in self.selected_sids:
            self.selected_sid = self.selected_sids[-1] if self.selected_sids else None
        if len(self.selected_sids) < 2:
            self.status_bar.showMessage("Select 2+ shapes (Ctrl+click) to align.")
            return
        key = self.get_shape(self.selected_sid)
        if key is None:
            return
        targets = [self.get_shape(s) for s in self.selected_sids]
        targets = [s for s in targets if s is not None]
        self.save_for_undo()
        skipped = align_shapes(targets, key, op)
        # Per-shape fix-ups: keep circles square, clamp chamfers and bounds.
        # A multirect is the one kind apply_snapped_bbox refuses, so it falls
        # through to the keep-size translate instead of going unclamped.
        w, h = self.canvas.image_size
        for s in targets:
            if s is not key and not apply_snapped_bbox(s, s.bbox(), w, h, min_size=0.0):
                s.clamp_into(w, h)
        self.update_list()
        self._sync_key_ui()
        note = f" Skipped {skipped} multirect(s): split to resize." if skipped else ""
        self.status_bar.showMessage(
            f"Aligned {len(targets) - 1 - skipped} shape(s) to the key object ({op}).{note}")

    def _set_geometry_enabled(self, enabled: bool) -> None:
        for spin in self.geom_spins.values():
            spin.setEnabled(enabled)
            if not enabled:
                spin.blockSignals(True)
                spin.setValue(0.0)
                spin.blockSignals(False)

    def _populate_geometry(self, shp: Shape) -> None:
        # A merged area shows its envelope read-only: editing it would say
        # nothing about where the pieces go.
        editable = shp.kind != "multirect"
        x1, y1, x2, y2 = shp.bbox()
        for geom_field, value in zip(("x1", "y1", "x2", "y2"), (x1, y1, x2, y2)):
            spin = self.geom_spins[geom_field]
            spin.setEnabled(editable)
            spin.blockSignals(True)
            spin.setValue(float(value))
            spin.blockSignals(False)
        if not editable:
            self.status_bar.showMessage("Split to edit pieces")

    def _on_geometry_edited(self) -> None:
        if self.selected_sid is None:
            return
        shp = self.get_shape(self.selected_sid)
        if not shp:
            return
        x1 = self.geom_spins["x1"].value()
        y1 = self.geom_spins["y1"].value()
        x2 = self.geom_spins["x2"].value()
        y2 = self.geom_spins["y2"].value()
        nx1, nx2 = (x1, x2) if x1 <= x2 else (x2, x1)
        ny1, ny2 = (y1, y2) if y1 <= y2 else (y2, y1)
        if (nx2 - nx1) < MIN_SHAPE_SIZE_PX or (ny2 - ny1) < MIN_SHAPE_SIZE_PX:
            # Ignore degenerate edits; restore the field values from the shape.
            self._populate_geometry(shp)
            return
        # The per-kind fix-ups (square circle, capped at the canvas, clamped to
        # bounds, chamfer no larger than half the box) are apply_snapped_bbox's
        # job -- doing them here by hand is how the geometry fields grew their
        # own subtly different rules. The snapshot is captured before the edit
        # and only committed if the edit changed anything: a value that clamps
        # back to where the shape already was is not an undo step.
        before = self._snapshot_state()
        old = (shp.bbox(), shp.chamfer)
        apply_snapped_bbox(shp, (nx1, ny1, nx2, ny2), *self.canvas.image_size,
                           min_size=0.0)
        if (shp.bbox(), shp.chamfer) == old:
            return
        self.commit_undo_snapshot(before)
        self._populate_geometry(shp)
        self.update_list()
        self.redraw_shapes()

    def _snapshot_state(self) -> AppState:
        return AppState(
            shapes=[s.clone() for s in self.shapes],
            next_sid=self.next_sid,
            selected_sid=self.selected_sid,
        )

    def save_for_undo(self) -> None:
        self.commit_undo_snapshot(self._snapshot_state())

    def commit_undo_snapshot(self, state: AppState) -> None:
        """Push a previously-captured pre-edit snapshot onto the undo stack.
        The stack takes ownership of `state`; callers must not keep editing it."""
        self._nudge_timer.stop()      # any other edit ends a nudge burst
        self.undo_manager.save_state(state)
        self._has_unsaved_changes = True
        self._update_title()

    def do_undo(self) -> None:
        self._nudge_timer.stop()
        state = self.undo_manager.undo(self._snapshot_state())
        if state:
            # The popped state is discarded, so its shapes can be adopted as-is.
            self.shapes = state.shapes
            self.next_sid = state.next_sid
            self.selected_sid = state.selected_sid
            self._reconcile_selection()
            self.update_list()
            # A restored state can differ in every way (shape set, names,
            # geometry), so the scene is rebuilt rather than restyled.
            self.redraw_shapes()
            self._sync_key_ui()
            # Undo moves the document away from whatever was last saved, so it
            # is an unsaved change like any other edit -- without this, save +
            # undo + close threw the undone work away without prompting.
            self._has_unsaved_changes = True
            self._update_title()
            self.status_bar.showMessage("Undo")

    def do_redo(self) -> None:
        self._nudge_timer.stop()
        state = self.undo_manager.redo(self._snapshot_state())
        if state:
            self.shapes = state.shapes
            self.next_sid = state.next_sid
            self.selected_sid = state.selected_sid
            self._reconcile_selection()
            self.update_list()
            # A restored state can differ in every way (shape set, names,
            # geometry), so the scene is rebuilt rather than restyled.
            self.redraw_shapes()
            self._sync_key_ui()
            self._has_unsaved_changes = True   # same as undo: the file is stale
            self._update_title()
            self.status_bar.showMessage("Redo")

    def update_list(self) -> None:
        # Rebuilding the list emits itemSelectionChanged as items are cleared;
        # suppress it so a programmatic rebuild never clobbers the current
        # (possibly multi-) selection state.
        self.areas_list.blockSignals(True)
        self.areas_list.clear()
        for shp in self.shapes:
            self.areas_list.addItem(f"[{shp.sid:03d}] {shp.kind:9s}  {shp.name}")
        self.areas_list.blockSignals(False)
        if self._is_pdf and self._pdf_page_count:
            pages_with_shapes = sorted(
                {p for p, st in self._pdf_page_states.items() if st.shapes}
                | ({self._current_pdf_page} if self.shapes else set())
            )
            total = sum(
                len(self.shapes) if p == self._current_pdf_page else len(self._pdf_page_states[p].shapes)
                for p in pages_with_shapes
            )
            label = ", ".join(str(p + 1) for p in pages_with_shapes) or "none"
            self.areas_group.setTitle(
                f"Areas — page {self._current_pdf_page + 1}/{self._pdf_page_count} "
                f"({total} total, on pages: {label})"
            )
        else:
            self.areas_group.setTitle("Areas")

    def rename_shape(self, sid: int) -> None:
        """Prompt to rename a shape; shared by the list and the context menu."""
        shp = self.get_shape(sid)
        if not shp:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename Shape", "Enter new name:", text=shp.name
        )
        if ok and new_name.strip():
            self.save_for_undo()
            shp.name = new_name.strip()
            if sid == self.selected_sid:
                self.name_edit.setText(shp.name)
            self.update_list()
            self.redraw_shapes()

    def _on_list_selection(self) -> None:
        rows = sorted(self.areas_list.row(it) for it in self.areas_list.selectedItems())
        sids = [self.shapes[r].sid for r in rows if 0 <= r < len(self.shapes)]
        self.selected_sids = sids
        cur = self.areas_list.currentRow()
        if 0 <= cur < len(self.shapes) and self.shapes[cur].sid in sids:
            self.selected_sid = self.shapes[cur].sid
        elif sids:
            self.selected_sid = sids[-1]
        else:
            self.selected_sid = None
        shp = self.get_shape(self.selected_sid) if self.selected_sid is not None else None
        if shp:
            self.name_edit.setText(shp.name)
            self._populate_geometry(shp)
        else:
            self.name_edit.setText("")
            self._set_geometry_enabled(False)
        self.restyle_selection()

    def _on_list_double_click(self, item: QListWidgetItem) -> None:
        row = self.areas_list.row(item)
        if 0 <= row < len(self.shapes):
            self.rename_shape(self.shapes[row].sid)

    def _show_list_context_menu(self, pos) -> None:
        item = self.areas_list.itemAt(pos)
        if item is None:
            return
        row = self.areas_list.row(item)
        if not (0 <= row < len(self.shapes)):
            return
        self._select_for_context_menu(self.shapes[row].sid)
        self._popup_shape_menu(self.areas_list.mapToGlobal(pos))

    def _select_for_context_menu(self, sid: int) -> None:
        """Make `sid` the key object for a right-click, WITHOUT collapsing a
        multi-selection the shape is already part of — otherwise "Merge
        Selected" could never be reached from a context menu."""
        if sid in self.selected_sids:
            if sid != self.selected_sid:
                self.selected_sid = sid
                self._sync_key_ui()
            return
        self.set_selected(sid)

    def _popup_shape_menu(self, global_pos) -> None:
        """Right-click menu for the selected shape (shared by list and canvas)."""
        if self.selected_sid is None:
            return
        menu = QMenu(self)
        rename = menu.addAction("Rename…")
        duplicate = menu.addAction("Duplicate")
        menu.addSeparator()
        merge = menu.addAction("Merge Selected")
        merge.setEnabled(self._can_merge())
        split = menu.addAction("Split")
        split.setEnabled(self._can_split())
        menu.addSeparator()
        zoom = menu.addAction("Zoom to Selection")
        menu.addSeparator()
        delete = menu.addAction("Delete")
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen == rename:
            self.rename_shape(self.selected_sid)
        elif chosen == duplicate:
            self.duplicate_selected()
        elif chosen == merge:
            self.merge_selected()
        elif chosen == split:
            self.split_selected()
        elif chosen == zoom:
            self.zoom_to_selection()
        elif chosen == delete:
            self.delete_selected()

    def apply_name(self) -> None:
        if self.selected_sid is None:
            return
        shp = self.get_shape(self.selected_sid)
        if shp:
            new_name = self.name_edit.text().strip()
            if new_name:
                self.save_for_undo()
                shp.name = new_name
                self.update_list()
                self.redraw_shapes()

    def duplicate_selected(self) -> None:
        if self.selected_sid is None:
            return
        shp = self.get_shape(self.selected_sid)
        if shp:
            self.save_for_undo()
            # Offset the clone, then keep it on the canvas: duplicating a shape
            # at the right/bottom edge used to place the copy partly (or wholly)
            # outside the image. keep_size translates it back in, like a nudge.
            # Cloning (rather than rebuilding from a bbox) is what carries a
            # multirect's pieces across; translate then moves the whole unit.
            ox1, oy1, ox2, oy2 = shp.bbox()
            x1, y1, _x2, _y2 = self._clamp_bbox(
                ox1 + 20, oy1 + 20, ox2 + 20, oy2 + 20, keep_size=True
            )
            new_shp = shp.clone()
            new_shp.sid = self.next_sid
            new_shp.name = f"{shp.name}_copy"
            new_shp.page = shp.page if self._is_pdf else 0
            new_shp.translate(x1 - ox1, y1 - oy1)
            self.shapes.append(new_shp)
            self.next_sid += 1
            self.update_list()
            self.set_selected(new_shp.sid)
            self.status_bar.showMessage(f"Duplicated: {shp.name}")

    # -------------------------
    # Merge / Split (irregular areas)
    # -------------------------

    def _selected_shapes(self) -> List[Shape]:
        """The live shapes behind selected_sids, dropping ids that are gone."""
        return [s for s in (self.get_shape(i) for i in self.selected_sids) if s is not None]

    def _can_merge(self) -> bool:
        """2+ selected, every one a rect or a merged area. self.shapes only ever
        holds the current page, so 'same page' comes for free."""
        picked = self._selected_shapes()
        return len(picked) >= 2 and all(s.kind in ("rect", "multirect") for s in picked)

    def _can_split(self) -> bool:
        picked = self._selected_shapes()
        return len(picked) == 1 and picked[0].kind == "multirect"

    def merge_selected(self) -> None:
        """Fuse the selected rects/merged areas into one irregular area."""
        picked = self._selected_shapes()
        if len(picked) < 2:
            self.status_bar.showMessage("Select 2+ shapes (Ctrl+click) to merge.")
            return
        before = self._snapshot_state()
        try:
            merged = merge_shapes(picked, self.next_sid)
        except ValueError as e:
            self.status_bar.showMessage(f"Cannot merge: {e}")
            return
        self.commit_undo_snapshot(before)     # one step; also ends a nudge burst
        self.shapes = splice_shapes(self.shapes, picked, [merged])
        self.next_sid += 1
        self.update_list()
        self.set_selected(merged.sid)
        self.status_bar.showMessage(
            f"Merged {len(picked)} shapes into '{merged.name}' ({len(merged.rects)} pieces).")

    def split_selected(self) -> None:
        """Break the selected merged area back into one plain rect per piece."""
        picked = self._selected_shapes()
        if not self._can_split():
            self.status_bar.showMessage("Select one merged area to split.")
            return
        shp = picked[0]
        before = self._snapshot_state()
        parts = split_shape(shp, self.next_sid)
        self.commit_undo_snapshot(before)     # one step; also ends a nudge burst
        self.shapes = splice_shapes(self.shapes, [shp], parts)
        self.next_sid += len(parts)
        self.selected_sids = [p.sid for p in parts]
        self.selected_sid = parts[0].sid
        self.update_list()
        self._sync_key_ui()
        self.status_bar.showMessage(f"Split '{shp.name}' into {len(parts)} rect(s).")

    def delete_selected(self) -> None:
        if not self.selected_sids and self.selected_sid is None:
            return
        self.save_for_undo()
        doomed = set(self.selected_sids) or {self.selected_sid}
        self.shapes = [s for s in self.shapes if s.sid not in doomed]
        self.selected_sid = None
        self.selected_sids = []
        self.update_list()
        self.redraw_shapes()

    def clear_all(self) -> None:
        other_pages_have_shapes = self._is_pdf and any(
            bool(state.shapes) for state in self._pdf_page_states.values()
        )
        has_shapes = bool(self.shapes) or other_pages_have_shapes

        if not has_shapes:
            return

        # Per-page undo cannot restore shapes on other PDF pages, so be explicit
        # rather than silently destroying unrecoverable work.
        if other_pages_have_shapes:
            msg = (
                "Delete all shapes on EVERY page of this PDF?\n\n"
                "Shapes on other pages cannot be restored with Undo."
            )
        else:
            msg = "Delete all shapes?"
        reply = QMessageBox.question(
            self, "Clear All", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.save_for_undo()
            if self._is_pdf:
                self._pdf_page_states = {}
            self.shapes.clear()
            self.selected_sid = None
            self.selected_sids = []
            self.next_sid = 1
            self.update_list()
            self.redraw_shapes()
            self._update_title()

    # -------------------------
    # Normalization
    # -------------------------

    def _clamp_bbox(self, x1: float, y1: float, x2: float, y2: float, keep_size: bool) -> Tuple[float, float, float, float]:
        if not self.canvas.pixmap_item:
            return x1, y1, x2, y2
        w, h = self.canvas.image_size
        return _clamp_bbox_wh(x1, y1, x2, y2, w, h, keep_size)

    def normalize_annotations(self) -> None:
        """
        Normalize/align zones into neat columns. Rectangles, circles, and chamfer
        rectangles are each aligned by column. This is geometry-only normalization
        (layout alignment), not OCR/text work.
        """
        if not self.canvas.pixmap_item:
            QMessageBox.warning(self, "No image", "Open an image first.")
            return

        rects = [s for s in self.shapes if s.kind == "rect"]
        circles = [s for s in self.shapes if s.kind == "circle"]
        chamfers = [s for s in self.shapes if s.kind == "chamfer"]
        if max(len(rects), len(circles), len(chamfers)) < 2:
            return

        self.save_for_undo()
        sel = self.selected_sid
        w, h = self.canvas.image_size

        try:
            normalize_shapes(self.shapes, w, h)
        except Exception as e:
            QMessageBox.critical(self, "Normalize failed", str(e))
            return

        self.update_list()
        self.set_selected(sel)
        self.redraw_shapes()
        self.status_bar.showMessage(
            f"Normalized {len(rects)} rect(s), {len(circles)} circle(s), {len(chamfers)} chamfer(s)."
        )

    # -------------------------
    # Document lines (place / size / align)
    # -------------------------

    def _line_key(self) -> int:
        """Cache key for the current view's detected lines."""
        return self._current_pdf_page if self._is_pdf else 0

    def _current_lines(self) -> Tuple[list, list]:
        return self._line_cache.get(self._line_key(), ([], []))

    def has_lines(self) -> bool:
        h, v = self._current_lines()
        return bool(h or v)

    def _detection_ready(self, announce: bool) -> bool:
        """Common guard for the detect_* slots: a page must be loaded."""
        if self._source_image is None or not self.canvas.pixmap_item:
            if announce:
                QMessageBox.warning(self, "No image", "Open an image or PDF first.")
            return False
        return True

    def _run_detection(self, work: Callable[[], object],
                       apply: Callable[[object], None]) -> None:
        """Run a detector and apply its result on the GUI thread.

        Synchronous unless `async_detection` is on, so the CLI and the tests
        keep a deterministic, event-loop-free path. In the interactive GUI the
        work goes to a pool thread instead -- a full-page pass takes seconds on
        a large scan, and the wait cursor alone does not stop the freeze. Only
        one pass runs at a time; the shortcut is ignored while one is in flight.
        """
        if self._detection_busy:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        if not self.async_detection:
            try:
                result = work()
            finally:
                QApplication.restoreOverrideCursor()
            apply(result)
            return
        self._detection_busy = True
        worker = _DetectionWorker(work)
        # The page the pass is about. Opening another document or flipping to
        # another PDF page replaces _source_image, and a result computed from
        # the old one must not be applied to the new one.
        image = self._source_image
        worker.signals.finished.connect(
            lambda result, error: self._finish_detection(apply, image, result, error))
        # Keep the worker alive: the pool thread still touches the runnable
        # after run() returns, and this is its last Python reference. The next
        # launch overwrites it, so it is never nulled here.
        self._detection_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _finish_detection(self, apply: Callable[[object], None], image,
                          result, error) -> None:
        """Worker completion, back on the GUI thread."""
        self._detection_busy = False
        QApplication.restoreOverrideCursor()
        if error is not None:
            raise error
        if self._source_image is not image:
            self.status_bar.showMessage(
                "Detection discarded: the document changed while it ran.")
            return
        apply(result)

    def _run_cached_detection(self, detector: Callable[[object], object], cache: Dict,
                              show_attr: str, checkbox_attr: str,
                              message: Callable[[object], str], announce: bool) -> None:
        """Shared body of the three overlay detectors (lines, text regions,
        high-contrast blocks).

        Each one runs its detector on the current page, stores the result in its
        own per-page `cache`, drops the derived snap lines, switches its overlay
        on (keeping the side-panel checkbox in step), redraws, and reports."""
        if not self._detection_ready(announce):
            return
        image = self._source_image
        key = self._line_key()

        def apply(result) -> None:
            cache[key] = result
            self._invalidate_snap_lines(key)
            if not getattr(self, show_attr):
                setattr(self, show_attr, True)
                checkbox = getattr(self, checkbox_attr, None)
                if checkbox is not None:
                    checkbox.blockSignals(True)
                    checkbox.setChecked(True)
                    checkbox.blockSignals(False)
            self.redraw_shapes()
            if announce:
                self.status_bar.showMessage(message(result))

        self._run_detection(lambda: detector(image), apply)

    def detect_lines(self, *, announce: bool = True) -> None:
        """Detect rules/underlines in the current page and cache them."""
        self._run_cached_detection(
            detect_document_lines, self._line_cache, "show_guides", "show_guides_cb",
            lambda result: (
                f"Detected {len(result[0])} horizontal and {len(result[1])} vertical "
                f"line(s). Enable 'Snap to lines' to align fields."
            ),
            announce,
        )

    def _snap_lines(self) -> Tuple[list, list]:
        """The current page's derived (h, v) snap lines, cached.

        Document rules + high-contrast block edges + text-region edges, in that
        order (ties resolve to the last match, so the order is part of the
        behavior). Deriving this cost a pass over every text box on every
        mouse-move of a drag; it only ever changes when a detector runs, so it
        is cached beside the caches it is derived from and dropped by every
        writer of those (see _invalidate_snap_lines)."""
        key = self._line_key()
        derived = self._snap_cache.get(key)
        if derived is None:
            h_lines, v_lines = self._current_lines()
            # High-contrast block edges act as extra snap rules (zero-thickness),
            # but are not exclusion zones the way text is, so they fold into the
            # lines instead of into the text boxes.
            hc_h, hc_v = _text_boxes_to_lines(self._current_hc_regions())
            text_h, text_v = _text_boxes_to_lines(self._current_text_boxes())
            derived = (list(h_lines) + hc_h + text_h, list(v_lines) + hc_v + text_v)
            self._snap_cache[key] = derived
        return derived

    def _invalidate_snap_lines(self, key: Optional[int] = None) -> None:
        """Drop the derived snap lines for one page (or all of them). Must be
        called by everything that writes _line_cache/_text_cache/_hc_cache."""
        if key is None:
            self._snap_cache.clear()
        else:
            self._snap_cache.pop(key, None)

    def _snap_bbox(self, x1: float, y1: float, x2: float, y2: float,
                   keep_size: bool = False) -> Tuple[Tuple[float, float, float, float], bool]:
        """Snap a bbox to the current page's detected lines and text regions.

        Detected text-region edges act as extra snap lines, and (when sizing) the
        field is clipped out of any text region so it never covers printed text."""
        h_lines, v_lines = self._snap_lines()
        text_boxes = self._current_text_boxes()
        if not (h_lines or v_lines or text_boxes):
            return (x1, y1, x2, y2), False
        return _snap_and_exclude(
            x1, y1, x2, y2, h_lines, v_lines, text_boxes,
            self.line_snap_tol, keep_size=keep_size,
        )

    def maybe_snap_live(self, x1: float, y1: float, x2: float, y2: float,
                        keep_size: bool) -> Tuple[float, float, float, float]:
        """Snap during an active drag when snapping is enabled; pass-through
        otherwise. Returns the (possibly snapped) bbox."""
        if not self.snap_to_lines:
            return (x1, y1, x2, y2)
        (nx1, ny1, nx2, ny2), _ = self._snap_bbox(x1, y1, x2, y2, keep_size=keep_size)
        return (nx1, ny1, nx2, ny2)

    def snap_fields_to_lines(self) -> None:
        """Align fields to detected lines and text regions: the selected field if
        one is selected, otherwise every field on the page. Field edges snap to
        document rules and to text-region edges, and each field is clipped out of
        any text region so it never covers printed text."""
        if not (self.has_lines() or self._current_text_boxes() or self._current_hc_regions()):
            QMessageBox.information(
                self, "Nothing to align to",
                "Run 'Detect Lines', 'Detect Text', and/or 'Detect High-Contrast' "
                "first so there are lines or regions to align to."
            )
            return
        targets = (
            [self.get_shape(self.selected_sid)] if self.selected_sid is not None
            else list(self.shapes)
        )
        targets = [s for s in targets if s is not None]
        if not targets:
            return

        self.save_for_undo()
        changed = 0
        for shp in targets:
            x1, y1, x2, y2 = shp.bbox()
            snapped_bbox, snapped = self._snap_bbox(x1, y1, x2, y2, keep_size=False)
            if snapped and apply_snapped_bbox(shp, snapped_bbox):
                changed += 1

        sel = self.selected_sid
        self.update_list()
        self.set_selected(sel)
        self.redraw_shapes()
        self.status_bar.showMessage(f"Snapped {changed} field(s) to lines.")

    def toggle_snap_to_lines(self, checked: bool) -> None:
        self.snap_to_lines = bool(checked)
        self.status_bar.showMessage(
            f"Snap to lines: {'on' if self.snap_to_lines else 'off'}"
        )

    def toggle_show_guides(self, checked: bool) -> None:
        self.show_guides = bool(checked)
        self.redraw_shapes()

    # -------------------------
    # Text regions (where text is)
    # -------------------------

    def _current_text_boxes(self) -> list:
        return self._text_cache.get(self._line_key(), [])

    def detect_text(self, *, announce: bool = True) -> None:
        """Detect strings of characters on the current page and box each one,
        marking where printed text is (the blanks between are field candidates)."""
        self._run_cached_detection(
            detect_text_boxes, self._text_cache, "show_text_regions", "show_text_cb",
            lambda boxes: (
                f"Found {len(boxes)} text string(s). The gaps between them mark "
                f"where fields go."
            ),
            announce,
        )

    def toggle_show_text_regions(self, checked: bool) -> None:
        self.show_text_regions = bool(checked)
        self.redraw_shapes()

    # -------------------------
    # High-contrast regions (solid bars / inverted headers)
    # -------------------------

    def _current_hc_regions(self) -> list:
        return self._hc_cache.get(self._line_key(), [])

    def detect_high_contrast(self, *, announce: bool = True) -> None:
        """Detect solid high-contrast blocks (filled bars, white-on-black
        headers) on the current page. Their edges become extra snap targets;
        no shapes are created."""
        self._run_cached_detection(
            detect_high_contrast_regions, self._hc_cache, "show_hc_regions", "show_hc_cb",
            lambda regions: (
                f"Found {len(regions)} high-contrast region(s). "
                f"Enable 'Snap to lines' to align field edges to them."
            ),
            announce,
        )

    def toggle_show_hc_regions(self, checked: bool) -> None:
        self.show_hc_regions = bool(checked)
        self.redraw_shapes()

    def add_text_regions_as_fields(self) -> None:
        """Materialize the detected text-string boxes as rect fields."""
        boxes = self._current_text_boxes()
        if not boxes:
            QMessageBox.information(
                self, "No text regions",
                "Run 'Detect Text' first to find text strings."
            )
            return

        def covered(cx: float, cy: float) -> bool:
            for s in self.shapes:
                bx1, by1, bx2, by2 = s.bbox()
                if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                    return True
            return False

        new = [b for b in boxes if not covered((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)]
        if not new:
            self.status_bar.showMessage("All text regions are already annotated.")
            return
        self.save_for_undo()
        page = self._current_pdf_page if self._is_pdf else 0
        first = None
        for (x1, y1, x2, y2) in new:
            shp = Shape(self.next_sid, "rect", f"text_{self.next_sid}", x1, y1, x2, y2, page=page)
            self.shapes.append(shp)
            if first is None:
                first = shp.sid
            self.next_sid += 1
        self.update_list()
        self.set_selected(first)
        self.redraw_shapes()
        self.status_bar.showMessage(f"Added {len(new)} text region(s) as fields.")

    def detect_checkboxes(self) -> None:
        """Detect check-mark boxes on the current page and add a rect field for
        each new one (skipping boxes already covered by an existing shape)."""
        if not self._detection_ready(announce=True):
            return
        image = self._source_image
        page = self._current_pdf_page if self._is_pdf else 0

        # Skip a detected box if its center already falls inside an existing
        # shape, so re-running is idempotent and won't duplicate fields.
        def covered(cx: float, cy: float) -> bool:
            for s in self.shapes:
                bx1, by1, bx2, by2 = s.bbox()
                if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                    return True
            return False

        def apply(boxes) -> None:
            new_boxes = [
                (x1, y1, x2, y2) for (x1, y1, x2, y2) in boxes
                if not covered((x1 + x2) / 2.0, (y1 + y2) / 2.0)
            ]

            if not new_boxes:
                msg = (
                    "No check-mark boxes detected on this page."
                    if not boxes else
                    "All detected check-mark boxes are already annotated."
                )
                self.status_bar.showMessage(msg)
                return

            self.save_for_undo()
            first_new = None
            for (x1, y1, x2, y2) in new_boxes:
                shp = Shape(self.next_sid, "rect", f"checkbox_{self.next_sid}",
                            x1, y1, x2, y2, page=page)
                self.shapes.append(shp)
                if first_new is None:
                    first_new = shp.sid
                self.next_sid += 1

            self.update_list()
            self.set_selected(first_new)
            self.redraw_shapes()
            self.status_bar.showMessage(f"Detected {len(new_boxes)} check-mark box(es).")

        self._run_detection(lambda: detect_checkbox_squares(image), apply)

    # -------------------------
    # Markers (QR / AprilTag) and barcodes
    # -------------------------

    def _add_detections_as_fields(self, dets: List[Detection], noun: str, announce: bool) -> None:
        """Add a rect field per detection, named by decoded payload (or kind+id),
        skipping any whose centre is already inside an existing shape so re-runs
        are idempotent."""
        def covered(cx: float, cy: float) -> bool:
            for s in self.shapes:
                bx1, by1, bx2, by2 = s.bbox()
                if bx1 <= cx <= bx2 and by1 <= cy <= by2:
                    return True
            return False

        new = [d for d in dets
               if not covered((d["bbox"][0] + d["bbox"][2]) / 2.0,
                              (d["bbox"][1] + d["bbox"][3]) / 2.0)]
        if not new:
            if announce:
                self.status_bar.showMessage(
                    f"No {noun}s detected on this page." if not dets
                    else f"All detected {noun}s are already annotated.")
            return

        self.save_for_undo()
        page = self._current_pdf_page if self._is_pdf else 0
        first = None
        for d in new:
            x1, y1, x2, y2 = d["bbox"]
            kind = d.get("kind") or noun
            payload = _clean_name((d.get("payload") or "").replace("\n", " ").strip())[:40]
            name = f"{kind}_{payload}" if payload else f"{kind}_{self.next_sid}"
            shp = Shape(self.next_sid, "rect", name, x1, y1, x2, y2, page=page)
            self.shapes.append(shp)
            if first is None:
                first = shp.sid
            self.next_sid += 1
        self.update_list()
        self.set_selected(first)
        self.redraw_shapes()
        if announce:
            self.status_bar.showMessage(f"Detected {len(new)} {noun}(s).")

    def detect_markers(self, *, announce: bool = True) -> None:
        """Detect QR codes and AprilTag/ArUco markers and add a field for each."""
        if not self._detection_ready(announce):
            return
        if _get_cv2() is None and _get_pyzbar() is None and _get_apriltag_detector() is None:
            if announce:
                QMessageBox.information(
                    self, "Marker detection unavailable",
                    "Install opencv-contrib-python (and optionally pyzbar + the "
                    "system libzbar, and pupil-apriltags) to detect QR codes and "
                    "AprilTags.")
            return
        image = self._source_image
        self._run_detection(lambda: detect_markers(image),
                            lambda dets: self._add_detections_as_fields(dets, "marker", announce))

    def detect_barcodes(self, *, announce: bool = True) -> None:
        """Detect 1D barcodes and add a field for each."""
        if not self._detection_ready(announce):
            return
        if _get_cv2() is None and _get_pyzbar() is None:
            if announce:
                QMessageBox.information(
                    self, "Barcode detection unavailable",
                    "Install opencv-contrib-python (and optionally pyzbar + the "
                    "system libzbar) to detect barcodes.")
            return
        image = self._source_image
        self._run_detection(lambda: detect_barcodes(image),
                            lambda dets: self._add_detections_as_fields(dets, "barcode", announce))

    # -------------------------
    # Drawing
    # -------------------------

    def _theme_colors(self) -> Dict:
        """Color palette for the current theme, allocated once per redraw."""
        if self.current_theme == "dark":
            return {
                "outline": QColor("#4499ff"),
                "selected": QColor("#ff6666"),
                "fill": {
                    "rect": QColor(42, 74, 90, 100),
                    "circle": QColor(42, 90, 42, 100),
                    "chamfer": QColor(90, 58, 90, 100),
                },
                "selected_fill": QColor(90, 58, 58, 100),
                "label": QColor("#eee"),
                "handle_fill": QColor("#333"),
                "handle_outline": QColor("#fff"),
            }
        return {
            "outline": QColor("#1166cc"),
            "selected": QColor("#d11"),
            "fill": {
                "rect": QColor(173, 216, 230, 100),
                "circle": QColor(144, 238, 144, 100),
                "chamfer": QColor(221, 160, 221, 100),
            },
            "selected_fill": QColor(255, 204, 203, 100),
            "label": QColor("#111"),
            "handle_fill": QColor("#fff"),
            "handle_outline": QColor("#000"),
        }

    def grab_scene_image(self) -> "QImage":
        """Render the current page (source image + every shape/overlay) to a
        QImage at 1:1, exactly as drawn on the canvas — the annotated content the
        user sees, independent of zoom/pan."""
        scene = self.canvas.scene
        # Render the image area only — the sceneRect carries scroll margins that
        # must not appear in the screenshot.
        if self.canvas.pixmap_item is not None:
            rect = self.canvas.pixmap_item.sceneBoundingRect()
        else:
            rect = self.canvas.sceneRect()
        w = max(1, int(round(rect.width())))
        h = max(1, int(round(rect.height())))
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor("white"))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scene.render(painter, QRectF(0, 0, w, h), rect)
        painter.end()
        return img

    def save_screenshot(self, path: str, *, window: bool = False) -> bool:
        """Save a screenshot to `path`. window=True grabs the whole application
        window as the user sees it (panels, list, canvas); otherwise the
        annotated page is rendered at 1:1. Returns True on success."""
        if window:
            return bool(self.grab().save(path))
        return bool(self.grab_scene_image().save(path))

    def _ask_save_path(self, caption: str, name_filter: str, suffix: str) -> Optional[str]:
        """Pick a save path with the extension applied BEFORE the overwrite
        check. The static getSaveFileName() asks Qt about exactly what was
        typed, so "project" passed the check and then clobbered an existing
        project.json without a word; setDefaultSuffix makes Qt test the real
        name. Returns None when the user cancels."""
        dialog = QFileDialog(self, caption)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter(name_filter)
        dialog.setDefaultSuffix(suffix)
        if not dialog.exec():
            return None
        selected = dialog.selectedFiles()
        # Belt and braces: setDefaultSuffix does nothing on the platforms that
        # use a native picker, so the old suffix fix-up still runs.
        return _ensure_suffix(selected[0], "." + suffix) if selected else None

    def save_screenshot_dialog(self) -> None:
        """File menu action: save a PNG screenshot of the current view."""
        if not self.canvas.pixmap_item:
            QMessageBox.warning(self, "No image", "Open an image or PDF first.")
            return
        path = self._ask_save_path("Save Screenshot", "PNG Image (*.png)", "png")
        if not path:
            return
        # Drop the selection so resize handles don't appear in the screenshot.
        self.set_selected(None)
        ok = self.save_screenshot(path, window=False)
        self.status_bar.showMessage(
            f"Saved screenshot -> {path}" if ok else "Failed to save screenshot.")

    def redraw_shapes(self) -> None:
        """Rebuild every scene item from scratch. Only for changes the existing
        items cannot express: a different shape SET (add/delete/load/page/undo),
        a kind change, a theme change, or a label/fill toggle.

        Selection changes go through restyle_selection() and live move/resize
        through update_active_graphics; neither destroys or creates items."""
        if not self.canvas.pixmap_item:
            return

        scene = self.canvas.scene
        self.canvas.setUpdatesEnabled(False)
        try:
            # Remove old shape items
            for item in scene.items():
                if item != self.canvas.pixmap_item:
                    scene.removeItem(item)
            self.canvas._shape_items = {}

            colors = self._theme_colors()
            show_fill = self.show_fill_cb.isChecked()
            show_labels = self.show_labels_cb.isChecked()

            # Detected guides/regions sit beneath the shapes.
            if self.show_guides:
                self._draw_line_guides()
            if self.show_text_regions:
                self._draw_text_regions()
            if self.show_hc_regions:
                self._draw_hc_regions()

            for shp in self.shapes:
                self._build_shape_items(shp, colors, show_fill, show_labels)
        finally:
            self.canvas.setUpdatesEnabled(True)

    def _draw_line_guides(self) -> None:
        h_lines, v_lines = self._current_lines()
        if not (h_lines or v_lines):
            return
        scene = self.canvas.scene
        color = QColor(255, 140, 0, 130) if self.current_theme == "dark" else QColor(0, 120, 215, 110)
        pen = QPen(color)
        pen.setWidth(0)  # cosmetic: 1px on screen regardless of zoom
        pen.setStyle(Qt.PenStyle.DashLine)
        for y, x0, x1, *_ in h_lines:
            scene.addLine(x0, y, x1, y, pen)
        for x, y0, y1, *_ in v_lines:
            scene.addLine(x, y0, x, y1, pen)

    def _draw_text_regions(self) -> None:
        boxes = self._current_text_boxes()
        if not boxes:
            return
        scene = self.canvas.scene
        color = QColor(0, 200, 160) if self.current_theme == "dark" else QColor(0, 150, 80)
        pen = QPen(color)
        pen.setWidth(0)  # cosmetic 1px
        fill = QColor(color.red(), color.green(), color.blue(), 40)
        brush = QBrush(fill)
        for x1, y1, x2, y2 in boxes:
            scene.addRect(x1, y1, x2 - x1, y2 - y1, pen, brush)

    def _draw_hc_regions(self) -> None:
        regions = self._current_hc_regions()
        if not regions:
            return
        scene = self.canvas.scene
        color = QColor(230, 80, 200) if self.current_theme == "dark" else QColor(160, 0, 160)
        pen = QPen(color)
        pen.setWidth(0)  # cosmetic 1px
        pen.setStyle(Qt.PenStyle.DashLine)
        for x1, y1, x2, y2 in regions:
            scene.addRect(x1, y1, x2 - x1, y2 - y1, pen)

    def _shape_pen_brush(self, shp: Shape, colors: Dict, show_fill: bool) -> Tuple[QPen, QBrush]:
        """Outline pen and fill brush for a shape in its current selection state.
        Shared by the full rebuild and the in-place restyle: if these two ever
        diverged, a selection click would paint the shape differently from a
        redraw of the same state."""
        selected = shp.sid in self.selected_sids
        pen = QPen(colors["selected"] if selected else colors["outline"])
        pen.setWidth(3 if selected else 2)
        fill = colors["selected_fill"] if selected else colors["fill"].get(shp.kind, colors["fill"]["rect"])
        return pen, (QBrush(fill) if show_fill else QBrush(Qt.BrushStyle.NoBrush))

    def _build_shape_items(self, shp: Shape, colors: Dict, show_fill: bool, show_labels: bool) -> None:
        scene = self.canvas.scene
        is_key = shp.sid == self.selected_sid   # the anchor: only it gets handles
        x1, y1, x2, y2 = shp.bbox()
        pen, brush = self._shape_pen_brush(shp, colors, show_fill)

        if shp.kind == "circle":
            item = scene.addEllipse(x1, y1, x2 - x1, y2 - y1, pen, brush)
        elif shp.kind == "chamfer":
            polygon = self.canvas._make_chamfer_polygon(x1, y1, x2, y2, shp.chamfer)
            item = scene.addPolygon(polygon, pen, brush)
        elif shp.kind == "multirect":
            item = scene.addPath(self.canvas._make_multirect_path(shp), pen, brush)
        else:
            item = scene.addRect(x1, y1, x2 - x1, y2 - y1, pen, brush)

        label_item = None
        if show_labels:
            label_item = scene.addText(shp.name)
            label_item.setPos(x1 + 4, y1 + 4)
            label_item.setDefaultTextColor(colors["label"])

        handle_items = self._make_handles(shp, colors) if is_key else []

        self.canvas._shape_items[shp.sid] = {
            "kind": shp.kind,
            "item": item,
            "label": label_item,
            "handles": handle_items,
        }

    def _make_handles(self, shp: Shape, colors: Dict, before=None) -> List:
        """The four corner grab handles for the key shape.

        `before`: stack the handles immediately in front of this item, so their
        paint order matches a full rebuild (where a shape's handles are added
        right after its own item, hence beneath any later shape).

        A merged area never gets handles: dragging one would have to resize the
        envelope, which says nothing about where its pieces go (Split first)."""
        if shp.kind == "multirect":
            return []
        scene = self.canvas.scene
        x1, y1, x2, y2 = shp.bbox()
        handles = []
        for hx, hy in [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]:
            handle_pen = QPen(colors["handle_outline"])
            handle_brush = QBrush(colors["handle_fill"])
            # Draw the handle centered at the item origin and ignore the view
            # transform so it stays a constant ~12px on screen at any zoom,
            # matching the screen-space grab tolerance in _hit_handle.
            h = scene.addRect(-6, -6, 12, 12, handle_pen, handle_brush)
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h.setPos(hx, hy)
            if before is not None:
                h.stackBefore(before)
            handles.append(h)
        return handles

    def restyle_selection(self) -> None:
        """Apply a selection change to the items already on the canvas.

        A selection click used to destroy and rebuild every scene item (~4 per
        shape, including a QGraphicsTextItem each). Nothing about a selection
        change needs new items: only the pens/brushes and which shape carries
        the four handles differ. Geometry is re-synced at the same time, so
        callers that edit shapes in place (align, geometry panel) stay correct.

        Falls back to a full redraw when the shape SET changed (add/delete/kind
        change) -- the one thing restyling cannot express."""
        items = getattr(self.canvas, "_shape_items", {})
        if not self.canvas.pixmap_item:
            return
        if len(items) != len(self.shapes) or any(
            shp.sid not in items or items[shp.sid]["kind"] != shp.kind
            for shp in self.shapes
        ):
            self.redraw_shapes()
            return

        colors = self._theme_colors()
        show_fill = self.show_fill_cb.isChecked()
        for index, shp in enumerate(self.shapes):
            rec = items[shp.sid]
            pen, brush = self._shape_pen_brush(shp, colors, show_fill)
            rec["item"].setPen(pen)
            rec["item"].setBrush(brush)

            is_key = shp.sid == self.selected_sid
            if is_key and not rec["handles"]:
                rec["handles"] = self._make_handles(
                    shp, colors, before=self._first_item_after(index))
            elif rec["handles"] and not is_key:
                for h in rec["handles"]:
                    self.canvas.scene.removeItem(h)
                rec["handles"] = []
            self.update_active_graphics(shp)

    def _first_item_after(self, index: int):
        """The next shape's outline item in draw order, or None if `index` is
        the last shape with items on the canvas."""
        items = self.canvas._shape_items
        for shp in self.shapes[index + 1:]:
            rec = items.get(shp.sid)
            if rec is not None:
                return rec["item"]
        return None

    def update_active_graphics(self, shp: Shape) -> None:
        """Update only the given shape's existing graphics in place (fast path
        for live drag), falling back to a full redraw if items are missing."""
        rec = getattr(self.canvas, "_shape_items", {}).get(shp.sid)
        if not rec or rec["kind"] != shp.kind:
            self.redraw_shapes()
            return

        x1, y1, x2, y2 = shp.bbox()
        item = rec["item"]
        if shp.kind == "circle":
            item.setRect(x1, y1, x2 - x1, y2 - y1)
        elif shp.kind == "chamfer":
            item.setPolygon(self.canvas._make_chamfer_polygon(x1, y1, x2, y2, shp.chamfer))
        elif shp.kind == "multirect":
            item.setPath(self.canvas._make_multirect_path(shp))
        else:
            item.setRect(x1, y1, x2 - x1, y2 - y1)

        if rec["label"] is not None:
            rec["label"].setPos(x1 + 4, y1 + 4)

        handle_positions = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        for h_item, (hx, hy) in zip(rec["handles"], handle_positions):
            # Handles ignore the view transform; reposition by origin only.
            h_item.setPos(hx, hy)

    # -------------------------
    # Export/Import
    # -------------------------

    def _export_payload(self) -> Dict:
        if not self.canvas.pixmap_item:
            raise ValueError("No image loaded.")
        shape_payload = [s.as_export_dict() for s in self._all_shapes_for_export()]
        return {
            "schema_version": 1,
            "image": {
                "path": self.image_path,
                "width": self.canvas.image_size[0],
                "height": self.canvas.image_size[1],
            },
            "shapes": shape_payload,
        }

    def save_json_dialog(self) -> bool:
        """Save the project through the file picker.

        Returns True ONLY when a file was actually written. Callers that are
        about to discard the document (close, open, load) must not proceed on
        False: a cancelled picker or a failed write means the work is still
        only in memory.
        """
        if not self.canvas.pixmap_item:
            QMessageBox.warning(self, "Warning", "Open an image first.")
            return False
        path = self._ask_save_path("Save JSON", "JSON (*.json)", "json")
        if not path:
            return False
        try:
            text = json.dumps(self._export_payload(), indent=2, allow_nan=False)
            _atomic_write_text(path, text)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")
            return False
        self._has_unsaved_changes = False
        self._update_title()
        self.status_bar.showMessage(f"Saved: {os.path.basename(path)}")
        return True

    def _confirm_discard(self, action: str) -> bool:
        """Guard an action that would throw away unsaved annotations.

        Returns True when the caller may proceed: nothing to lose, the user
        saved successfully, or the user chose to discard. ``action`` completes
        "Save before ...?" ("closing" keeps the historical close-time wording).
        """
        if not (self._has_unsaved_changes and self._document_has_shapes()):
            return True
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            f"You have unsaved changes. Save before {action}?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No |
            QMessageBox.StandardButton.Cancel
        )
        if reply == QMessageBox.StandardButton.Yes:
            # A cancelled/failed save is NOT permission to discard.
            return self.save_json_dialog()
        return reply == QMessageBox.StandardButton.No

    def load_json_dialog(self) -> None:
        if not self._confirm_discard("loading another project"):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load JSON", filter="JSON (*.json);;All files (*.*)"
        )
        if path:
            self.load_json(path)

    def load_json(self, path: str) -> None:
        """Replace the document with a saved project.

        Wrapped so that no failure escapes into the Qt slot that called it (an
        unhandled exception there aborts the process under PyQt6). Guarantee:
        every validation failure detected before state is committed leaves the
        document exactly as it was, and the shape set is never partially
        replaced. The one thing a late failure can leave behind is the image
        referenced by the JSON, which is loaded (with its own error handling)
        before the shapes are parsed.
        """
        try:
            self._load_json(path)
        except Exception as e:  # noqa: BLE001 - GUI slot safety net
            QMessageBox.critical(self, "Error", f"Failed to load: {e}")

    def _load_json(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load: {e}")
            return

        if not isinstance(data, dict):
            QMessageBox.critical(self, "Error", "JSON root must be an object.")
            return

        img_info = data.get("image") if isinstance(data.get("image"), dict) else {}
        img_path = img_info.get("path")
        # A non-string path is treated as absent: os.path.exists(1) is True (it
        # reads an int as a file descriptor), which would hand garbage to the
        # image loader.
        if not isinstance(img_path, str):
            img_path = None
        if img_path and os.path.exists(img_path):
            if not self.load_image(img_path):
                # The file is there but unreadable. Installing these shapes over
                # the OLD image would silently mix two projects, so stop.
                QMessageBox.warning(
                    self, "Warning",
                    "The image referenced by this JSON could not be opened.\n"
                    "The project was not loaded."
                )
                return
        elif not self.canvas.pixmap_item:
            QMessageBox.warning(
                self, "Warning",
                "JSON references an image that doesn't exist.\nOpen the image manually first."
            )
            return

        raw_shapes = data.get("shapes")
        if not isinstance(raw_shapes, list):
            raw_shapes = []

        parsed_shapes, skipped, coerced_kinds = _parse_shapes(raw_shapes)
        max_sid = max((s.sid for s in parsed_shapes), default=0)

        out_of_range = 0
        if self._is_pdf and self._pdf_page_count:
            last_page = self._pdf_page_count - 1
            for s in parsed_shapes:
                if s.page < 0 or s.page > last_page:
                    out_of_range += 1
                    s.page = min(max(s.page, 0), last_page)
            # Clamping can drop two shapes from different pages onto the same
            # one, recreating the (page, sid) collision _parse_shapes just
            # resolved -- so dedup again now that pages are final.
            _dedupe_shape_ids(parsed_shapes)
            page_states: Dict[int, AppState] = {}
            for s in parsed_shapes:
                if s.page not in page_states:
                    page_states[s.page] = AppState(next_sid=1)
                page_states[s.page].shapes.append(s)

            for page_id, state in page_states.items():
                max_id = max([item.sid for item in state.shapes], default=0)
                state.next_sid = max_id + 1 if max_id > 0 else 1

            cur_state = page_states.get(self._current_pdf_page, AppState(next_sid=1))
            # Refresh the visible page bitmap so the canvas and self.shapes stay
            # in sync even when a PDF was already open (load_image was skipped).
            # Rendered BEFORE anything is committed: a page that fails to
            # rasterize must leave the open document untouched, not stranded
            # with new shapes over the previous page's bitmap.
            try:
                page_image = self._pdf_page_image(self._current_pdf_page).convert("RGBA")
            except Exception as e:
                QMessageBox.critical(
                    self, "Error",
                    f"Failed to render PDF page {self._current_pdf_page + 1}: {e}"
                )
                return
            self._pdf_page_states = page_states
            self.shapes = [s.clone() for s in cur_state.shapes]
            self.next_sid = cur_state.next_sid
            self._update_pdf_controls()
            self.image_path = os.path.abspath(img_path) if img_path else self.image_path
            self._source_image = page_image
            self._set_canvas_image()
        else:
            self.shapes = parsed_shapes
            self.next_sid = max_sid + 1 if max_sid > 0 else 1

        self.selected_sid = None
        self.selected_sids = []   # a selection from the replaced document
        self.undo_manager.clear()
        self._has_unsaved_changes = False
        self.update_list()
        self.redraw_shapes()
        self._update_title()

        if self._is_pdf and self._pdf_page_count:
            self.status_bar.showMessage(
                f"Loaded JSON: {os.path.basename(path)} "
                f"({len(parsed_shapes)} shapes across {self._pdf_page_count} pages)"
            )
        else:
            self.status_bar.showMessage(
                f"Loaded JSON: {os.path.basename(path)} ({len(parsed_shapes)} shapes)"
            )

        notes = []
        if skipped:
            notes.append(f"{skipped} malformed shape(s) skipped")
        if coerced_kinds:
            notes.append(f"{coerced_kinds} shape(s) with unknown kind set to 'rect'")
        if out_of_range:
            notes.append(f"{out_of_range} shape(s) on out-of-range pages clamped to a valid page")
        if notes:
            QMessageBox.warning(self, "Loaded with adjustments", "\n".join(notes))

    def export_csv_dialog(self) -> None:
        if not self.canvas.pixmap_item:
            QMessageBox.warning(self, "Warning", "Open an image first.")
            return
        path = self._ask_save_path("Export CSV", "CSV (*.csv)", "csv")
        if path:
            try:
                shapes = self._all_shapes_for_export()
                _atomic_write_text(path, _shapes_to_csv(shapes))
                self.status_bar.showMessage(
                    f"Exported CSV: {os.path.basename(path)} ({len(shapes)} shapes)"
                )
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export: {e}")

    def _all_shapes_for_export(self) -> List[Shape]:
        """Flat list of shapes for export. For PDFs this spans every page (with
        the current page's live edits), matching the JSON export."""
        if self._is_pdf and self._pdf_page_count:
            page_shapes: Dict[int, List[Shape]] = {
                page: [s.clone() for s in state.shapes]
                for page, state in self._pdf_page_states.items()
            }
            page_shapes[self._current_pdf_page] = [s.clone() for s in self.shapes]
            ordered: List[Shape] = []
            for page in sorted(page_shapes):
                ordered.extend(page_shapes[page])
            return ordered
        return [s.clone() for s in self.shapes]

    # -------------------------
    # Dialogs
    # -------------------------

    def _show_shortcuts(self) -> None:
        shortcuts = """
Keyboard Shortcuts:

TOOLS:
  V - Select/Move/Resize tool
  R - Rectangle tool
  C - Circle tool
  H - Chamfer Rect tool
  (single-letter shortcuts pause while typing in a field)

EDIT SELECTED SHAPE:
  Arrow keys - Nudge by 1 px
  Shift+Arrows - Nudge by 10 px
  Delete - Delete selected shape
  Ctrl+D - Duplicate selected shape
  Ctrl+M - Merge the selected rects into one irregular area
      (Edit > Split, or the right-click menu, breaks a merged
       area back into plain rects)
  Ctrl+Z - Undo    Ctrl+Y - Redo
  N - Normalize/align shapes into columns

DETECTION:
  L - Detect the document's rules/underlines
      (then enable 'Snap fields to lines' to place/size/align
       fields against them, or 'Snap fields to lines now' to
       align existing fields)
  B - Detect check-mark boxes and add a field for each
  X - Detect text: box each string of characters (toggle
      'Show text regions'); the gaps mark where fields go
  G - Detect high-contrast regions (solid bars / inverted
      headers); their edges become snap targets
  M - Detect markers: QR codes and AprilTag/ArUco fiducials
      (decoded value becomes the field name)
  K - Detect barcodes (1D); one field per barcode

NAVIGATION:
  Alt+Left - Previous PDF page
  Alt+Right - Next PDF page

FILE:
  Ctrl+O - Open image / PDF
  Ctrl+S - Save JSON

VIEW:
  Ctrl + Scroll Wheel - Zoom at cursor
  Middle Mouse Button - Pan view
  Z - Zoom to selection
  T - Toggle theme

SELECT & ALIGN:
  Ctrl+Click - Add/remove from selection (last = key/anchor)
  Shift+Drag - Marquee-select shapes the rubber-band touches
  Align panel - Align edges/centres or match size to the key object

OTHER:
  Enter - Apply name to selected shape
  Right-click - Shape actions menu (canvas or list)
  Double-click list item - Rename shape
  F1 - Show this help
"""
        QMessageBox.information(self, "Keyboard Shortcuts", shortcuts.strip())

    def _show_about(self) -> None:
        about = """
Template Annotator (PyQt6)

Draw and name regions on images or PDFs for template definitions.

Supports:
- Rectangle, Circle, and Chamfer (cut-corner) shapes
- Multi-page PDFs with per-page annotations
- Numeric bbox editing and column normalization
- Line detection: snap fields to the document's rules/underlines
- Export to JSON or CSV (all pages)

Light/dark UI themes; dark mode shows the document as a negative
(toggle via View > Invert Document Colors).
"""
        QMessageBox.about(self, "About", about.strip())

    def _document_has_shapes(self) -> bool:
        if self.shapes:
            return True
        if self._is_pdf:
            return any(bool(state.shapes) for state in self._pdf_page_states.values())
        return False

    def closeEvent(self, event) -> None:
        # Cancel -- and "Yes, save" whose save never wrote a file -- keep the
        # window (and the annotations) alive.
        if not self._confirm_discard("closing"):
            event.ignore()
            return
        event.accept()
        self._close_pdf()


# ============================================================================
# CLI helpers
# ============================================================================


def _load_media_dimensions(path: str) -> Tuple[int, int]:
    """Return image width/height for a raster image or PDF document."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"File does not exist: {path}")

    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        if fitz is None:
            raise RuntimeError(
                "PyMuPDF is required for PDF dimension support: pip install PyMuPDF"
            )

        doc = fitz.open(path)
        try:
            if doc.page_count < 1:
                raise ValueError("PDF has no pages.")
            page = doc.load_page(0)
            rect = page.rect
            # Shapes are authored in the GUI's rendered-pixel space, which is the
            # PDF point size scaled by PDF_RENDER_SCALE. Report that same space so
            # `normalize-json --image-size-source <pdf>` matches exported JSON.
            return (
                int(round(rect.width * PDF_RENDER_SCALE)),
                int(round(rect.height * PDF_RENDER_SCALE)),
            )
        finally:
            doc.close()

    with Image.open(path) as img:
        return img.width, img.height


def _cli_print_info(path: str) -> int:
    """Print image/PDF metadata to stdout."""
    if not os.path.exists(path):
        print(f"Error: file does not exist: {path}", file=sys.stderr)
        return 1

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            if fitz is None:
                return _cli_err("PyMuPDF is required for PDF metadata: pip install PyMuPDF")
            doc = fitz.open(path)
            try:
                if doc.page_count < 1:
                    return _cli_err(f"PDF has no pages: {path}")
                page = doc.load_page(0)
                rect = page.rect
                px_w = int(round(rect.width * PDF_RENDER_SCALE))
                px_h = int(round(rect.height * PDF_RENDER_SCALE))
                print(f"PDF: {path}")
                print(f"Pages: {doc.page_count}")
                print(f"Page 1 size (pt): {rect.width:.2f} x {rect.height:.2f}")
                print(f"Page 1 size (px @ {PDF_RENDER_SCALE:g}x render): {px_w} x {px_h}")
            finally:
                doc.close()
            return 0

        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            print(f"Image: {path}")
            print(f"Size: {img.width} x {img.height}")
        return 0
    except Exception as e:
        return _cli_err(str(e))


def _cli_render_pdf(path: str, out_dir: str, scale: float, prefix: str) -> int:
    """Render all PDF pages to PNG files."""
    if fitz is None:
        print(
            "Error: PyMuPDF is required for PDF rendering. Install with: pip install PyMuPDF",
            file=sys.stderr,
        )
        return 1

    if scale <= 0:
        print("Error: scale must be greater than zero.", file=sys.stderr)
        return 1

    if os.path.splitext(path)[1].lower() != ".pdf":
        return _cli_err(f"render expects a PDF document, got: {path}")

    output_path = Path(out_dir)
    try:
        output_path.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(path)
    except Exception as e:
        return _cli_err(str(e))

    try:
        if doc.page_count < 1:
            return _cli_err(f"PDF has no pages: {path}")

        for i in range(doc.page_count):
            page = doc.load_page(i)
            # Clamp per page so a huge --scale can't allocate a giant bitmap.
            s = scale
            longest = max(page.rect.width, page.rect.height) * s
            if longest > MAX_PDF_RENDER_PX and longest > 0:
                s *= MAX_PDF_RENDER_PX / longest
            pix = page.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            out_path = output_path / f"{prefix}-{i + 1:03d}.png"
            image.save(str(out_path), format="PNG")
            print(str(out_path))
    except Exception as e:
        return _cli_err(str(e))
    finally:
        doc.close()
    return 0


def _normalize_json_payload(payload: Dict, image_width: Optional[float], image_height: Optional[float]) -> Tuple[Dict, int]:
    """Apply current column-normalize behavior to rect shapes in a JSON payload."""
    shapes = payload.get("shapes")
    if not isinstance(shapes, list):
        raise ValueError("JSON does not contain a shape list.")

    image_meta = payload.get("image") if isinstance(payload.get("image"), dict) else {}
    w = image_meta.get("width")
    h = image_meta.get("height")

    if image_width is not None and image_height is not None:
        w = image_width
        h = image_height

    if not isinstance(w, (int, float)) or not isinstance(h, (int, float)) or w <= 0 or h <= 0:
        image_path = image_meta.get("path") if image_meta else None
        if isinstance(image_path, str) and image_path:
            img_w, img_h = _load_media_dimensions(image_path)
            w, h = img_w, img_h

    if not isinstance(w, (int, float)) or not isinstance(h, (int, float)) or w <= 0 or h <= 0:
        raise ValueError("Could not determine image dimensions for normalization.")

    # ONE column-normalize implementation: parse to Shapes, run the same pass the
    # GUI's 'N' runs, serialize back. Rect-only here (that is this command's
    # documented default; --all-kinds routes to _cli_normalize_project instead),
    # and clustered per page because the GUI only ever normalizes one page.
    parsed, skipped, coerced = _parse_shapes(shapes)
    if skipped:
        print(f"Warning: {skipped} malformed shape(s) were dropped while normalizing.",
              file=sys.stderr)
    if coerced:
        print(f"Warning: {coerced} shape(s) with an unknown kind were set to 'rect' "
              f"while normalizing.", file=sys.stderr)
    changed = 0
    for pg in sorted({s.page for s in parsed}):
        changed += normalize_rect_columns([s for s in parsed if s.page == pg], w, h)

    # Unknown top-level keys (notes, schema_version, ...) survive untouched and
    # shapes keep their input order -- but each shape is rewritten in areaDef's
    # canonical schema, so unknown PER-SHAPE keys (colour, source, ...) are not
    # preserved. That is the price of having one normalize implementation.
    out_payload = dict(payload)
    out_payload["shapes"] = [s.as_export_dict() for s in parsed]
    return out_payload, changed


def _cli_normalize_json(input_json: str, output_json: Optional[str], image_path: Optional[str]) -> int:
    """Normalize rectangle zones in a JSON payload with the same geometry pass as GUI N."""
    if not os.path.exists(input_json):
        print(f"Error: file does not exist: {input_json}", file=sys.stderr)
        return 1

    try:
        with open(input_json, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        print(f"Error: could not read JSON: {e}", file=sys.stderr)
        return 1

    image_width = image_height = None
    if image_path:
        try:
            image_width, image_height = _load_media_dimensions(image_path)
        except Exception as e:
            print(f"Warning: could not load image size override: {e}", file=sys.stderr)

    try:
        normalized, changed = _normalize_json_payload(payload, image_width, image_height)
    except Exception as e:
        print(f"Error: normalize failed: {e}", file=sys.stderr)
        return 1

    json_text = json.dumps(normalized, indent=2, allow_nan=False)
    if output_json:
        try:
            _atomic_write_text(output_json, json_text)
            print(f"Normalized {changed} rectangle zone(s). Saved to {output_json}")
            return 0
        except Exception as e:
            print(f"Error: could not write output JSON: {e}", file=sys.stderr)
            return 1

    print(json_text)
    return 0


# ============================================================================
# Headless project engine + CLI (every GUI op, no display required)
# ============================================================================


def render_media_page(path: str, page: int = 0, scale: float = PDF_RENDER_SCALE) -> "Image.Image":
    """Render a media page to a PIL image in the SAME pixel space the GUI uses
    (PDFs at PDF_RENDER_SCALE, clamped to MAX_PDF_RENDER_PX). A raster image is a
    single page, so any `page` other than 0 is rejected (consistent with the PDF
    out-of-range error), instead of being silently ignored."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        if fitz is None:
            raise RuntimeError("PyMuPDF is required for PDF support: pip install PyMuPDF")
        doc = fitz.open(path)
        try:
            if page < 0 or page >= doc.page_count:
                raise ValueError(f"page {page} is out of range (0..{doc.page_count - 1})")
            pg = doc.load_page(page)
            rect = pg.rect
            s = scale
            longest = max(rect.width, rect.height) * s
            if longest > MAX_PDF_RENDER_PX and longest > 0:
                s *= MAX_PDF_RENDER_PX / longest
            pix = pg.get_pixmap(matrix=fitz.Matrix(s, s), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
    if page != 0:
        raise ValueError(f"page {page} is out of range (0..0) for a single-page image")
    with Image.open(path) as img:
        return img.convert("RGB")


def _pil_to_qpixmap(im: "Image.Image"):
    """Convert a PIL image to a QPixmap (RGBA, detached from the source buffer)."""
    rgba = im.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimg = QImage(data, rgba.width, rgba.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


def _csv_safe(value: str) -> str:
    """Neutralize a spreadsheet formula cell.

    Excel/Sheets execute a cell that starts with = + - or @, so a shape named
    "=cmd|..." would run on open. A leading apostrophe makes the cell literal
    text; every other name is written through untouched."""
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def _shapes_to_csv(shapes: List[Shape]) -> str:
    """Serialize shapes to the same CSV the GUI exports."""
    import io
    rows = []
    for s in shapes:
        x1, y1, x2, y2 = s.bbox()
        row = {
            "id": str(s.sid), "name": _csv_safe(s.name), "kind": s.kind, "page": str(s.page),
            "x1": f"{x1:.3f}", "y1": f"{y1:.3f}", "x2": f"{x2:.3f}", "y2": f"{y2:.3f}",
            "width": f"{(x2 - x1):.3f}", "height": f"{(y2 - y1):.3f}",
            "cx": "", "cy": "", "r": "",
            "chamfer": s.as_export_dict().get("chamfer", "") if s.kind == "chamfer" else "",
            # Exact geometry of an irregular area, as JSON, so a consumer that
            # only reads the bbox columns keeps seeing the envelope it always saw.
            "rects": json.dumps(s.as_export_dict()["rects"]) if s.kind == "multirect" and s.rects else "",
        }
        if s.kind == "circle":
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            r = (x2 - x1) / 2.0
            row["cx"], row["cy"], row["r"] = f"{cx:.3f}", f"{cy:.3f}", f"{r:.3f}"
        rows.append(row)
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["id", "name", "kind", "page", "x1", "y1", "x2", "y2",
                    "width", "height", "cx", "cy", "r", "chamfer", "rects"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


class Project:
    """A headless annotation project (image dimensions + shapes), with no Qt
    dependency. Backs the CLI so every GUI capability is scriptable."""

    def __init__(self, image_path: Optional[str] = None, width: int = 0,
                 height: int = 0, shapes: Optional[List[Shape]] = None) -> None:
        self.image_path = image_path
        # Negative/invalid dims collapse to 0 (the degenerate sentinel downstream
        # code already treats as "unknown"), never a negative size.
        self.width = max(0, int(width or 0))
        self.height = max(0, int(height or 0))
        self.shapes: List[Shape] = shapes if shapes is not None else []
        self.skipped_shapes = 0
        self.coerced_shapes = 0

    def next_sid(self) -> int:
        return max((s.sid for s in self.shapes), default=0) + 1

    @classmethod
    def from_json(cls, path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Project JSON root must be an object.")
        img = data.get("image") if isinstance(data.get("image"), dict) else {}
        shapes, skipped, coerced = _parse_shapes(data.get("shapes") or [])
        proj = cls(img.get("path"), img.get("width", 0), img.get("height", 0), shapes)
        proj.skipped_shapes = skipped
        proj.coerced_shapes = coerced
        # Surface silent data loss so a load+rewrite command doesn't drop shapes
        # (and reuse their ids) without telling the user.
        if skipped:
            print(f"Warning: {skipped} malformed shape(s) were dropped while loading "
                  f"{path}.", file=sys.stderr)
        if coerced:
            print(f"Warning: {coerced} shape(s) with an unknown kind were set to 'rect' "
                  f"while loading {path}.", file=sys.stderr)
        return proj

    @classmethod
    def from_media(cls, source: str, page: int = 0) -> "Project":
        img = render_media_page(source, page)
        return cls(os.path.abspath(source), img.width, img.height, [])

    def to_payload(self) -> Dict:
        ordered = sorted(self.shapes, key=lambda s: (s.page, s.sid))
        return {
            "schema_version": 1,
            "image": {"path": self.image_path, "width": self.width, "height": self.height},
            "shapes": [s.as_export_dict() for s in ordered],
        }

    def write(self, path: Optional[str]) -> Optional[str]:
        """Write the project JSON. Returns the actual path written (with the
        .json suffix applied) so callers report the real file, or None for stdout."""
        text = json.dumps(self.to_payload(), indent=2, allow_nan=False)
        if path:
            final = _ensure_suffix(path, ".json")
            _atomic_write_text(final, text)
            return final
        print(text)
        return None

    def get(self, sid: int, page: Optional[int] = None) -> Optional[Shape]:
        # sids repeat across PDF pages, so a shape's identity is (page, sid).
        # page=None matches the first shape with that sid (single-page projects).
        return next((s for s in self.shapes
                     if s.sid == sid and (page is None or s.page == page)), None)

    def pages_for_sid(self, sid: int) -> List[int]:
        return sorted({s.page for s in self.shapes if s.sid == sid})

    def add_shape(self, kind: str, name: Optional[str], bbox, page: int = 0,
                  chamfer: float = 0.0) -> Shape:
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
        if kind == "multirect":
            # A multirect is defined by its pieces, which a bbox cannot express;
            # it only ever comes from merging existing shapes.
            raise ValueError("a multirect is created by merging shapes, not by add-shape")
        if int(page) < 0:
            raise ValueError(f"page must be >= 0, got {page}")
        sid = self.next_sid()
        x1, y1, x2, y2 = (float(v) for v in bbox)
        ch = float(chamfer)
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2, ch)):
            raise ValueError(
                f"shape coordinates must be finite, got bbox={(x1, y1, x2, y2)} chamfer={ch}")
        shp = Shape(sid, kind, name or f"area_{sid}", x1, y1, x2, y2,
                    chamfer=ch, page=int(page))
        self.shapes.append(shp)
        return shp

    def covers(self, bbox, page: int = 0) -> bool:
        """Whether a shape on `page` already contains bbox's centre.

        The idempotence rule every detector merge uses: re-running a detection
        must not stack a second field on one that is already there. Only the
        SAME page counts — a detection on page N must not be suppressed by a
        shape on a different page."""
        x1, y1, x2, y2 = bbox
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        return any(s.bbox()[0] <= cx <= s.bbox()[2] and s.bbox()[1] <= cy <= s.bbox()[3]
                   for s in self.shapes if s.page == page)

    def add_boxes(self, boxes, prefix: str, page: int = 0, dedup: bool = True) -> int:
        added = 0
        for (x1, y1, x2, y2) in boxes:
            if dedup and self.covers((x1, y1, x2, y2), page):
                continue
            sid = self.next_sid()
            self.add_shape("rect", f"{prefix}_{sid}", (x1, y1, x2, y2), page=page)
            added += 1
        return added

    def delete(self, sid: int, page: Optional[int] = None) -> bool:
        # Remove exactly the one matched shape (page-aware), not every shape that
        # happens to share the sid on another page.
        for i, s in enumerate(self.shapes):
            if s.sid == sid and (page is None or s.page == page):
                del self.shapes[i]
                return True
        return False

    def rename(self, sid: int, name: str, page: Optional[int] = None) -> bool:
        s = self.get(sid, page)
        if s:
            s.name = name
            return True
        return False

    def move(self, sid: int, dx: float, dy: float, page: Optional[int] = None) -> bool:
        s = self.get(sid, page)
        if s:
            s.translate(dx, dy)   # multirects move every piece, not just the envelope
            return True
        return False

    def set_bbox(self, sid: int, bbox, page: Optional[int] = None) -> bool:
        s = self.get(sid, page)
        if s:
            if s.kind == "multirect":
                # Writing an envelope would say nothing about where the pieces go.
                raise ValueError(f"shape {sid} is a multirect: split it to edit its geometry")
            s.set_bbox(*(float(v) for v in bbox))
            return True
        return False

    def merge(self, ids: List[int], page: int = 0) -> int:
        """Replace the listed shapes with one multirect. Returns its new sid."""
        picked: List[Shape] = []
        for sid in dict.fromkeys(ids):     # de-duplicated, order preserved
            s = self.get(sid, page)
            if s is None:
                raise ValueError(f"no shape with id {sid} on page {page}")
            picked.append(s)
        merged = merge_shapes(picked, self.next_sid())
        self.shapes = splice_shapes(self.shapes, picked, [merged])
        return merged.sid

    def split(self, sid: int, page: int = 0) -> List[int]:
        """Replace a multirect with one plain rect per piece. Returns the new sids."""
        s = self.get(sid, page)
        if s is None:
            raise ValueError(f"no shape with id {sid} on page {page}")
        parts = split_shape(s, self.next_sid())
        self.shapes = splice_shapes(self.shapes, [s], parts)
        return [p.sid for p in parts]

    def normalize(self) -> Dict[str, int]:
        # The GUI only ever normalizes one page's shapes, so cluster per page.
        counts = {"rect": 0, "circle": 0, "chamfer": 0}
        for pg in sorted({s.page for s in self.shapes}):
            c = normalize_shapes([s for s in self.shapes if s.page == pg],
                                 self.width, self.height)
            for k in counts:
                counts[k] += c[k]
        return counts

    def snap_to_lines(self, h_lines, v_lines, tol: float, text_boxes=None,
                      page: Optional[int] = None) -> int:
        """Snap shapes onto detected lines/text edges.

        ``page`` is the page the lines were detected on: only that page's
        shapes move, because a multi-page project shares one coordinate space
        and page N's rules say nothing about page M. ``None`` (the default)
        snaps every shape, which is what single-page projects want.
        """
        text_boxes = text_boxes or []
        n = 0
        for s in self.shapes:
            if page is not None and s.page != page:
                continue
            bbox, snapped = snap_bbox_to_lines_and_text(
                *s.bbox(), h_lines, v_lines, text_boxes, tol, keep_size=False
            )
            if snapped and apply_snapped_bbox(s, bbox):
                n += 1
        return n


def _cli_err(msg: str) -> int:
    print(f"Error: {msg}", file=sys.stderr)
    return 1


def _cli_new_project(source: str, page: int, output: Optional[str]) -> int:
    if not os.path.exists(source):
        return _cli_err(f"file does not exist: {source}")
    try:
        proj = Project.from_media(source, page)
    except Exception as e:
        return _cli_err(str(e))
    proj.write(output)
    if output:
        print(f"Created project ({proj.width}x{proj.height}) -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_detect_lines(source: str, page: int, output: Optional[str]) -> int:
    if not os.path.exists(source):
        return _cli_err(f"file does not exist: {source}")
    try:
        img = render_media_page(source, page)
    except Exception as e:
        return _cli_err(str(e))
    h_lines, v_lines = detect_document_lines(img)
    payload = {
        "image": {"path": os.path.abspath(source), "width": img.width, "height": img.height, "page": page},
        "lines": {
            "horizontal": [{"y": y, "x_start": x0, "x_end": x1} for (y, x0, x1, *_) in h_lines],
            "vertical": [{"x": x, "y_start": y0, "y_end": y1} for (x, y0, y1, *_) in v_lines],
        },
    }
    text = json.dumps(payload, indent=2, allow_nan=False)
    if output:
        _atomic_write_text(_ensure_suffix(output, ".json"), text)
        print(f"Detected {len(h_lines)} horizontal and {len(v_lines)} vertical line(s) -> {_ensure_suffix(output, '.json')}")
    else:
        print(text)
    return 0


def _load_or_new_project(into: Optional[str], source: str, img) -> Tuple[Optional["Project"], int]:
    """Resolve a detector's `--into` argument: open that project (warning when
    its page size disagrees with what was just rendered) or start a fresh one
    around `source`. Returns (project, exit_code); the project is None exactly
    when the exit code is non-zero."""
    if not into:
        return Project(os.path.abspath(source), img.width, img.height, []), 0
    if not os.path.exists(into):
        return None, _cli_err(f"project to merge into does not exist: {into}")
    try:
        proj = Project.from_json(into)
    except Exception as e:
        return None, _cli_err(str(e))
    if proj.width and (img.width != proj.width or img.height != proj.height):
        print(f"Warning: detection source ({img.width}x{img.height}) differs from the "
              f"merge target ({proj.width}x{proj.height}); coordinates may not line up.",
              file=sys.stderr)
    return proj, 0


def _cli_detect_boxes(source: str, page: int, output: Optional[str],
                      into: Optional[str], kind: str) -> int:
    """kind: 'checkbox' or 'text'. Detects boxes and writes a project JSON,
    optionally merging into an existing project (--into)."""
    if not os.path.exists(source):
        return _cli_err(f"file does not exist: {source}")
    try:
        img = render_media_page(source, page)
    except Exception as e:
        return _cli_err(str(e))
    boxes = detect_checkbox_squares(img) if kind == "checkbox" else detect_text_boxes(img)
    is_pdf = os.path.splitext(source)[1].lower() == ".pdf"
    shape_page = page if is_pdf else 0
    proj, code = _load_or_new_project(into, source, img)
    if proj is None:
        return code
    prefix = "checkbox" if kind == "checkbox" else "text"
    added = proj.add_boxes(boxes, prefix, page=shape_page, dedup=True)
    proj.write(output)
    if output:
        print(f"Detected {len(boxes)} {prefix} box(es), added {added} new -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_detect_codes(source: str, page: int, output: Optional[str],
                      into: Optional[str], kind: str) -> int:
    """kind: 'markers' (QR/AprilTag) or 'barcodes'. Detects codes and writes a
    project JSON, optionally merging into an existing project (--into). Fields
    are named by decoded payload."""
    if not os.path.exists(source):
        return _cli_err(f"file does not exist: {source}")
    try:
        img = render_media_page(source, page)
    except Exception as e:
        return _cli_err(str(e))
    dets = detect_markers(img) if kind == "markers" else detect_barcodes(img)
    is_pdf = os.path.splitext(source)[1].lower() == ".pdf"
    shape_page = page if is_pdf else 0
    proj, code = _load_or_new_project(into, source, img)
    if proj is None:
        return code
    noun = "marker" if kind == "markers" else "barcode"
    added = 0
    for d in dets:
        x1, y1, x2, y2 = d["bbox"]
        if proj.covers(d["bbox"], shape_page):
            continue
        payload = _clean_name((d.get("payload") or "").replace("\n", " ").strip())[:40]
        k = d.get("kind") or noun
        name = f"{k}_{payload}" if payload else f"{k}_{proj.next_sid()}"
        proj.add_shape("rect", name, (x1, y1, x2, y2), page=shape_page)
        added += 1
    proj.write(output)
    if output:
        print(f"Detected {len(dets)} {noun}(s), added {added} new -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_screenshot(project_json: str, output: Optional[str], theme: str,
                    page: int, window: bool) -> int:
    """Render a project's annotated page (source image + shapes) to a PNG,
    exactly as the GUI canvas draws it. Runs headless (offscreen Qt), so an
    agent can produce and view the same picture the user sees."""
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    try:
        proj = Project.from_json(project_json)
    except Exception as e:
        return _cli_err(str(e))
    src = proj.image_path
    # A single-image (or source-less) project has exactly one page; reject any
    # page != 0 to match the out-of-range error the PDF path already gives.
    is_pdf = bool(src) and os.path.splitext(src)[1].lower() == ".pdf"
    if not is_pdf and page != 0:
        return _cli_err(f"page {page} is out of range (0..0) for a single-page image")
    try:
        if src and os.path.exists(src):
            img = render_media_page(src, page)
        else:
            # No source image: synthesize a blank page from the declared dims,
            # clamped so an absurd width/height can't allocate gigabytes.
            w = max(1, int(proj.width or 1))
            h = max(1, int(proj.height or 1))
            longest = max(w, h)
            if longest > MAX_PDF_RENDER_PX:
                s = MAX_PDF_RENDER_PX / longest
                w = max(1, int(w * s))
                h = max(1, int(h * s))
            img = Image.new("RGB", (w, h), "white")
    except Exception as e:
        return _cli_err(str(e))

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    win = AnnotatorWindow(theme=theme)
    win.canvas.set_image(_pil_to_qpixmap(img))
    win.shapes = [s.clone() for s in proj.shapes if s.page == page]
    win.next_sid = max((s.sid for s in win.shapes), default=0) + 1
    win.update_list()
    win.set_selected(None)
    win.redraw_shapes()
    out = _ensure_suffix(output or "screenshot.png", ".png")
    if window:
        win.resize(1400, 900)
        win.show()
        app.processEvents()
    if not win.save_screenshot(out, window=window):
        return _cli_err("failed to render screenshot")
    # Report the ACTUAL saved dimensions (window grab is the window size, not the
    # page size).
    try:
        with Image.open(out) as saved:
            sw, sh = saved.size
    except Exception:
        sw, sh = img.width, img.height
    print(f"Wrote screenshot ({sw}x{sh}) -> {out}")
    return 0


def _cli_snap_to_lines(project_json: str, source: str, page: int, tol: float,
                       output: Optional[str], high_contrast: bool = False) -> int:
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    if not os.path.exists(source):
        return _cli_err(f"source image/PDF does not exist: {source}")
    try:
        proj = Project.from_json(project_json)
        img = render_media_page(source, page)
    except Exception as e:
        return _cli_err(str(e))
    if proj.width and (proj.width != img.width or proj.height != img.height):
        print(
            f"Warning: project dims ({proj.width}x{proj.height}) differ from source "
            f"render ({img.width}x{img.height}); snapping may be off.",
            file=sys.stderr,
        )
    h_lines, v_lines = detect_document_lines(img)
    text_boxes = detect_text_boxes(img)
    hc = 0
    if high_contrast:
        regions = detect_high_contrast_regions(img)
        hc = len(regions)
        hc_h, hc_v = _text_boxes_to_lines(regions)
        h_lines = list(h_lines) + hc_h
        v_lines = list(v_lines) + hc_v
    # Lines come from ONE page, so for a PDF only that page's shapes may move.
    # A raster source has no page dimension: keep snapping every shape.
    snap_page = page if os.path.splitext(source)[1].lower() == ".pdf" else None
    n = proj.snap_to_lines(h_lines, v_lines, tol, text_boxes=text_boxes, page=snap_page)
    proj.write(output)
    if output:
        extra = f", {hc} high-contrast region(s)" if high_contrast else ""
        print(
            f"Snapped {n} shape(s) to {len(h_lines) + len(v_lines)} line(s), "
            f"{len(text_boxes)} text region(s){extra} -> {_ensure_suffix(output, '.json')}"
        )
    return 0


def _cli_align(project_json: str, op: str, key_sid: int,
               ids: Optional[List[int]], output: Optional[str],
               page: Optional[int] = None) -> int:
    """Align/resize shapes to a key shape (the GUI's Adobe-style align). Operates
    within a single page (the key's), matching the GUI."""
    if op not in ALIGN_OPS:
        return _cli_err(f"op must be one of {ALIGN_OPS}")
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    try:
        proj = Project.from_json(project_json)
    except Exception as e:
        return _cli_err(str(e))
    candidates = proj.pages_for_sid(key_sid)
    if not candidates:
        return _cli_err(f"no shape with id {key_sid} (the --key anchor)")
    if page is None and len(candidates) > 1:
        return _cli_err(f"--key {key_sid} exists on pages {candidates}; specify --page")
    key_page = page if page is not None else candidates[0]
    key = proj.get(key_sid, key_page)
    if key is None:
        return _cli_err(f"no shape with id {key_sid} on page {key_page}")
    if ids:
        targets = [proj.get(i, key_page) for i in ids]
        missing = [i for i, t in zip(ids, targets) if t is None]
        if missing:
            return _cli_err(f"no shape with id(s) {missing} on page {key_page}")
    else:
        # Default to the key's page only (the GUI never aligns across pages).
        targets = [s for s in proj.shapes if s.page == key_page]
    if key not in targets:
        targets.append(key)
    if len([t for t in targets if t is not key]) < 1:
        return _cli_err("need at least one shape besides the key to align")
    # Clamp to the real image bounds (the GUI clamps to the canvas image size).
    # Fall back to the source media dimensions if the project's are degenerate.
    cw, ch = proj.width, proj.height
    if (not cw or not ch or cw <= 0 or ch <= 0) and proj.image_path and os.path.exists(proj.image_path):
        try:
            cw, ch = _load_media_dimensions(proj.image_path)
        except Exception:
            pass
    skipped = align_shapes(targets, key, op)
    for s in targets:
        if s is key:
            continue
        # min_size=0: align copies the key's geometry verbatim, so a small key
        # must produce small targets rather than silently skipped ones.
        # A multirect is refused (its envelope is derived, not writable), so it
        # is clamped by translating the whole unit instead.
        if not apply_snapped_bbox(s, s.bbox(), cw, ch, min_size=0.0):
            s.clamp_into(cw, ch)
    proj.write(output)
    if output:
        note = f", skipped {skipped} multirect(s): split to resize" if skipped else ""
        print(f"Aligned {len(targets) - 1 - skipped} shape(s) to key {key_sid} "
              f"({op}){note} -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_detect_high_contrast(source: str, page: int, output: Optional[str]) -> int:
    """Detect solid high-contrast regions (bars / inverted headers) and output
    their coordinates as JSON (their edges are snap targets, like detect-lines)."""
    if not os.path.exists(source):
        return _cli_err(f"file does not exist: {source}")
    try:
        img = render_media_page(source, page)
    except Exception as e:
        return _cli_err(str(e))
    regions = detect_high_contrast_regions(img)
    payload = {
        "image": {"path": os.path.abspath(source), "width": img.width, "height": img.height, "page": page},
        "regions": [{"x1": x1, "y1": y1, "x2": x2, "y2": y2} for (x1, y1, x2, y2) in regions],
    }
    text = json.dumps(payload, indent=2, allow_nan=False)
    if output:
        _atomic_write_text(_ensure_suffix(output, ".json"), text)
        print(f"Detected {len(regions)} high-contrast region(s) -> {_ensure_suffix(output, '.json')}")
    else:
        print(text)
    return 0


def _cli_normalize_project(project_json: str, output: Optional[str]) -> int:
    """Normalize ALL shape kinds (rect/circle/chamfer) like the GUI 'N'."""
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    try:
        proj = Project.from_json(project_json)
    except Exception as e:
        return _cli_err(str(e))
    counts = proj.normalize()
    proj.write(output)
    if output:
        print(f"Normalized {counts['rect']} rect(s), {counts['circle']} circle(s), "
              f"{counts['chamfer']} chamfer(s) -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_add_shape(project_json: str, kind: str, name: Optional[str], bbox,
                   page: int, chamfer: float, output: Optional[str]) -> int:
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    try:
        proj = Project.from_json(project_json)
        shp = proj.add_shape(kind, name, bbox, page=page, chamfer=chamfer)
    except Exception as e:
        return _cli_err(str(e))
    proj.write(output)
    if output:
        print(f"Added {shp.kind} #{shp.sid} '{shp.name}' -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_list_shapes(project_json: str) -> int:
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    try:
        proj = Project.from_json(project_json)
    except Exception as e:
        return _cli_err(str(e))
    print(f"{proj.image_path}  ({proj.width}x{proj.height})  {len(proj.shapes)} shape(s)")
    print(f"{'id':>4}  {'kind':<9} {'page':>4}  {'bbox':<32} name")
    for s in sorted(proj.shapes, key=lambda s: (s.page, s.sid)):
        x1, y1, x2, y2 = s.bbox()
        bbox = f"[{x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}]"
        print(f"{s.sid:>4}  {s.kind:<9} {s.page:>4}  {bbox:<32} {s.name}")
    return 0


def _cli_edit_shape(project_json: str, sid: Optional[int], name: Optional[str],
                    delete: bool, duplicate: bool, move: Optional[Tuple[float, float]],
                    bbox: Optional[List[float]], output: Optional[str],
                    page: Optional[int] = None, merge: Optional[str] = None,
                    split: bool = False) -> int:
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    # Merge/split replace whole shapes, so pairing them with a field edit (or with
    # each other) would silently drop one of the two requests.
    others = [name is not None, move is not None, bbox is not None, delete, duplicate]
    if (merge is not None or split) and (any(others) or (merge is not None and split)):
        return _cli_err("--merge/--split cannot be combined with each other or with "
                        "--name/--move/--bbox/--delete/--duplicate")
    if merge is None and sid is None:
        return _cli_err("--id is required (or use --merge to combine shapes)")
    try:
        proj = Project.from_json(project_json)
    except Exception as e:
        return _cli_err(str(e))
    if merge is not None:
        try:
            ids = [int(tok) for tok in merge.split(",") if tok.strip()]
        except ValueError:
            return _cli_err(f"--merge wants a comma-separated id list, got {merge!r}")
        try:
            new_sid = proj.merge(ids, page=page or 0)
        except Exception as e:
            return _cli_err(str(e))
        proj.write(output)
        if output:
            print(f"Merged {len(ids)} shape(s) into id {new_sid} "
                  f"('{proj.get(new_sid, page or 0).name}') -> {_ensure_suffix(output, '.json')}")
        return 0
    # sids repeat across pages; resolve the target shape's page (error if ambiguous).
    candidates = proj.pages_for_sid(sid)
    if not candidates:
        return _cli_err(f"no shape with id {sid}")
    if page is None and len(candidates) > 1:
        return _cli_err(f"shape id {sid} exists on pages {candidates}; specify --page")
    pg = page if page is not None else candidates[0]
    src = proj.get(sid, pg)
    if src is None:
        return _cli_err(f"no shape with id {sid} on page {pg}")
    if split:
        try:
            new_sids = proj.split(sid, page=pg)
        except Exception as e:
            return _cli_err(str(e))
        proj.write(output)
        if output:
            print(f"Split id {sid} into {len(new_sids)} rect(s) "
                  f"-> {_ensure_suffix(output, '.json')}")
        return 0
    # --delete/--duplicate take the whole shape; combining them with field edits
    # would silently ignore the edits, so reject the conflicting combination.
    if (delete or duplicate) and (name is not None or move is not None or bbox is not None):
        return _cli_err("--delete/--duplicate cannot be combined with --name/--move/--bbox")
    actions = []
    try:
        if delete:
            proj.delete(sid, pg)
            actions.append("deleted")
        elif duplicate:
            # Clone rather than re-add: a copy has to carry whatever defines the
            # original (chamfer, and a multirect's pieces), not just its bbox.
            new = src.clone()
            new.sid, new.name = proj.next_sid(), f"{src.name}_copy"
            new.translate(20, 20)
            proj.shapes.append(new)
            actions.append(f"duplicated to #{new.sid}")
        else:
            if name is not None:
                proj.rename(sid, name, pg)
                actions.append(f"renamed to {name!r}")
            if move is not None:
                proj.move(sid, move[0], move[1], pg)
                actions.append(f"moved by ({move[0]},{move[1]})")
            if bbox is not None:
                proj.set_bbox(sid, bbox, pg)
                actions.append("bbox set")
    except Exception as e:
        return _cli_err(str(e))
    if not actions:
        return _cli_err("no edit specified (use --name/--move/--bbox/--delete/--duplicate)")
    proj.write(output)
    if output:
        print(f"Shape {sid} (page {pg}): {', '.join(actions)} -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_clear_shapes(project_json: str, page: Optional[int], output: Optional[str]) -> int:
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    try:
        proj = Project.from_json(project_json)
    except Exception as e:
        return _cli_err(str(e))
    before = len(proj.shapes)
    if page is None:
        proj.shapes = []
    else:
        proj.shapes = [s for s in proj.shapes if s.page != page]
    proj.write(output)
    if output:
        print(f"Cleared {before - len(proj.shapes)} shape(s) -> {_ensure_suffix(output, '.json')}")
    return 0


def _cli_export_csv(project_json: str, output: Optional[str]) -> int:
    if not os.path.exists(project_json):
        return _cli_err(f"file does not exist: {project_json}")
    try:
        proj = Project.from_json(project_json)
    except Exception as e:
        return _cli_err(str(e))
    text = _shapes_to_csv(sorted(proj.shapes, key=lambda s: (s.page, s.sid)))
    if output:
        _atomic_write_text(_ensure_suffix(output, ".csv"), text)
        print(f"Exported {len(proj.shapes)} shape(s) -> {_ensure_suffix(output, '.csv')}")
    else:
        sys.stdout.write(text)
    return 0


# ============================================================================
# Main
# ============================================================================


def _gui_excepthook(exc_type, exc, tb) -> None:
    """Report an unhandled GUI exception instead of killing the process.

    While ``sys.excepthook`` is the interpreter default, PyQt6 answers an
    exception escaping a slot with ``qFatal()`` -> ``abort()``, which takes
    every unsaved annotation with it. Installing any replacement suppresses
    that; this one tells the user and leaves the window open so the work can
    still be saved.
    """
    traceback.print_exception(exc_type, exc, tb)
    try:
        QMessageBox.critical(
            None, "Unexpected Error",
            f"An unexpected error occurred:\n\n{exc_type.__name__}: {exc}\n\n"
            "The window is still open - save your work to a new file."
        )
    except Exception:  # noqa: BLE001 - never fail inside the last-resort hook
        pass


def _install_gui_excepthook() -> None:
    """GUI entry path only: CLI subcommands keep the plain "Error: ..." handler
    at the bottom of this file (and their own exit codes)."""
    sys.excepthook = _gui_excepthook


def _cli_gui(args) -> int:
    """The `gui` subcommand — and what running with no subcommand does."""
    _install_gui_excepthook()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = AnnotatorWindow(image_path=args.image, theme=args.theme)
    # Interactive session: detection runs off the UI thread (see _run_detection).
    window.async_detection = True
    window.show()
    if args.load_json:
        window.load_json(args.load_json)
    return app.exec()


def _cli_normalize(args) -> int:
    if args.all_kinds:
        return _cli_normalize_project(args.source, output=args.output)
    return _cli_normalize_json(args.source, output_json=args.output,
                               image_path=args.image_size_source or args.image)


def main() -> None:
    parser = argparse.ArgumentParser(description="Annotate an image with named shapes (PyQt6 version).")
    parser.add_argument("--version", action="version",
                        version=f"template-annotator {_package_version()}")
    parser.add_argument("--image", type=str, default=None, help="Path to image or PDF")
    parser.add_argument("--load-json", dest="load_json", type=str, default=None,
                        help="Open the GUI with this project JSON loaded (loads its referenced image/PDF)")
    parser.add_argument(
        "--theme",
        type=str,
        default="auto",
        choices=("auto", "light", "dark"),
        help="UI theme: auto (system), light, or dark (default: auto)",
    )

    # Every subparser carries the function that runs it (args -> exit code), so
    # dispatch is one call and a new subcommand cannot forget to be wired up.
    # No subcommand at all launches the GUI, same as `gui`.
    parser.set_defaults(func=_cli_gui)
    subparsers = parser.add_subparsers(dest="command")

    gui_parser = subparsers.add_parser("gui", help="Launch the Qt GUI")
    gui_parser.set_defaults(command="gui", func=_cli_gui)

    info_parser = subparsers.add_parser("info", help="Print metadata for an image or PDF")
    info_parser.add_argument("source", help="Path to image or PDF")
    info_parser.set_defaults(func=lambda a: _cli_print_info(a.source))

    render_parser = subparsers.add_parser("render", help="Render a PDF into PNG pages")
    render_parser.add_argument("source", help="Path to PDF")
    render_parser.add_argument(
        "--out-dir",
        "--outdir",
        default="./pdf_previews",
        help="Directory where PNG pages are written",
    )
    render_parser.add_argument(
        "--scale",
        type=float,
        default=PDF_RENDER_SCALE,
        help=f"Zoom scale to apply while rendering (default: {PDF_RENDER_SCALE:g})",
    )
    render_parser.add_argument(
        "--prefix",
        default="page",
        help="Filename prefix for PNG output",
    )
    render_parser.set_defaults(
        func=lambda a: _cli_render_pdf(a.source, a.out_dir, a.scale, a.prefix))

    normalize_parser = subparsers.add_parser(
        "normalize-json",
        help="Normalize rectangle shapes in a JSON payload (same geometry logic as GUI N)",
        description="Normalize rectangle shapes in a JSON payload (same geometry "
                    "logic as GUI N). Shapes are rewritten in areaDef's canonical "
                    "schema: unknown per-shape keys are not preserved, and "
                    "malformed shapes are dropped with a warning.",
    )
    normalize_parser.add_argument("source", help="Path to JSON payload")
    normalize_parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output JSON path. If omitted, JSON is printed to stdout.",
    )
    normalize_parser.add_argument(
        "--image-size-source",
        default=None,
        dest="image_size_source",
        help="Optional image/PDF path to override width/height from JSON image metadata",
    )
    normalize_parser.add_argument(
        "--all-kinds",
        action="store_true",
        help="Normalize rectangles, circles, AND chamfers (like the GUI 'N'), "
             "operating on a project JSON. Default normalizes rectangles only.",
    )
    normalize_parser.set_defaults(func=_cli_normalize)

    def _add_io(p, src_help="Path to image or PDF"):
        p.add_argument("source", help=src_help)
        p.add_argument("--page", type=int, default=0, help="PDF page index (0-based)")
        p.add_argument("--output", "-o", default=None, help="Output path (stdout if omitted)")

    np_parser = subparsers.add_parser("new-project", help="Create an empty project JSON for an image/PDF page")
    _add_io(np_parser)
    np_parser.set_defaults(func=lambda a: _cli_new_project(a.source, a.page, a.output))

    dl_parser = subparsers.add_parser("detect-lines", help="Detect rules/underlines; output line coordinates")
    _add_io(dl_parser)
    dl_parser.set_defaults(func=lambda a: _cli_detect_lines(a.source, a.page, a.output))

    dc_parser = subparsers.add_parser("detect-checkboxes", help="Detect check-mark boxes; output a project of rect fields")
    _add_io(dc_parser)
    dc_parser.add_argument("--into", default=None, help="Merge detected boxes into this existing project JSON")
    dc_parser.set_defaults(
        func=lambda a: _cli_detect_boxes(a.source, a.page, a.output, a.into, "checkbox"))

    dt_parser = subparsers.add_parser("detect-text", help="Detect text-string boxes; output a project of rect fields")
    _add_io(dt_parser)
    dt_parser.add_argument("--into", default=None, help="Merge detected boxes into this existing project JSON")
    dt_parser.set_defaults(
        func=lambda a: _cli_detect_boxes(a.source, a.page, a.output, a.into, "text"))

    dm_parser = subparsers.add_parser("detect-markers", help="Detect QR codes / AprilTag markers; output a project of rect fields")
    _add_io(dm_parser)
    dm_parser.add_argument("--into", default=None, help="Merge detected markers into this existing project JSON")
    dm_parser.set_defaults(
        func=lambda a: _cli_detect_codes(a.source, a.page, a.output, a.into, "markers"))

    db_parser = subparsers.add_parser("detect-barcodes", help="Detect 1D barcodes; output a project of rect fields")
    _add_io(db_parser)
    db_parser.add_argument("--into", default=None, help="Merge detected barcodes into this existing project JSON")
    db_parser.set_defaults(
        func=lambda a: _cli_detect_codes(a.source, a.page, a.output, a.into, "barcodes"))

    dhc_parser = subparsers.add_parser("detect-high-contrast", help="Detect solid bars / inverted headers; output region coordinates")
    _add_io(dhc_parser)
    dhc_parser.set_defaults(func=lambda a: _cli_detect_high_contrast(a.source, a.page, a.output))

    snap_parser = subparsers.add_parser("snap-to-lines", help="Snap a project's shapes to detected document lines")
    snap_parser.add_argument("project", help="Path to project JSON")
    snap_parser.add_argument("--source", required=True, help="Image/PDF to detect lines from")
    snap_parser.add_argument("--page", type=int, default=0, help="PDF page index (0-based)")
    snap_parser.add_argument("--tol", type=float, default=LINE_SNAP_TOL_PX, help=f"Snap tolerance in px (default {LINE_SNAP_TOL_PX:g})")
    snap_parser.add_argument("--high-contrast", action="store_true", dest="high_contrast",
                             help="Also snap to high-contrast region (bar/header) edges")
    snap_parser.add_argument("--output", "-o", default=None, help="Output path (stdout if omitted)")
    snap_parser.set_defaults(
        func=lambda a: _cli_snap_to_lines(a.project, a.source, a.page, a.tol, a.output,
                                          high_contrast=a.high_contrast))

    align_parser = subparsers.add_parser("align", help="Align/resize shapes to a key shape (Adobe-style)")
    align_parser.add_argument("project", help="Path to project JSON")
    align_parser.add_argument("--op", required=True, choices=ALIGN_OPS, help="Alignment operation")
    align_parser.add_argument("--key", type=int, required=True, dest="key", help="Key/anchor shape id (never moves)")
    align_parser.add_argument("--ids", type=int, nargs="+", default=None, help="Shape ids to align (default: all on the key's page)")
    align_parser.add_argument("--page", type=int, default=None, help="Page to operate on (required if --key is ambiguous across pages)")
    align_parser.add_argument("--output", "-o", default=None, help="Output path (stdout if omitted)")
    align_parser.set_defaults(
        func=lambda a: _cli_align(a.project, a.op, a.key, a.ids, a.output, page=a.page))

    add_parser = subparsers.add_parser("add-shape", help="Add a shape to a project JSON")
    add_parser.add_argument("project", help="Path to project JSON")
    # multirect is deliberately absent: it is only ever produced by --merge.
    add_parser.add_argument("--kind", default="rect",
                            choices=[k for k in VALID_KINDS if k != "multirect"])
    add_parser.add_argument("--name", default=None)
    add_parser.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("X1", "Y1", "X2", "Y2"))
    add_parser.add_argument("--page", type=int, default=0)
    add_parser.add_argument("--chamfer", type=float, default=0.0)
    add_parser.add_argument("--output", "-o", default=None, help="Output path (stdout if omitted)")
    add_parser.set_defaults(
        func=lambda a: _cli_add_shape(a.project, a.kind, a.name, a.bbox, a.page,
                                      a.chamfer, a.output))

    list_parser = subparsers.add_parser("list-shapes", help="Print the shapes in a project JSON")
    list_parser.add_argument("project", help="Path to project JSON")
    list_parser.set_defaults(func=lambda a: _cli_list_shapes(a.project))

    edit_parser = subparsers.add_parser(
        "edit-shape", help="Rename/move/resize/delete/merge/split a shape by id")
    edit_parser.add_argument("project", help="Path to project JSON")
    edit_parser.add_argument("--id", type=int, default=None, dest="sid",
                             help="Id of the shape to edit (not needed with --merge)")
    edit_parser.add_argument("--name", default=None)
    edit_parser.add_argument("--move", type=float, nargs=2, default=None, metavar=("DX", "DY"))
    edit_parser.add_argument("--bbox", type=float, nargs=4, default=None, metavar=("X1", "Y1", "X2", "Y2"))
    edit_parser.add_argument("--delete", action="store_true")
    edit_parser.add_argument("--duplicate", action="store_true", help="Clone the shape (offset by 20px)")
    edit_parser.add_argument("--merge", default=None, metavar="ID1,ID2[,...]",
                             help="Merge 2+ rect/multirect shapes into one irregular area")
    edit_parser.add_argument("--split", action="store_true",
                             help="Split the --id multirect back into plain rects")
    edit_parser.add_argument("--page", type=int, default=None, help="Page of the shape (required if the id is ambiguous across pages)")
    edit_parser.add_argument("--output", "-o", default=None, help="Output path (stdout if omitted)")
    edit_parser.set_defaults(
        func=lambda a: _cli_edit_shape(a.project, a.sid, a.name, a.delete, a.duplicate,
                                       tuple(a.move) if a.move else None, a.bbox,
                                       a.output, page=a.page, merge=a.merge, split=a.split))

    clear_parser = subparsers.add_parser("clear-shapes", help="Remove all shapes (or all on one page) from a project")
    clear_parser.add_argument("project", help="Path to project JSON")
    clear_parser.add_argument("--page", type=int, default=None, help="Only clear this page (default: all)")
    clear_parser.add_argument("--output", "-o", default=None, help="Output path (stdout if omitted)")
    clear_parser.set_defaults(func=lambda a: _cli_clear_shapes(a.project, a.page, a.output))

    csv_parser = subparsers.add_parser("export-csv", help="Export a project JSON to CSV")
    csv_parser.add_argument("project", help="Path to project JSON")
    csv_parser.add_argument("--output", "-o", default=None, help="Output path (stdout if omitted)")
    csv_parser.set_defaults(func=lambda a: _cli_export_csv(a.project, a.output))

    ss_parser = subparsers.add_parser(
        "screenshot", help="Render a project's annotated page to PNG (as seen on the canvas)")
    ss_parser.add_argument("project", help="Path to project JSON")
    ss_parser.add_argument("--output", "-o", default=None, help="Output PNG (default screenshot.png)")
    ss_parser.add_argument("--theme", default="light", choices=("light", "dark"),
                           help="UI theme for shape colours (default: light)")
    ss_parser.add_argument("--page", type=int, default=0, help="PDF page index (0-based)")
    ss_parser.add_argument("--window", action="store_true",
                           help="Grab the whole window (panels + canvas) instead of just the page")
    ss_parser.set_defaults(
        func=lambda a: _cli_screenshot(a.project, a.output, a.theme, a.page, a.window))

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    # Top-level guard: a CLI command that hits an unwritable path, a corrupt
    # input, etc. should print a clean "Error: ..." and exit 1, never a raw
    # traceback. SystemExit (the normal per-command exit codes) passes through.
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI top-level safety net
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
