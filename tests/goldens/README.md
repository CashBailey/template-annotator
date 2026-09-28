# Visual regression goldens

Committed reference PNGs for `tests/e2e/test_visual_regression.py`. The oracle is the
`screenshot` CLI subcommand, canvas mode only (never `--window`), always `--theme light`.
Each screenshot runs in its own subprocess, so every golden and every comparison run gets
fresh, isolated Qt state.

## Platform directory

Goldens are keyed by platform + Qt/Freetype toolchain, since pixel-level rendering is not
byte-portable across operating systems (font hinting, antialiasing, and Qt's own backend all
differ). Only `linux-py/` exists today. If `tests/goldens/<platform>/` is absent for the
platform running the suite, `test_golden_screenshot` skips with a clear reason instead of
failing — the two structural checks in the same file (`test_screenshot_size_matches_source`,
`test_screenshot_shapes_vs_cleared_differ`) are golden-free and always run, on any platform.

## What each golden shows

All four goldens draw shapes with **empty names** (`""`), so no text label is ever rendered —
see "Font pinning" below for why. Geometry (rect/circle/chamfer outlines, page content) is
still fully exercised.

- **`g1_simple_rects.png`** (800x600) — the committed `fixtures/projects/simple_rects.json`
  project (3 rects: `header`, `field_a`, `field_b`) over `fixtures/form_800x600.png`, with
  every shape's name blanked before the shot.
- **`g2_circle_chamfer.png`** (600x400) — one circle and one chamfer-rect, added over
  `fixtures/checkboxes_600x400.png`. The test first round-trips a **unicode** shape name
  (`"円★"` / `"étiquette"`) through `add-shape`/`edit-shape` to prove the CLI handles
  non-ASCII names end to end, then blanks both names before taking the golden screenshot —
  the committed PNG itself carries no rendered glyphs.
- **`g3_pdf_page1.png`** (800x600) — page 1 (index 0) of `fixtures/sample_form.pdf`, rendered
  at the project's standard 2x scale, with two unlabeled rects placed over the page's known
  title band and first checkbox.
- **`g4_sourceless_blank.png`** (640x480) — a project with **no source image** at all
  (`image.path: null`); `_cli_screenshot` synthesizes a blank white page from the declared
  640x480 dims. One rect, one circle, one chamfer rect, all unlabeled. No image decode and no
  font dependency at all, which makes this the most stable of the four goldens.

## Font pinning

`AnnotatorWindow` draws each shape's `name` as a `QGraphicsTextItem` when its "Show labels on
canvas" option is on (the default in a fresh window, which is what `screenshot` always
constructs). Text rasterization depends on whichever font family Qt's "sans-serif" resolves
to, which is **not** the same on every machine:

- This dev box (where the goldens were generated) has Noto/Ubuntu/URW font families
  installed; `fc-match` resolves the default sans-serif to **Noto Sans**.
- CI installs only `fonts-dejavu-core`/`fonts-dejavu-mono`, so it would resolve to
  **DejaVu Sans** instead.

Committing a golden with rendered text labels would therefore be pinned to whatever font the
*generating* machine happened to have installed, not to anything CI can reproduce — a
guaranteed mismatch, not a rare flake. Rather than chase font parity across machines, every
golden shape has its name blanked (`edit-shape --name ""`) immediately before the screenshot
that becomes the golden, so `_build_shape_items` still creates a label item but it has zero
glyphs to draw. All four goldens are geometry-only. (This is the fallback the task brief
anticipated for goldens 1–3; it was applied to all four here rather than waiting to see it
flake, since the font mismatch is systematic, not intermittent.)

If CI or a future contributor needs label rendering covered visually, that requires either
pinning the *same* font package on every machine that generates or checks goldens (matching
CI's `fonts-dejavu-core`), or moving to a much looser, per-pixel-cluster comparison that
tolerates hinting differences — neither is done here.

## Regenerating goldens

```bash
# Rewrite all 4 goldens under tests/goldens/linux-py/, then self-check them
# (writes, then re-screenshots and compares the fresh shot against what was
# just written — a bad regeneration fails immediately, not on the next run).
python -m pytest tests/e2e/test_visual_regression.py::test_golden_screenshot --update-goldens

# Equivalent, for environments that prefer an env var over a pytest flag:
UPDATE_GOLDENS=1 python -m pytest tests/e2e/test_visual_regression.py::test_golden_screenshot
```

Regenerate only after a deliberate rendering change (canvas colors, pen widths, shape
drawing code); commit the new PNGs in the same change that caused them to move. Running
`--update-goldens` twice in a row must produce byte-identical files — if it doesn't, something
in the render path is non-deterministic and should be fixed before the goldens are trusted.

## Tolerance rationale

`assert_images_match` (in `tests/helpers/images.py`) uses the suite-wide global-constraint
tolerances, unchanged from their defaults:

- **RMS ≤ 3.0** on a 0–255 per-channel scale, computed over `ImageChops.difference` across
  all three RGB channels. Absorbs the last bit or two of PNG/Qt rounding noise while catching
  any real rendering regression (a moved shape, a missing outline, a theme color change) by a
  wide margin.
- **≤ 0.5 % of pixels** may differ by more than 16 (0–255) in their worst channel. Catches a
  small-but-solid change (e.g. one shape rendered in the wrong color) that could otherwise
  hide under a low whole-image RMS.

Exact image size is checked before either statistic — a size mismatch is a `ValueError` from
`assert_images_match` (or an explicit assertion in the test), never a silently-passing
comparison.

Measured margin on this machine: all four goldens compare **RMS 0.0000** against a freshly
taken screenshot (see `test_golden_screenshot`'s `[golden]` log line), and two independent,
same-arguments subprocess screenshots of the same project are **byte-identical** (see
`test_screenshot_determinism_across_runs`'s `[determinism]` log line) — offscreen-Qt canvas
rendering on this box has no run-to-run antialiasing noise at all. The 3.0/0.5% tolerance is
headroom for a different toolchain (CI's PyQt6/Freetype build), not something this suite is
currently spending.
