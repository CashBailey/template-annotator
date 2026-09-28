# Irregular (Multi-Rect) Areas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users merge 2+ axis-aligned boxes into one named L/T-shaped region (kind `multirect`), split it back apart, and round-trip it through JSON/CSV/CLI/GUI.

**Architecture:** `Shape` gains an optional `rects` piece-list; the shape's own coords stay the derived envelope, so every bbox-reading consumer keeps working. Merge/Split are pure module functions reused by `Project` (CLI) and `AnnotatorWindow` (GUI). Rendering is a `QPainterPath` union; hit-testing is point-in-any-piece; all mutations on a multirect are whole-unit translations.

**Tech Stack:** Python 3.9+, PyQt6, existing test stack (stdlib unittest for tests/test_areadef.py, pytest + pytest-qt for tests/e2e/).

**Spec:** docs/superpowers/specs/2026-07-28-irregular-areas-design.md — the authoritative requirements. Read it first.

## Global Constraints

- Interpreter for everything: `/home/gatorhub/areaDef/.venv/bin/python`.
- tests/test_areadef.py stays runnable by plain `python -m unittest` with zero non-stdlib test deps (PIL/areaDef imports fine; no pytest, no tests/helpers imports beyond what it already uses).
- Fixtures byte-deterministic (`python scripts/gen_fixtures.py --check` green); goldens NEVER regenerated (no rendering change to existing kinds).
- `ruff check .` clean (select-pin lives in pyproject).
- Every behavior change lands with a pinning test in the same commit; TDD (red → green) for each task's core behavior, with the red evidence noted in the task report.
- Line numbers in this plan are NOT given — grep for the anchors named in each task; areaDef.py is ~6,400 lines and drifts.
- Full verification per task: `python -m unittest tests.test_areadef` (0 fail/0 skip), `python -m pytest -m "not e2e and not gui and not visual" -q -n auto`, `python -m pytest tests/e2e -q --timeout=180` (serial), `gen_fixtures --check`, `ruff check .`.
- Commit messages: imperative, end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

### Task 1: Core model, parse/export, merge/split logic, CLI

**Files:**
- Modify: `areaDef.py` — `VALID_KINDS`, `Shape` dataclass (grep `class Shape`), `as_export_dict`, `_parse_shapes` (module-level), `_shapes_to_csv` (grep it), `Project` (grep `class Project`), argparse `edit-shape` subparser + its `_cli_` handler (grep `edit-shape`).
- Test: `tests/test_areadef.py` (new `TestMultirect` class; stdlib-pure).

**Interfaces:**
- Consumes: existing `Shape`, `_parse_shapes`, `Project`, `_atomic_write_text`, existing edit-shape CLI conventions (`--id`, page-aware mutators, `-o`).
- Produces (later tasks rely on these exact names):
  - `Shape.rects: Optional[List[Tuple[float, float, float, float]]] = None` (dataclass field; `None` for non-multirects)
  - `def multirect_envelope(rects: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]` — min/max over sorted pieces
  - `def merge_shapes(shapes: List[Shape], sid: int) -> Shape` — pure; flattens rect bboxes + multirect pieces; kind `"multirect"`; name = `shapes[0].name`; coords = envelope; raises `ValueError` if fewer than 2 source shapes, if any kind not in (`"rect"`, `"multirect"`), or if shapes span pages
  - `def split_shape(shape: Shape, first_sid: int) -> List[Shape]` — pure; one `rect` per piece, names `f"{shape.name}_{i}"` (1-based), sids `first_sid, first_sid+1, …`, same page; raises `ValueError` if kind != `"multirect"`
  - `Project.merge(ids: List[int], page: int = 0) -> int` — returns new sid; `Project.split(sid: int, page: int = 0) -> List[int]`
  - CLI: `edit-shape PROJECT --merge "ID1,ID2[,…]"` and `edit-shape PROJECT --split --id N` (both honor `--page`, `-o`, and print a one-line summary; errors exit 1 with `Error:` prefix, no traceback)

- [ ] **Step 1: Write failing unit tests** (add to tests/test_areadef.py; excerpt — write all of these plus envelope-sync and CSV cases):

```python
class TestMultirect(unittest.TestCase):
    def _rect(self, sid, name, x1, y1, x2, y2, page=0):
        return areaDef.Shape(sid, "rect", name, x1, y1, x2, y2, page=page)

    def test_merge_two_rects_makes_a_multirect_with_envelope(self):
        a = self._rect(1, "company", 312, 45, 1114, 155)
        b = self._rect(2, "company_b", 236, 168, 1114, 265)
        m = areaDef.merge_shapes([a, b], sid=7)
        self.assertEqual(m.kind, "multirect")
        self.assertEqual(m.sid, 7)
        self.assertEqual(m.name, "company")            # first-selected wins
        self.assertEqual(len(m.rects), 2)
        self.assertEqual(m.bbox(), (236.0, 45.0, 1114.0, 265.0))  # envelope

    def test_merge_flattens_nested_multirects(self):
        a = self._rect(1, "a", 0, 0, 10, 10)
        b = self._rect(2, "b", 5, 5, 20, 20)
        m1 = areaDef.merge_shapes([a, b], sid=3)
        c = self._rect(4, "c", 100, 100, 120, 120)
        m2 = areaDef.merge_shapes([m1, c], sid=5)
        self.assertEqual(len(m2.rects), 3)             # pieces, not nesting

    def test_merge_refuses_circles_and_single_shapes(self):
        a = self._rect(1, "a", 0, 0, 10, 10)
        c = areaDef.Shape(2, "circle", "c", 0, 0, 10, 10)
        with self.assertRaises(ValueError):
            areaDef.merge_shapes([a, c], sid=3)
        with self.assertRaises(ValueError):
            areaDef.merge_shapes([a], sid=3)

    def test_split_restores_rects_with_derived_names(self):
        a = self._rect(1, "blk", 0, 0, 10, 10)
        b = self._rect(2, "x", 5, 5, 20, 20)
        m = areaDef.merge_shapes([a, b], sid=3)
        parts = areaDef.split_shape(m, first_sid=10)
        self.assertEqual([p.kind for p in parts], ["rect", "rect"])
        self.assertEqual([p.name for p in parts], ["blk_1", "blk_2"])
        self.assertEqual([p.sid for p in parts], [10, 11])
        self.assertEqual(parts[0].bbox(), (0.0, 0.0, 10.0, 10.0))

    def test_export_dict_carries_envelope_bbox_and_rects(self):
        m = areaDef.merge_shapes(
            [self._rect(1, "a", 0, 0, 10, 10), self._rect(2, "b", 5, 5, 20, 20)], sid=3)
        d = m.as_export_dict()
        self.assertEqual(d["kind"], "multirect")
        self.assertEqual(d["bbox"], [0.0, 0.0, 20.0, 20.0])
        self.assertEqual(d["rects"], [[0.0, 0.0, 10.0, 10.0], [5.0, 5.0, 20.0, 20.0]])

    def test_parse_shapes_round_trips_and_drops_malformed_multirects(self):
        good = {"id": 1, "kind": "multirect", "name": "m", "page": 0,
                "bbox": [999, 999, 1000, 1000],          # ignored: recomputed
                "rects": [[0, 0, 10, 10], [5, 5, 20, 20]]}
        bad = {"id": 2, "kind": "multirect", "name": "b", "page": 0,
               "bbox": [0, 0, 1, 1], "rects": [[0, 0, 10, 10]]}   # <2 pieces
        shapes, skipped, coerced = areaDef._parse_shapes([good, bad])
        self.assertEqual(len(shapes), 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(shapes[0].bbox(), (0.0, 0.0, 20.0, 20.0))
```

- [ ] **Step 2: Run to verify red** — `python -m unittest tests.test_areadef.TestMultirect -v` → every test errors (`AttributeError: module 'areaDef' has no attribute 'merge_shapes'` etc.). Record the tail.
- [ ] **Step 3: Implement the model** — add `"multirect"` to `VALID_KINDS`; `rects` field on `Shape` (default `None`; `clone()` must deep-copy the list); `multirect_envelope`; envelope re-sync helper used by any piece mutation; `as_export_dict` `rects` emission (3-dp, sorted pieces); `_parse_shapes` multirect branch (validate each piece with the same finite/4-float rules as bbox; sort each piece; <2 valid pieces ⇒ count as malformed/skipped; recompute envelope, ignore input bbox).
- [ ] **Step 4: Implement `merge_shapes` / `split_shape`** exactly per Interfaces, near `Shape`.
- [ ] **Step 5: `Project.merge` / `Project.split`** — thin wrappers: resolve ids on the page (missing id ⇒ `ValueError` with the id in the message), call the pure functions, allocate sids via the project's existing next-sid logic, replace shapes in place preserving list order position of the first source.
- [ ] **Step 6: CSV** — `_shapes_to_csv` gains a final `rects` column: `json.dumps(d["rects"])` for multirects, `""` otherwise. Update the existing CSV header assertions in tests (they pin exact columns — extend, don't loosen).
- [ ] **Step 7: CLI** — `edit-shape` gains `--merge "IDS"` (comma list, ≥2 ids) and `--split` (uses existing `--id`); mutually exclusive with each other and with `--bbox/--move/--delete/--duplicate/--name` (argparse mutually-exclusive group if one exists, else explicit check → exit 1). Success prints e.g. `Merged 2 shape(s) into id 7 ('company').` / `Split id 7 into 2 rect(s).`. Add CLI tests in the same class using the existing `_run` subprocess helper pattern: happy paths (JSON on disk verified), circle-in-selection refusal, unknown id, `--merge 3` (single id) refusal, page-awareness.
- [ ] **Step 8: Run green** — the new class, then the four verification commands + ruff.
- [ ] **Step 9: Commit** — `feat: multirect core — model, merge/split, JSON/CSV, CLI`.

### Task 2: GUI — rendering, hit-testing, interactions, Merge/Split actions

**Files:**
- Modify: `areaDef.py` — `CanvasView` (grep `_hit_shape`, `_build_shape_items`, `restyle_selection`), `AnnotatorWindow` (grep `_build_menus`, `_popup_shape_menu`, `duplicate_selected`, `_nudge_selected`, `align_selected`, `_on_geometry_edited`, `_sync_geometry_fields` or the geometry-spinbox sync method, `snap_fields_to_lines`, `maybe_snap_live`).
- Test: `tests/test_areadef.py` (offscreen GUI tests in the existing style — direct method calls allowed here; real-event coverage is Task 3).

**Interfaces:**
- Consumes: Task 1's `merge_shapes`, `split_shape`, `Shape.rects`, `multirect_envelope`.
- Produces: `AnnotatorWindow.merge_selected()` and `AnnotatorWindow.split_selected()` slots; Edit-menu actions "Merge Selected" (Ctrl+M) and "Split"; both context menus extended; multirect entries in `_build_shape_items`/`restyle_selection`/`_hit_shape`.

- [ ] **Step 1: Failing offscreen tests first** (same file/style as existing `TestGuiBehavior`): hit-test — multirect of `[(0,0,100,50),(0,50,40,150)]`: point (80,100) (envelope yes, piece no) → `None`; point (20,100) → hit. `merge_selected()` with two rects selected → one multirect, `len(win.shapes)` drops by 1, ONE undo step (undo restores both rects with original sids), dirty flag set. `merge_selected()` with a circle in the selection → no change + status message set. `split_selected()` reverses. Nudge on a multirect moves every piece by exactly (dx,dy) and the envelope follows. Geometry spinboxes disabled when a multirect is selected, re-enabled for a rect. Align Match-W refuses for multirect selections.
- [ ] **Step 2: Red run**, record tail.
- [ ] **Step 3: Rendering** — in `_build_shape_items`, kind `multirect` → `QPainterPath` with `addRect` per piece, `path.setFillRule`/`united` as needed → `scene.addPath(...)` with the same pen/brush from `_shape_pen_brush`; label at envelope top-left like rects. `restyle_selection` needs no special case beyond the existing item-map (path items restyle via `setPen`/`setBrush` the same way); the kind-change validity gate already forces a rebuild when a merge/split swaps kinds.
- [ ] **Step 4: Hit-testing** — `_hit_shape`: multirect hits iff the point is inside any piece (respect existing z-order/topmost rules). Resize handles: `_hit_handle` must never return handles for multirects (no handles are drawn for them — key-shape handle drawing skips multirects).
- [ ] **Step 5: Whole-unit translation** — every path that today writes x1/y1/x2/y2 as a *move* (mouse move-drag, `_nudge_selected`, `duplicate_selected`, translation aligns, envelope snap-on-move) must, for multirects, apply the same delta to every piece then re-sync the envelope. Root-cause it: add one `Shape.translate(dx, dy)` method used by all of those paths for every kind (rects translate coords; multirects translate pieces + envelope), instead of per-caller special cases. Resize-shaped paths (`_hit_handle` resize drag, Match W/H/Both, `_on_geometry_edited` writes, snap-resize) refuse or are disabled for multirects: geometry spinboxes disabled with status hint "Split to edit pieces"; Match W/H/Both shows the existing refusal-style status message.
- [ ] **Step 6: Actions & menus** — `merge_selected()`: guard (2+ selected, current page, kinds all rect/multirect — else status message and return), snapshot undo once, build via `merge_shapes` with a fresh sid, remove sources, insert, select the new shape, dirty flag, `update_list`, redraw. `split_selected()`: single selected multirect only; inverse. Edit menu: "Merge Selected" Ctrl+M, "Split" (no shortcut). Both context menus (canvas `_popup_shape_menu`, areas-list menu) gain the entries, enabled per the same guards. Ensure Ctrl+M doesn't collide (grep existing shortcuts).
- [ ] **Step 7: Snap/normalize exclusions** — `snap_fields_to_lines` and `Project.snap_to_lines` skip multirects (count them out of the "Snapped N" message); `maybe_snap_live` snaps the envelope translation-only during moves (works via `translate`); normalize already filters kinds — verify `--all-kinds` path skips multirects (it parses via `_parse_shapes`, then normalize_shapes: confirm multirects pass through untouched and add one assertion for it in Task 1's class if not already covered).
- [ ] **Step 8: Green run** — new tests + all four verification commands + ruff. Goldens must be untouched (no golden project contains a multirect).
- [ ] **Step 9: Commit** — `feat: multirect GUI — union rendering, piece hit-testing, Merge/Split actions`.

### Task 3: E2E coverage + docs

**Files:**
- Create: `tests/e2e/test_gui_multirect.py`
- Modify: `tests/e2e/test_cli_journeys.py` (new J10 journey function), `README.md` (features list + a short "Irregular areas" paragraph + CLI examples), `CHANGELOG.md` (`[Unreleased]` → new entry).

**Interfaces:**
- Consumes: everything from Tasks 1–2; existing e2e helpers (`drag`, `key`, `canvas_point`, `menu_action`, `annotator_window`, QMenu stub fixture, `cli` runner).

- [ ] **Step 1: GUI e2e (real events, pytestmark `[e2e, gui]`)** — one journey test: draw rect A (drag), draw overlapping rect B, Ctrl+click both, Ctrl+M → assert one shape, kind multirect, 2 pieces, expected envelope ±2 px; click in the notch (region inside envelope, outside both pieces) → selection cleared; click inside a piece → selected; arrow-nudge → every piece moved 1 px; Ctrl+Z → the two rects return (sids/bboxes); redo; context-menu Split via the QMenu stub → two rects named `*_1`/`*_2`. Plus one guard test: rect+circle selected, Ctrl+M → model unchanged.
- [ ] **Step 2: CLI e2e (J10)** — `new-project` → `add-shape` ×2 rects → `edit-shape --merge "1,2" -o out.json` → on-disk JSON has kind multirect + `rects` + envelope bbox → `export-csv` → `rects` column parses as JSON with 2 pieces → `edit-shape --split --id 3 -o back.json` → two rects → `screenshot` on the merged project produces a PNG (magic bytes; no golden). Also error paths: `--merge "1"` exit 1; `--merge` including a circle exit 1 with message, no output file.
- [ ] **Step 3: Red→green** — the new e2e files against the Task-2 tree should pass immediately; if any test passes against a tree WITHOUT Tasks 1–2 it's vacuous — spot-check the journey fails on `main` before this branch's commits (checkout not required: assert on multirect-specific fields, which can't exist on old code).
- [ ] **Step 4: Docs** — README: add "**Irregular areas** — merge overlapping boxes into one L/T-shaped region (Ctrl+M), Split to reshape; exports envelope `bbox` + exact `rects`" to the features list; extend the CLI section with the two edit-shape examples; CHANGELOG entry under `[Unreleased]`.
- [ ] **Step 5: Full verification** — all four commands + ruff; counts reported.
- [ ] **Step 6: Commit** — `feat: multirect e2e journeys + docs`.
