# Irregular (multi-rect) areas — design

Date: 2026-07-28 · Status: approved by user · Base: main @ 032efd4 (v1.1.0)

## Goal

Let a defined area be irregularly shaped (L/T/etc.) by **merging two or more
axis-aligned boxes into one named region**, and splitting it back apart to
reshape. Decided with the user:

- **Rectilinear only** — all edges horizontal/vertical; no diagonal polygons.
- **Creation = merge only** — draw normal rects, select 2+, Merge. No
  line-by-line outline tool (addable later if merging feels clumsy).
- **One unit + Split** — a merged area selects/moves/renames/duplicates/deletes
  as a single shape; reshaping = Split → edit pieces → re-Merge. No live
  per-piece handles.
- **Export = envelope + pieces** — user controls downstream consumers; old
  readers keep working off `bbox` (envelope), new readers use `rects`.

## Data model

- `VALID_KINDS` gains `"multirect"`.
- `Shape.rects: Optional[List[Tuple[float, float, float, float]]]` — present
  iff kind == "multirect"; ≥ 2 pieces; each piece sorted (x1 ≤ x2, y1 ≤ y2).
  Pieces may overlap; disjoint pieces are legal (region may be discontiguous).
- The shape's own x1/y1/x2/y2 is the **envelope** (min/max over pieces), kept
  in sync whenever pieces change; `bbox()` returns it.
- `as_export_dict`: normal dict plus `"rects": [[…4 floats…], …]`, 3-dp
  rounded, `bbox` = envelope.
- `_parse_shapes`: accepts `rects` for kind multirect; validates each piece
  like a bbox (finite, 4 numbers); < 2 valid pieces ⇒ shape is malformed
  (existing drop+warn path). Envelope is recomputed on load (input `bbox`
  ignored for multirects).
- CSV export: existing columns unchanged (bbox columns = envelope); new last
  column `rects` containing the piece list as a JSON string, empty for other
  kinds.

## Merge / Split operations

- **Merge Selected** — Ctrl+M, Edit menu, canvas context menu, list context
  menu. Enabled when 2+ shapes selected, all on the current page, every one a
  `rect` or `multirect`. Circles/chamfers in the selection ⇒ refuse with a
  status-bar message (no dialog).
  Result: one new multirect whose pieces = all selected rects + all pieces of
  selected multirects (flattened); name = first-selected shape's name; new
  sid; original shapes removed; ONE undo step.
- **Split** — Edit menu + both context menus; enabled only for a single
  selected multirect. Replaces it with plain rects named `<name>_1..n`
  (existing collision-free naming rules apply), new sids; ONE undo step.
- **CLI**: `edit-shape --merge ID1,ID2[,…]` and `edit-shape --split ID`
  (page-aware like other mutators; same refusal rules; output via -o as
  usual).

## Canvas / GUI behavior

- Drawing is unchanged (multirects are only ever created by Merge).
- Rendering: one QGraphicsPathItem from the union of piece QRectFs
  (QPainterPath united), same pen/brush/selection styling rules as rects;
  label at envelope top-left. Restyle-in-place (Phase 4) treats it like any
  other item; kind-change gate already forces rebuild when kinds change.
- Hit-testing: point-in-any-piece. Clicks in the envelope but outside every
  piece (the notch of an L) do NOT hit the shape.
- Move-drag, arrow nudge (1 px / Shift 10 px), duplicate (+20 px): translate
  every piece by the same delta; clamped keep-size against the envelope.
- No resize handles for multirects. Geometry X1/Y1/X2/Y2 spin boxes show the
  envelope read-only (disabled) with a status hint "Split to edit pieces".
- Snap-on-move: envelope edges snap (translation only). No snap-resize.
- Align: translation-based aligns (left/right/top/bottom/center) work on the
  envelope; Match W/H/Both refuse for multirects (status message).
- normalize (GUI N + CLI): unchanged — it filters `kind == "rect"`, so
  multirects are naturally skipped (also under --all-kinds: skipped, because
  scaling pieces is out of scope; document in --help if wording implies
  otherwise).
- snap-to-lines batch (GUI + CLI): multirects skipped (translation-snap of a
  merged block to a line is out of scope; revisit on demand).

## Out of scope (explicit)

Diagonal/free polygons; live per-piece editing while merged; merging
circles/chamfers; piece-aware normalize/snap-resize; line-by-line outline
drawing tool; new goldens (rendering pinned by model/structural tests
instead).

## Testing

- Unit (stdlib-pure, tests/test_areadef.py): envelope math incl. mutation
  sync; merge/split round-trip (geometry, sids, name rules, single undo step
  each); parse/export round-trip incl. malformed-piece drop+warn; CSV rects
  column; CLI --merge/--split incl. refusals (circle in selection, unknown
  id, cross-page) and page-awareness.
- E2E (pytest-qt, tests/e2e/): real-event journey — draw two overlapping
  rects → Ctrl+click both → Ctrl+M → model has one multirect with 2 pieces →
  click in the notch (no selection) → click in a piece (selects) → arrow
  nudge moves whole unit → context-menu Split → two rects again. CLI journey
  covering --merge/--split on-disk JSON. Guard test: Merge with a circle
  selected refuses and changes nothing.
- Contracts: legacy suite stays plain-unittest/zero-non-stdlib-deps; fixtures
  byte-identical; goldens untouched; ruff clean; every behavior change pinned
  in the same commit.
