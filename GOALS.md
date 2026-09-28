# GOALS — areaDef improvement plan (from 2026-07-28 audit)

Source: read-only audit at commit `3623b39` — 6 parallel auditors (correctness, robustness,
performance, architecture, packaging/CI/docs, test gaps) + adversarial verification of every
Critical/Important finding (50 raw → 25 confirmed, 0 refuted, 25 minors). **All `areaDef.py`
line numbers are as of `3623b39`** — re-grep after edits shift them.

How to use: work the phases in order; check items off as they land. Every phase ends green:
`python -m unittest tests.test_areadef` (179, 0 fail, 0 skip) and `pytest tests/` (265+).
New behavior gets a pinning test in the same PR.

---

## Phase 1 — Data-safety PR (do first)

- [x] **1. CRITICAL — "Yes, save" on close can still destroy work** (`areaDef.py:4611`)
  `closeEvent` runs `save_json_dialog()` then unconditionally `event.accept()`. Cancelling
  the file picker or a failed write still closes the window and discards all annotations.
  Fix: `save_json_dialog()` returns `True` only when a file was actually written; on
  `False`, `event.ignore()` and keep the document open.
- [x] **2. Open/Load over a dirty document silently discards everything** (`3101`, `4365`)
  The unsaved-changes prompt exists only in `closeEvent`; `File▸Open` and `Load JSON`
  clear shapes + undo with no warning. Fix: extract a reusable `_confirm_discard()`
  (Yes/No/Cancel, same semantics as closeEvent) and call it from `open_image_dialog` and
  `load_json_dialog`; abort on Cancel.
- [x] **3. Load JSON after failed image decode corrupts projects** (`4350`)
  `os.path.exists` is treated as "loaded"; on decode failure the new shapes are installed
  over the OLD image, and Save writes project B's shapes with project A's path/dims.
  Fix: `load_image()` returns success bool; `load_json` aborts when the referenced image
  exists but fails to load (mirror the missing-file branch).
- [x] **4. Hostile/corrupt project JSON aborts the GUI process** (`4447`, `4440`, `4350`)
  Verified by execution: `Infinity` page/id → `OverflowError` (not in the `except
  (TypeError, ValueError)` tuples); 400-digit bbox ints → `OverflowError`; non-string
  `image.path` (int `1` passes `os.path.exists` as a file descriptor!). Unhandled Qt-slot
  exception → PyQt6 `qFatal()`. Fix: add `OverflowError` to both except tuples, reject
  non-`str` `image.path`, wrap `load_json` body in try/except with a QMessageBox, and
  install a `sys.excepthook` that shows a dialog instead of aborting.
- [x] **5. Corrupt PDF page N aborts the app on page-flip** (`3223`, `4390`)
  Only page 0 is probe-rendered; `_set_pdf_page` and `load_json`'s PDF branch render
  unguarded inside Qt slots. Fix: try/except around `_pdf_page_image`, show a message,
  stay on the current page (don't commit `_current_pdf_page` until render succeeds).
- [x] **6. CLI `snap-to-lines` corrupts multi-page projects** (`5091`)
  `Project.snap_to_lines` has no page filter, so one page's detected lines move shapes on
  every page (GUI version filters). Fix: thread the detection page into
  `Project.snap_to_lines`; skip shapes whose `s.page` differs for PDF sources. Update the
  J5 e2e fixture to snap a shape on the page it is snapped against.
- [x] **Tests pinning this phase** (audit #18, #19):
  - [x] D6 e2e: save via Ctrl+S → File▸Load JSON round-trip; assert shapes/pages/next_sid/
        title. Plus one malformed-JSON case and one out-of-range-page PDF case asserting
        the recorded warning/critical dialog.
  - [x] Dirty-transition e2e: draw (dirty) → Ctrl+O, pin all three prompt answers the way
        D4 does.

## Phase 2 — Decoupling (5-minute win, then lazy imports)

- [x] **13. Move `_parse_shapes` off the window class** (`4968`, staticmethod at `4424`)
  Pure dict→Shape logic parked on `AnnotatorWindow`; the ONLY headless→GUI dependency.
  Move to module level next to `Shape`; makes `Project`'s "no Qt dependency" docstring
  (4944) true and unlocks a later core/cli/gui package split with `areaDef.py` as shim.
- [x] **12. Lazy-import Qt and CV backends** (`81–132`; measured 377 ms vs 13 ms bare)
  *(CV half done — CLI 474→286 ms, sys.modules pin added. Qt half SKIPPED: needs package
  split — Qt subclasses are defined at module level; only ~25 ms at stake.)*
  Move cv2/numpy/pyzbar/pupil_apriltags imports into the backend functions (first-call,
  cached in the same module globals). Defer PyQt6 to the gui/screenshot paths. Pure-JSON
  subcommands drop to Pillow+stdlib startup.

## Phase 3 — CI hardening (cheapest permanent win)

- [x] **17. Add a lint lane** (`.github/workflows/tests.yml`)
  Seconds-fast first job: `pip install ruff && ruff check .` (F-rules catch undefined
  names in unclicked GUI slots). Optionally `--cov=areaDef --cov-report=term` on the fast
  lane for visibility — no threshold yet.
- [x] **CI runs matrix twice per PR, never cancels superseded runs** (tests.yml:3)
  `on: push: branches: [main]` + workflow-level
  `concurrency: {group: "${{ github.workflow }}-${{ github.ref }}", cancel-in-progress: true}`.
- [x] **`needs: unit` serializes e2e behind both unit legs** (tests.yml:47)
  Drop it (or gate both lanes on the new lint job instead).

## Phase 4 — Performance cluster (worst-first, all verified/benchmarked)

- [x] **7. Pure-Python per-pixel detectors freeze the UI** (`208–426`, callers `3722+`)
  Measured 1.11 s/pass at 8.4 MP; L/X/B/G workflow ≈ 10–30 s frozen. numpy is already an
  optional import: binarize once per page, vectorize run-scan + component labeling
  (cv2.connectedComponentsWithStats when present), keep pure-Python fallback. Move
  detection off the UI thread (QRunnable) — the wait cursor doesn't stop the freeze.
- [x] **8. `detect_text_boxes` satellite loop O(marks × all glyphs)** (`832`)
  Loop-invariant `lo`/`hi` recomputed per (mark, line) pair → ~40–80 s on speckled scans.
  Two-line hoist into the line-setup loop; optionally sort lines by cy and bisect.
- [x] **9. Selection click rebuilds the whole scene** (`4162`; callers `3291/3321/2099`)
  ~1,900 QGraphicsItems destroyed/recreated per click (incl. per-shape `addText`).
  Restyle in place: persistent overlay layers, setPen/setBrush on selection deltas, move
  the 4 handles. Full rebuild only for load/page/theme changes.
- [x] **10. Undo deep-copies 2–3× per edit; nudge auto-repeat floods history** (`1819`, `2947`)
  `save_state` should take ownership (append, don't re-clone); same for popped states in
  do_undo/do_redo. Coalesce nudges: one snapshot per burst (~300 ms QTimer).
- [x] **11. Live snap re-derives all snap lines per mouse-move** (`1037`, `3747`)
  Measured 3.1 ms/move at 2 k boxes; ~1.9 s for 500-shape batch snap. Cache the derived
  (h+text+hc, v+text+hc) lists per page beside `_line_cache`/`_text_cache`/`_hc_cache`,
  invalidate in detect_*/cache-clear paths; optionally bisect sorted axis lists.

## Phase 5 — Architecture debt

- [x] **14. Column-normalize algorithm exists twice** (`1095` vs `4744`, both `tol = 0.02`)
  `_normalize_json_payload` should parse via (now module-level) `_parse_shapes`, run
  `normalize_shapes`/`normalize_rect_columns`, serialize via `as_export_dict`. Delete the
  dict-based clone. Add a GUI-vs-CLI parity test.
- [x] **15. Snap logic diverged: CLI emits elliptical "circles"** (`5088` vs `3803`)
  Move post-snap kind fix-up (circle re-square, chamfer clamp, MIN_SHAPE_SIZE_PX guard)
  into one `apply_snapped_bbox(shape, bbox)` used by both GUI and Project paths
  (`_cli_align` at 5372 already does the fix-up — make it universal).
- [x] **16. God methods** (`_build_ui` 274 ln @2421, `main()` 231 @5548, `_build_menus` 189)
  (a) split `_build_ui` into per-group builders; (b) `set_defaults(func=...)` per
  subparser so main()'s 18-branch dispatch collapses to `args.func(args)`; (c) leave
  `_build_menus`/`detect_text_boxes` alone until touched.
- [x] **20/21. Remaining test gaps**
  - [x] `duplicate_selected` + context menus (Ctrl+D event test asserting _copy suffix,
        +20 offset, unique sid, dirty flag; QMenu.exec stub fixture for right-click
        Delete/Duplicate/Zoom). Note: duplicate is the only mutation with NO
        `_clamp_bbox` — fix the out-of-bounds clone while pinning it (`3591`).
  - [x] Marker/barcode degraded configs: monkeypatch `areaDef._pyzbar = None` /
        `_cv2 = None` in turn; pin first-wins dedupe; split J3's double importorskip.

## Phase 6 — Hygiene sweep (minors, ~1 line each; batch into one PR)

Correctness:
- [x] Ghost shapes on raster open — clear state before `_set_canvas_image()` (`3098`)
- [x] Undo/redo never set dirty flag → save-then-undo closes unprompted (`3447`)
- [x] Edge-clamped circles persist as ellipses, radius≠bbox — clamp keep_size=True (`3375`)
- [x] Page-clamp can recreate duplicate (page, sid) after dedup — clamp before dedup (`4374`)
- [x] Stale multi-selection survives Load JSON in narrow cases — reset `selected_sids` (`4396`)

Robustness:
- [x] Atomic saves reset perms to 0600 — fchmod umask-derived or copy st_mode (`1268`)
- [x] Sanitize names (strip non-printables) at ingest; guard CSV `=+-@` formula cells (`5450`)
- [x] Save dialogs append suffix AFTER overwrite check — setDefaultSuffix instead (`4319`)

Performance:
- [x] Skip redundant `convert("RGBA")` on already-RGBA; one-pass invert; cache inverted page (`3176`)
- [x] PDF page cache: evict by byte budget (~100 MB), not count of 5 (`2367`)

Architecture/hygiene:
- [x] Fold the ×3 detection-wrapper block into `_run_cached_detection(...)` (`3716`)
- [x] Extract `_load_or_new_project()` for `--into` boilerplate; reuse add_boxes dedup (`5177`)
- [x] Delete `areaDef.py.bak` (byte-identical) and stale `tests/test_areadef.py.bak`
- [x] Delete dead `_median` (+ its test); optionally `statistics.fmean` for `_mean` *(fmean SKIPPED: raises on empty input — contract change)* (`1060`)
- [x] `Line` NamedTuple + `Detection` TypedDict; delete the `len(line) > 3` guard *(types done; guard KEPT — SKIPPED: 3-tuple lines are a tested public entry point)* (`879`)
- [x] Delete `meta_prompt.md` or move to docs/history/ with a "historical brief" header

Packaging/docs:
- [x] README:88 quick-start cds into `/home/gatorhub/Desktop/driverApplication` — delete block
- [x] Add `--version` (importlib.metadata), tag `v1.1.0`, start CHANGELOG.md
- [x] pyproject: drop dead `[tool.setuptools.packages.find]`; fix/drop pythonw-broken
      gui-script; add classifiers + `[project.urls]`
- [x] Shrink requirements.txt to `-e .[pdf,markers]` (pyproject = single source of truth);
      optional CI constraints file for reproducibility *(constraints file SKIPPED: optional, deferred)*

Test suite:
- [x] `zoom_to_selection` (Z) event test (`2960`)
- [x] Theme menu test — REAL DEFECT: selecting "Auto" immediately unchecks itself
      (`_set_theme`/`_apply_theme` desync, `2998`); fix + pin; optional dark golden *(dark golden SKIPPED: optional; Auto-desync fixed + pinned)*
- [x] Parametrized guard-dialog test: L/B/X/G/M/K/N/Ctrl+S/Ctrl+E on empty window →
      one warning each, no crash (`3675` et al.)
- [x] `timeout = 180` in `[tool.pytest.ini_options]`; drop redundant CI flags
- [x] `encoding="utf-8"` in CliRunner subprocess call; one journey using a non-ASCII
      output directory (`tests/helpers/cli.py:45`)

---

Verification after each phase:

```bash
python -m unittest tests.test_areadef        # 179 ran, 0 failures, 0 skips
python -m pytest -m "not e2e and not gui and not visual" -n auto
python -m pytest tests/e2e -ra               # all green, serial
python scripts/gen_fixtures.py --check       # byte-identical
```
