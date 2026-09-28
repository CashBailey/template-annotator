"""Image comparison for visual regression + the deterministic PNG fixture builders.

The builders live here (rather than in ``scripts/gen_fixtures.py``) so tests and
the generator share ONE definition of what each fixture contains: the module
level constants below are the ground truth every detection assertion asserts
against.

Determinism rules for the builders: pure PIL geometry, no fonts, no randomness,
no timestamps. ``scripts/gen_fixtures.py --check`` enforces byte-identity.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Iterable, Sequence, Tuple

from PIL import Image, ImageChops, ImageDraw

# ---------------------------------------------------------------------------
# Image comparison
# ---------------------------------------------------------------------------

#: Global-constraint tolerances: RMS <= 3.0 on a 0-255 scale, and no more than
#: 0.5 % of pixels may differ by more than DIFF_PIXEL_THRESHOLD in any channel.
RMS_TOLERANCE = 3.0
MAX_DIFF_PIXEL_FRAC = 0.005
DIFF_PIXEL_THRESHOLD = 16


def _as_rgb(img: "Image.Image | os.PathLike | str") -> Image.Image:
    if isinstance(img, Image.Image):
        return img.convert("RGB")
    with Image.open(img) as opened:
        return opened.convert("RGB")


def image_diff_stats(actual, expected) -> Tuple[float, float, Image.Image]:
    """Return ``(rms, diff_pixel_frac, diff_image)`` for two same-size images."""
    a, b = _as_rgb(actual), _as_rgb(expected)
    if a.size != b.size:
        raise ValueError(f"image size mismatch: actual {a.size} != expected {b.size}")

    diff = ImageChops.difference(a, b)
    hists = [band.histogram() for band in diff.split()]
    total = sum(sum(h) for h in hists) or 1
    squares = sum(count * value * value for h in hists for value, count in enumerate(h))
    rms = math.sqrt(squares / total)

    r, g, bl = diff.split()
    worst = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    over = sum(worst.histogram()[DIFF_PIXEL_THRESHOLD + 1:])
    frac = over / float(a.size[0] * a.size[1])
    return rms, frac, diff


def assert_images_match(actual, expected, rms_tolerance: float = RMS_TOLERANCE,
                        max_diff_pixel_frac: float = MAX_DIFF_PIXEL_FRAC) -> None:
    """Assert two images are visually identical within the suite's tolerances.

    On failure, writes ``*-actual/-expected/-diff.png`` to ``$E2E_ARTIFACT_DIR``
    (when that env var is set) so CI can upload them, and raises AssertionError.
    """
    rms, frac, diff = image_diff_stats(actual, expected)
    if rms <= rms_tolerance and frac <= max_diff_pixel_frac:
        return

    written = _write_artifacts(actual, expected, diff)
    raise AssertionError(
        f"images differ: RMS {rms:.3f} (limit {rms_tolerance}), "
        f"{frac * 100:.3f}% of pixels off by >{DIFF_PIXEL_THRESHOLD} "
        f"(limit {max_diff_pixel_frac * 100:.3f}%)"
        + (f"\nartifacts: {written}" if written else "")
    )


def _write_artifacts(actual, expected, diff) -> str:
    art_dir = os.environ.get("E2E_ARTIFACT_DIR")
    if not art_dir:
        return ""
    out = Path(art_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(expected).stem if isinstance(expected, (str, os.PathLike)) else "image"
    base = out / stem
    # Never clobber a previous failure's artifacts.
    n = 0
    while (base.with_name(f"{base.name}{'' if n == 0 else f'-{n}'}-diff.png")).exists():
        n += 1
    suffix = "" if n == 0 else f"-{n}"
    paths = []
    for label, img in (("actual", actual), ("expected", expected), ("diff", diff)):
        p = base.with_name(f"{base.name}{suffix}-{label}.png")
        _as_rgb(img).save(p)
        paths.append(str(p))
    return ", ".join(paths)


# ---------------------------------------------------------------------------
# KNOWN fixture content (assert against these, never against magic numbers)
# ---------------------------------------------------------------------------

BLANK_SIZE = (100, 100)

FORM_SIZE = (800, 600)
#: y of the centre row of each 3-px-thick horizontal rule in form_800x600.png.
FORM_H_RULES = (100.0, 200.0, 300.0, 400.0, 506.0)
#: x of the centre column of each 3-px-thick vertical rule.
FORM_V_RULES = (60.0, 740.0)
FORM_RULE_X = (60, 740)          # horizontal rules run between these x
FORM_RULE_Y = (99, 507)          # vertical rules run between these y
FORM_RULE_HALF_THICKNESS = 1.0
#: FORM_H_RULES[-1] sits exactly 6 px below this y — the snap-test position.
FORM_SNAP_TEST_Y = 500.0

CHECKBOX_SIZE = (600, 400)
CHECKBOX_SIDE = 24               # outer size of every drawn box, in px
CHECKBOX_BORDER = 2
#: Top-left corner of each drawn checkbox; the detector reports
#: (x, y, x + 23, y + 23) for each, i.e. centre (x + 11.5, y + 11.5).
CHECKBOX_ORIGINS = ((80, 80), (280, 80), (480, 80),
                    (80, 240), (280, 240), (480, 240))
CHECKBOX_COUNT = len(CHECKBOX_ORIGINS)
CHECKBOX_CENTERS = tuple((x + (CHECKBOX_SIDE - 1) / 2.0, y + (CHECKBOX_SIDE - 1) / 2.0)
                         for x, y in CHECKBOX_ORIGINS)

QR_SIZE = (400, 300)
QR_PAYLOAD = "AREADEF-E2E-QR-1"
QR_MODULE_PX = 6
QR_ORIGIN = (30, 30)
#: Ink bounding box of the QR symbol (excludes the quiet zone).
QR_BBOX = (30, 30, 155, 155)
BARCODE_PAYLOAD = "5901234123457"   # a valid EAN-13 (check digit 7)
BARCODE_MODULE_PX = 2
BARCODE_ORIGIN = (60, 200)
BARCODE_HEIGHT = 70
BARCODE_BBOX = (60, 200, 249, 269)

#: Version-1 QR matrix for QR_PAYLOAD, '1' = dark module. Precomputed with
#: cv2.QRCodeEncoder and frozen here so fixture bytes never depend on the
#: installed OpenCV build.
QR_MODULES = (
    "111111101100101111111",
    "100000100100101000001",
    "101110101010101011101",
    "101110101001001011101",
    "101110101110001011101",
    "100000100000001000001",
    "111111101010101111111",
    "000000000110000000000",
    "111100101010010011101",
    "000100011010100001110",
    "101111101000110100010",
    "111011011100101010000",
    "101000101011011110111",
    "000000001111001111011",
    "111111100100011110010",
    "100000100000101110000",
    "101110100111010000000",
    "101110101000000001010",
    "101110101100111001100",
    "100000101000100010100",
    "111111101000100111010",
)

# EAN-13 symbol tables (L = odd parity, G = even parity, R = right-hand).
_EAN_L = ("0001101", "0011001", "0010011", "0111101", "0100011",
          "0110001", "0101111", "0111011", "0110111", "0001011")
_EAN_G = ("0100111", "0110011", "0011011", "0100001", "0011101",
          "0111001", "0000101", "0010001", "0001001", "0010111")
_EAN_R = ("1110010", "1100110", "1101100", "1000010", "1011100",
          "1001110", "1010000", "1000100", "1001000", "1110100")
_EAN_PARITY = ("OOOOOO", "OOEOEE", "OOEEOE", "OOEEEO", "OEOOEE",
               "OEEOOE", "OEEEOO", "OEOEOE", "OEOEEO", "OEEOEO")

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


def ean13_bits(code: str) -> str:
    """Encode 13 digits as the 95-module EAN-13 bit string ('1' = bar)."""
    if len(code) != 13 or not code.isdigit():
        raise ValueError(f"EAN-13 needs 13 digits, got {code!r}")
    d = [int(c) for c in code]
    check = (10 - sum(v * (3 if i % 2 else 1) for i, v in enumerate(d[:12], start=1)) % 10) % 10
    if check != d[12]:
        raise ValueError(f"bad EAN-13 check digit in {code!r}: expected {check}")
    bits = ["101"]
    for i, digit in enumerate(d[1:7]):
        bits.append(_EAN_L[digit] if _EAN_PARITY[d[0]][i] == "O" else _EAN_G[digit])
    bits.append("01010")
    bits.extend(_EAN_R[digit] for digit in d[7:])
    bits.append("101")
    return "".join(bits)


# ---------------------------------------------------------------------------
# Fixture builders (pure geometry — no fonts, no randomness)
# ---------------------------------------------------------------------------

def _blank(size: Sequence[int]) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", tuple(size), WHITE)
    return img, ImageDraw.Draw(img)


def build_blank() -> Image.Image:
    """blank_100x100.png — plain white, no ink at all."""
    return _blank(BLANK_SIZE)[0]


def build_form() -> Image.Image:
    """form_800x600.png — 5 horizontal + 2 vertical rules, 3 px thick.

    Nothing else: no glyph-sized ink, so detect-checkboxes finds 0 here and
    detect-lines finds exactly FORM_H_RULES / FORM_V_RULES.
    """
    img, d = _blank(FORM_SIZE)
    x0, x1 = FORM_RULE_X
    y0, y1 = FORM_RULE_Y
    for y in FORM_H_RULES:
        d.rectangle([x0, int(y) - 1, x1, int(y) + 1], fill=BLACK)
    for x in FORM_V_RULES:
        d.rectangle([int(x) - 1, y0, int(x) + 1, y1], fill=BLACK)
    return img


def build_checkboxes() -> Image.Image:
    """checkboxes_600x400.png — exactly CHECKBOX_COUNT hollow squares."""
    img, d = _blank(CHECKBOX_SIZE)
    for x, y in CHECKBOX_ORIGINS:
        d.rectangle([x, y, x + CHECKBOX_SIDE - 1, y + CHECKBOX_SIDE - 1],
                    outline=BLACK, width=CHECKBOX_BORDER)
    return img


def build_qr() -> Image.Image:
    """qr_400x300.png — one QR (QR_PAYLOAD) plus one EAN-13 (BARCODE_PAYLOAD)."""
    img, d = _blank(QR_SIZE)
    ox, oy = QR_ORIGIN
    s = QR_MODULE_PX
    for j, row in enumerate(QR_MODULES):
        for i, module in enumerate(row):
            if module == "1":
                d.rectangle([ox + i * s, oy + j * s, ox + i * s + s - 1, oy + j * s + s - 1],
                            fill=BLACK)
    bx, by = BARCODE_ORIGIN
    mw = BARCODE_MODULE_PX
    for i, bit in enumerate(ean13_bits(BARCODE_PAYLOAD)):
        if bit == "1":
            d.rectangle([bx + i * mw, by, bx + i * mw + mw - 1, by + BARCODE_HEIGHT - 1],
                        fill=BLACK)
    return img


#: name -> builder, consumed by scripts/gen_fixtures.py.
PNG_BUILDERS = {
    "blank_100x100.png": build_blank,
    "form_800x600.png": build_form,
    "checkboxes_600x400.png": build_checkboxes,
    "qr_400x300.png": build_qr,
}


def write_pngs(dest: Path) -> Iterable[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    for name, builder in PNG_BUILDERS.items():
        path = dest / name
        # ponytail: PNG byte-identity assumes a stable Pillow encoder; Pillow is
        # unpinned, so if gen_fixtures.py --check flags drift right after a Pillow
        # upgrade, regenerate and recommit the fixtures.
        builder().save(path, format="PNG", optimize=True)
        yield path
