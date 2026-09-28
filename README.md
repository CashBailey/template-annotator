# Template Annotator (PyQt6)

A desktop/CLI tool for drawing named template regions (rectangles, circles, chamfer rectangles) on
images or PDFs, then exporting the regions as JSON/CSV for downstream document-processing workflows.

The app supports multi-page PDFs in the GUI and now includes CLI utilities so you can run most tasks
without opening the graphical interface.

## Features

- Annotate with:
  - Rectangle
  - Circle
  - Chamfer Rect
- Per-shape fields:
  - Name
  - ID
  - Kind
  - Bounding box (`bbox`)
- **Line detection & snapping** — detect the document's rules/underlines (`L`)
  and snap field edges to them while drawing, moving, or resizing, or align
  existing fields in one click
- **Checkbox detection** — find check-mark boxes (`B`) and add a field for each,
  empty or checked, without mistaking letters for boxes
- **Text-region detection** — box each string of characters (`X`) to mark where
  printed text is, so the gaps between boxes demarcate where fill-in fields go
  (now keeps small punctuation like `:` `.` `,` `'` `-` as part of the string)
- **High-contrast detection** — find solid bars / white-on-black headers (`G`);
  their edges become snap targets (no shapes created)
- **Marker detection** — locate QR codes and AprilTag/ArUco fiducials (`M`), one
  field per marker, named by the decoded payload (optional ensemble backends)
- **Barcode detection** — locate 1D barcodes (`K`), one field per barcode
- **Multi-select + Adobe-style align** — Ctrl+click (or Shift-drag marquee) to
  select several shapes, then align edges/centres or match width/height/both to
  the last-selected **key object**
- **Irregular areas** — merge overlapping boxes into one L/T-shaped region
  (`Ctrl+M`), Split to reshape; exports the envelope `bbox` **and** the exact
  `rects` pieces
- **Numeric bbox editing** — type exact X1/Y1/X2/Y2 for the selected shape
- **Keyboard nudging** — arrow keys (Shift = 10 px) to fine-position a shape
- **Right-click context menu** — Rename / Duplicate / Merge / Split / Zoom /
  Delete (canvas or list)
- **Zoom to selection** (`Z`); Ctrl+wheel and Zoom In/Out zoom at the cursor
- Undo/Redo history
- Duplicate, delete, and multi-page navigation for PDFs (lazy page rendering)
- Normalize tool (`N`) to align rectangles, circles, and chamfer rects into
  columns (horizontal column alignment only — never repositions vertically)
- Light/dark UI themes — dark mode shows the document as a negative by default
  (matching the dark chrome), with a View-menu toggle to override
- Window title shows the open file, current page, and unsaved-changes (`*`) state
- Export/import JSON template payloads (atomic, crash-safe writes)
- Export CSV (covers every PDF page, includes a `page` column)
- CLI tools:
  - `info` (metadata, incl. rendered pixel size)
  - `render` (PDF -> PNG)
  - `normalize-json` (batch normalize bbox geometry)

## Requirements

- Python 3.9+
- [PyQt6](https://pypi.org/project/PyQt6/)
- [Pillow](https://pypi.org/project/Pillow/)
- Optional (PDF CLI/GUI support): [PyMuPDF](https://pypi.org/project/PyMuPDF/)
- Optional (QR / AprilTag / barcode detection): `opencv-contrib-python`,
  `pyzbar`, `pupil-apriltags`. Each backend is optional at runtime — a missing
  one just narrows coverage. `pyzbar` also needs the system library `libzbar0`
  (e.g. `sudo apt install libzbar0`).

Install dependencies:

```bash
pip install -r requirements.txt      # == pip install -e .[pdf,markers]
# or pick the extras yourself (a `template-annotator` entry point either way):
pip install -e .[pdf]
# with marker/barcode detection too:
pip install -e .[pdf,markers]
```

## Run the GUI

From repository root:

```bash
python areaDef.py --image /path/to/template.png
# or
python areaDef.py --image /path/to/document.pdf
```

### GUI notes

- Use **N** to run column-based normalization of rectangle zones.
- Use previous/next buttons (or Alt+Left/Alt+Right) for PDF pages.
- Use keyboard shortcuts shown in-app (`F1`).

### Snapping fields to document lines

Forms are full of rules and underlines that already mark where fields belong.
The annotator can detect them and use them to **place, size, and align** fields:

1. Open the document and press **L** (or *Document lines → Detect Lines*). The
   detected rules are drawn as dashed guides over the page.
2. Leave **Snap fields to lines** enabled (default). Now when you draw, move, or
   resize a field, its edges snap to nearby detected lines — the bottom of a box
   locks onto an underline, sides lock onto vertical rules, etc.
3. To align fields you already placed, click **Snap fields to lines now** (aligns
   the selected field, or every field on the page if none is selected).

Detection works on the rendered page bitmap, so it handles vector PDFs, scanned
PDFs, and plain images alike (no extra dependencies). It distinguishes real
rules from text by requiring each line to be mostly-solid ink (not bridged gaps
between letters) and thin (not the tall ink band of glyphs or headings), so
paragraphs and labels are not mistaken for lines. Toggle **Show line guides** to
hide/show the overlay.

**Text regions are snap boundaries too.** If you have also run **Detect Text**
(`X`), the green text-region boxes act like lines when snapping: a field edge
snaps to a nearby text-region edge just as it would to a rule, and — because a
fill-in field marks a *blank*, not printed text — snapping also **clips the field
out of any text region** so it never shares area with printed text. A field drawn
sloppily over a label is shortened to start where the label ends (its height is
kept); a field that merely grazes a line of text above or below is trimmed by that
small overlap. "Snap fields to lines now" works with lines, text regions, or both.

### Detecting check-mark boxes

Press **B** (or *Detection → Detect Checkboxes*) to find the page's check-mark
boxes and add a `checkbox_N` rect field for each. It finds connected ink shapes
that are small, square, mostly hollow, and have four complete straight borders —
so empty *and* checked boxes are found while letters with loops (B, D, O, 8) and
filled blocks are not. Boxes already covered by an existing field are skipped, so
re-running is safe, and the whole batch is a single undo step.

### Detecting text regions

Press **X** (or *Detection → Detect Text*) to box each string of characters,
marking where printed text is. It groups ink components into text lines, then
splits each line into strings wherever the horizontal gap is wider than a normal
space — so labels/words stay together but the wide blanks that mark fill-in
fields break the run. The boxes draw as a toggleable overlay (*Show text
regions*); the **gaps between them indicate where text fields belong**. Use
*Edit → Add Text Regions as Fields* to turn the boxes into `text_N` rect fields.
Once text is detected, the boxes also act as snap boundaries when you snap fields
to lines: fields snap to text-region edges and are kept from overlapping printed
text (see *Snapping fields to document lines* above).

### Detecting high-contrast regions

Press **G** (or *Detection → Detect High-Contrast*) to find solid high-contrast
blocks — filled bars and white-on-black headers like a `DESCRIPTION | Qty | Rate
| TOTAL` row. These are the inverse of a checkbox: a big, mostly-**filled** ink
component rather than a thin rule or hollow box, so thin rules and ordinary
(sparse) text are ignored. No shapes are created; instead the block **edges
become snap targets**, so field edges snap to them (with *Snap fields to lines*)
exactly as they do to rules. Toggle **Show high-contrast regions** to hide/show
the dashed overlay.

When a field edge snaps to a thick rule or block it now lands on the line's
**outer edge on the field's side** — the box hugs the rule without covering the
ink, instead of centring on it.

### Detecting markers and barcodes

Press **M** (*Detect Markers*) to locate QR codes and AprilTag/ArUco fiducials,
or **K** (*Detect Barcodes*) for 1D barcodes. Each adds one rect field per code,
named by the decoded payload (e.g. `qr_https://...`, `apriltag_tag36h11:7`,
`barcode_012345678905`); re-running is idempotent. Detection runs an **ensemble**
of whatever optional backends are installed (OpenCV QR/ArUco/barcode, ZBar via
pyzbar, pupil-apriltags) and merges overlapping hits, preferring a decoded value.
If none are installed the action explains what to `pip install`.

### Selecting multiple shapes and aligning (Adobe-style)

- **Ctrl+click** a shape to add/remove it from the selection. The most recently
  clicked shape is the **key object** (anchor) and keeps its resize handles.
- **Shift+drag** an empty area to marquee-select every shape the rubber-band
  touches.
- Ctrl/Shift-click rows in the **Areas** list for the same multi-selection.

With 2+ shapes selected, the **Align (to key object)** panel (or *Edit → Align*)
aligns the others to the key: **Left / Right / Top / Bottom**, **Center H /
Center V**, and **Match Width / Height / Both**. The key never moves; each align
is a single undo step.

### Irregular areas (merge / split)

Not every field is a rectangle. Select 2+ rectangles and press **Ctrl+M**
(*Edit → Merge Selected*, or the right-click menu) to fuse them into one named
**merged area** — an L, a T, or any union of boxes. It behaves as a single
shape: one name and id, one entry in the Areas list, one undo step, and clicks
land only on the pieces, so the notch of an L is not part of it. Move it with
the mouse or the arrow keys and every piece travels together.

A merged area has no resize handles and its X1/Y1/X2/Y2 fields are read-only,
because its box is derived from the pieces. To reshape it, **Split** it (*Edit →
Split* or the right-click menu) back into plain rectangles named `<name>_1`,
`<name>_2`, …, adjust those, and merge again.

Exports carry both views: `bbox` is the envelope (so every existing consumer
keeps working) and `rects` lists the exact pieces. The CSV gains a `rects`
column holding the same list as JSON.

## CLI usage

Every GUI operation is scriptable headlessly. The central artifact is the
**project JSON** (image metadata + shapes — the same file the GUI saves); detection
commands produce or extend it, and editing/geometry commands transform it. All
commands print to stdout unless `-o/--output` is given, and `--page N` selects a
PDF page (0-based).

```
info               metadata for an image/PDF (incl. rendered pixel size)
render             render PDF pages to PNG
new-project        create an empty project for an image/PDF page
detect-lines       detect rules/underlines -> line coordinates JSON
detect-checkboxes  detect check-mark boxes -> project of rect fields
detect-text        detect text-string boxes -> project of rect fields
detect-markers     detect QR codes / AprilTag markers -> project of rect fields
detect-barcodes    detect 1D barcodes -> project of rect fields
detect-high-contrast  detect solid bars / inverted headers -> region coordinates
snap-to-lines      snap a project's shapes to lines + text regions (--high-contrast adds bar/header edges)
align              align/resize shapes to a key shape (--op, --key, --ids)
normalize-json     normalize shape geometry (--all-kinds for circles/chamfers)
add-shape          add a rect/circle/chamfer to a project
list-shapes        print a project's shapes
edit-shape         rename / move / resize / duplicate / delete a shape by id
clear-shapes       remove all shapes (or all on one page)
export-csv         export a project to CSV
screenshot         render a project's annotated page to PNG (as seen on the canvas)
gui                launch the graphical interface (default)
```

Every GUI capability now has a CLI path, so an agent can do anything a user can.
The **screenshot** command renders the annotated page headlessly (offscreen Qt),
so you can *see* the result of a scripted pipeline:

```bash
# build a project, auto-detect fields, then look at it
python areaDef.py new-project doc.pdf --page 0 -o proj.json
python areaDef.py detect-text doc.pdf --page 0 --into proj.json -o proj.json
python areaDef.py align proj.json --op left --key 1 -o proj.json
python areaDef.py screenshot proj.json -o annotated.png          # view annotated.png
python areaDef.py screenshot proj.json --window -o window.png    # whole window
```

In the GUI, **File → Save Screenshot…** (Ctrl+Shift+P) saves the same picture.

### Examples

Print media info / render pages:

```bash
python areaDef.py info /path/to/file
python areaDef.py render doc.pdf --out-dir ./pdf_previews --scale 2.0
```

Build a project from a PDF page entirely from the CLI:

```bash
# start an empty project for page 5, then auto-detect fields into it
python areaDef.py new-project doc.pdf --page 4 -o proj.json
python areaDef.py detect-checkboxes doc.pdf --page 4 --into proj.json -o proj.json
python areaDef.py detect-text       doc.pdf --page 4 --into proj.json -o proj.json

# add a field by hand, align everything to the document's rules, inspect, export
python areaDef.py add-shape proj.json --kind rect --name "Driver Name" --bbox 395 250 1434 349 --page 4 -o proj.json
python areaDef.py snap-to-lines proj.json --source doc.pdf --page 4 -o proj.json
python areaDef.py list-shapes proj.json
python areaDef.py export-csv proj.json -o fields.csv
```

On a PDF, `snap-to-lines` only moves shapes on the page it detected lines from
(`--page`), so snapping one page of a multi-page project leaves the others
exactly as they were. A raster source has no pages and snaps every shape.

Edit shapes by id:

```bash
python areaDef.py edit-shape proj.json --id 3 --name "Date"        -o proj.json
python areaDef.py edit-shape proj.json --id 3 --move 10 -4         -o proj.json
python areaDef.py edit-shape proj.json --id 3 --bbox 0 0 200 60    -o proj.json
python areaDef.py edit-shape proj.json --id 3 --duplicate          -o proj.json
python areaDef.py edit-shape proj.json --id 3 --delete             -o proj.json
```

Merge shapes into one irregular area, and split it back:

```bash
# fuse ids 1 and 2 into a single merged area (the new id is printed)
python areaDef.py edit-shape proj.json --merge "1,2" -o proj.json
# break it apart again: one plain rect per piece, named <name>_1, <name>_2, ...
python areaDef.py edit-shape proj.json --split --id 3 -o proj.json
```

Only rectangles (and already-merged areas) can be merged, and only within one
page; anything else exits 1 with an `Error:` line and writes nothing.

Normalize:

```bash
# rectangles only, with optional size override
python areaDef.py normalize-json input.json -o normalized.json
python areaDef.py normalize-json input.json --image-size-source template.png -o out.json
# all kinds (rect + circle + chamfer), matching the GUI 'N'
python areaDef.py normalize-json proj.json --all-kinds -o normalized.json
```

If `--output` is omitted, output is printed to stdout. Either way the shapes are
rewritten in areaDef's canonical schema (see below): unknown per-shape keys are
not preserved, and malformed shapes are dropped with a warning on stderr.
Unknown top-level keys are kept.

## Export/import format

### JSON schema (typical)

```json
{
  "schema_version": 1,
  "image": {
    "path": "/absolute/path/to/image_or_pdf",
    "width": 1837,
    "height": 2379
  },
  "shapes": [
    {
      "id": 1,
      "name": "Applicant Name",
      "kind": "rect",
      "bbox": [395.2, 250.7, 1434.0, 348.8],
      "page": 0
    }
  ]
}
```

Notes:

- Coordinates are in original pixel space for the source image/page.
- For PDF files, shapes include a `page` field indicating page index.
- `kind` is `rect`, `circle`, `chamfer`, or `multirect`. A `multirect` also
  carries `"rects": [[x1, y1, x2, y2], …]` — its pieces; its `bbox` is their
  envelope. A `multirect` without usable `rects` is dropped on load.

### Coordinate space for PDFs

PDF pages are rasterized at `PDF_RENDER_SCALE` (2×) before annotation, so saved
`bbox` coordinates and the exported `image.width/height` are in that 2×-pixel
space — **not** PDF points. The `info` command prints both the point size and
the rendered pixel size, and `normalize-json --image-size-source <file.pdf>`
reports dimensions in the same 2× space so overrides stay consistent with the
GUI's exported JSON.

## Testing

All tests run headlessly via Qt's offscreen platform (`QT_QPA_PLATFORM=offscreen`,
set automatically).

The legacy suite (`tests/test_areadef.py`) needs nothing beyond the app's own
runtime deps — no test-only install required:

```bash
python -m unittest tests.test_areadef -v
```

The full E2E suite (`tests/e2e/`) is pytest-based (CLI journeys/errors, GUI
events/dialogs, visual regression). Install the `test` extra plus the
optional features it exercises:

```bash
pip install -e .[pdf,markers,test]
```

Fast lane — everything except `e2e`/`gui`/`visual`, run in parallel:

```bash
pytest -m "not e2e and not gui and not visual" -n auto
```

Full E2E suite — run serially (Qt GUI tests are not `xdist`-safe):

```bash
pytest tests/e2e
```

Update the visual-regression goldens after an intentional rendering change:

```bash
pytest tests/e2e/test_visual_regression.py --update-goldens
```

Verify committed fixtures/goldens are still byte-identical to their generator:

```bash
python scripts/gen_fixtures.py --check
```

## Project files

- `areaDef.py` — main app and CLI entrypoint
- `tests/test_areadef.py` — unit/GUI/CLI test suite
- `requirements.txt` — one-line install shim (`-e .[pdf,markers]`)
- `pyproject.toml` — packaging metadata, dependencies, console entry point
- `README.md` — this file
- `LICENSE` — proprietary, all rights reserved

## Notes on CLI + GUI compatibility

- GUI remains default when no subcommand is passed (or with the explicit `gui` subcommand).
- PDF CLI commands require PyMuPDF.
- If image/PDF paths are omitted for normalization, the tool attempts to use JSON `image.width/height` metadata.
