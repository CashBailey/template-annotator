"""Detector parity: the vectorized paths must equal the pure-Python fallback.

areaDef's four page detectors have three execution paths, picked at runtime by
what is installed:

    numpy + cv2   binarize + run-scan in numpy, labeling in cv2
    numpy only    binarize + run-scan + run-based labeling in numpy
    neither       the original per-pixel Python scanners

Output must be IDENTICAL across all three -- not merely equivalent. Downstream
grouping (text lines, satellite marks, checkbox order) breaks ties by encounter
order, and every detector returns a list, so a reordered component list is a
different annotation set.

Each path is forced by assigning ``areaDef._np`` / ``areaDef._cv2`` directly,
which the lazy loaders honour (see the module comment on ``_UNSET``).
"""

from __future__ import annotations

import pytest
from PIL import Image

import areaDef

DETECTORS = (
    areaDef.detect_document_lines,
    areaDef.detect_checkbox_squares,
    areaDef.detect_high_contrast_regions,
    areaDef.detect_text_boxes,
)

pytestmark = pytest.mark.skipif(
    areaDef._get_np() is None,
    reason="numpy not installed: only the pure-Python path exists here",
)


def _force(monkeypatch, *, np, cv2):
    monkeypatch.setattr(areaDef, "_np", np)
    monkeypatch.setattr(areaDef, "_cv2", cv2)


def _all_paths(monkeypatch, fn, image):
    """(fast, numpy_only, pure) results for one detector on one image."""
    real_np, real_cv2 = areaDef._get_np(), areaDef._get_cv2()
    _force(monkeypatch, np=real_np, cv2=real_cv2)
    fast = fn(image)
    _force(monkeypatch, np=real_np, cv2=None)
    numpy_only = fn(image)
    _force(monkeypatch, np=None, cv2=None)
    pure = fn(image)
    return fast, numpy_only, pure


def _assert_parity(monkeypatch, fn, image, label):
    fast, numpy_only, pure = _all_paths(monkeypatch, fn, image)
    assert fast == pure, f"{fn.__name__} on {label}: numpy+cv2 != pure Python"
    assert numpy_only == pure, f"{fn.__name__} on {label}: numpy-only != pure Python"


# ---------------------------------------------------------------------------
# Committed fixtures -- real page content
# ---------------------------------------------------------------------------

def _pdf_pages(path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(path)
    try:
        for index in range(doc.page_count):
            pm = doc.load_page(index).get_pixmap(
                matrix=fitz.Matrix(areaDef.PDF_RENDER_SCALE, areaDef.PDF_RENDER_SCALE))
            yield index, Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    finally:
        doc.close()


@pytest.mark.parametrize("name", ["checkboxes_600x400.png", "form_800x600.png"])
@pytest.mark.parametrize("fn", DETECTORS, ids=lambda f: f.__name__)
def test_parity_on_raster_fixtures(monkeypatch, fixtures_dir, fn, name):
    with Image.open(fixtures_dir / name) as image:
        image.load()
        _assert_parity(monkeypatch, fn, image, name)


@pytest.mark.parametrize("fn", DETECTORS, ids=lambda f: f.__name__)
def test_parity_on_pdf_pages(monkeypatch, fixtures_dir, fn):
    pages = list(_pdf_pages(fixtures_dir / "sample_form.pdf"))
    assert pages, "sample_form.pdf rendered no pages"
    for index, image in pages:
        _assert_parity(monkeypatch, fn, image, f"sample_form.pdf p{index}")


# ---------------------------------------------------------------------------
# Synthetic stress -- the fixtures are clean line art, so they exercise very
# few components and no ties. These do: dense speckle (thousands of tiny
# components, many sharing a first row) and diagonal 1-px contacts, where
# 8-connected labeling or a different label order would diverge.
# ---------------------------------------------------------------------------

def _noise(density: float, size=(97, 83), seed: int = 0) -> Image.Image:
    """Deterministic salt-and-pepper page (stdlib PRNG, no numpy)."""
    import random

    rnd = random.Random(seed)
    w, h = size
    px = bytes(0 if rnd.random() < density else 255 for _ in range(w * h))
    return Image.frombytes("L", (w, h), px).convert("RGB")


@pytest.mark.parametrize("density", [0.02, 0.15, 0.4])
@pytest.mark.parametrize("fn", DETECTORS, ids=lambda f: f.__name__)
def test_parity_on_dense_noise(monkeypatch, fn, density):
    image = _noise(density, seed=int(density * 100))
    _assert_parity(monkeypatch, fn, image, f"noise({density})")


DIAGONAL_PATTERNS = (
    ("checker", ["#.#", ".#.", "#.#"]),
    ("stair", ["##.", ".#.", ".##"]),
    ("kiss", ["#..#", ".##."]),
    ("solid", ["####", "####"]),
    ("blank", ["....", "...."]),
)


@pytest.mark.parametrize("name,rows", DIAGONAL_PATTERNS, ids=[p[0] for p in DIAGONAL_PATTERNS])
def test_component_labeling_parity_on_1px_patterns(monkeypatch, name, rows):
    """4-connectivity at diagonal contacts, straight off the labeling helper."""
    w, h = len(rows[0]), len(rows)
    px = bytes(0 if c == "#" else 255 for row in rows for c in row)
    image = Image.frombytes("L", (w, h), px)

    real_np, real_cv2 = areaDef._get_np(), areaDef._get_cv2()
    arr, _binary, aw, ah = areaDef._binarize(image, 128)
    binary = image.point(lambda v: 255 if v < 128 else 0, mode="L")
    assert (aw, ah) == (w, h)

    pure = areaDef._connected_components(bytearray(binary.tobytes()), w, h)
    _force(monkeypatch, np=real_np, cv2=real_cv2)
    assert areaDef._find_components(arr, binary, w, h) == pure, f"{name}: cv2 labeling"
    _force(monkeypatch, np=real_np, cv2=None)
    assert areaDef._find_components(arr, binary, w, h) == pure, f"{name}: numpy labeling"


def test_binarize_matches_the_pil_threshold(monkeypatch):
    """The numpy ink mask and the PIL ink=255 buffer must mark the same pixels."""
    image = _noise(0.3, seed=11)
    real_np = areaDef._get_np()
    for threshold in (150, 160, 185):
        _force(monkeypatch, np=real_np, cv2=None)
        arr, _b, w, h = areaDef._binarize(image, threshold)
        _force(monkeypatch, np=None, cv2=None)
        _a, binary, _w, _h = areaDef._binarize(image, threshold)
        buf = binary.tobytes()
        flat = arr.ravel().tolist()
        assert len(flat) == w * h
        assert flat == [b >= 128 for b in buf], f"threshold {threshold}"
