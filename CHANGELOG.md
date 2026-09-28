# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Irregular areas: merge 2+ rectangles into one `multirect` shape (`Ctrl+M`,
  *Edit → Merge Selected*, or the right-click menu) and split it back into
  plain rects (*Edit → Split*). A merged area renders as one union outline,
  is hit-tested per piece (the notch of an L is not part of it), moves as one
  unit, and has read-only geometry fields — split it to reshape.
- `multirect` in the export format: `bbox` stays the envelope and a new `rects`
  list carries the exact pieces, in JSON and as a `rects` CSV column.
- CLI: `edit-shape --merge "ID1,ID2[,…]"` and `edit-shape --split --id N`.

### Changed

- Aligning a merged area translates the whole unit (edge/centre ops) and skips
  it for Match Width/Height/Both, reporting how many were skipped; the result
  is clamped back into the canvas at full size.
- Right-click → Delete on a row that's part of a multi-selection now deletes
  the whole selection instead of collapsing to just the clicked row.

## [1.1.0]

The audit follow-up: six phases of fixes over the 1.0 tree, from data-loss
paths to a hygiene sweep.

### Added

- `--version` flag (reads the installed distribution's version, falls back to a
  source marker when running from a checkout).
- CLI parity for the GUI's detectors and editors: `detect-markers`,
  `detect-barcodes`, `detect-high-contrast`, `snap-to-lines`, `align`,
  `add-shape`, `edit-shape`, `list-shapes`, `clear-shapes`, `export-csv`,
  `screenshot`.
- Test suite: e2e CLI journeys and error cases, GUI event/dialog/data-safety
  suites driven by real synthesized input, and visual regression goldens.
- CI: a ruff lint lane plus unit and e2e lanes on Python 3.9 and 3.12.

### Changed

- Detection runs off the UI thread in the interactive GUI; the page detectors
  are vectorized, the derived snap lines are cached per page, and selection
  changes restyle the scene instead of rebuilding it.
- Undo history: one entry per arrow-key nudge burst, and snapshots are taken by
  ownership rather than re-cloned on every push.
- Rendered PDF pages are cached against a ~100 MB byte budget instead of a
  fixed count of five.
- Page inversion is a single lookup-table pass and is cached per page.
- `requirements.txt` is now a one-line shim for `pip install -e .[pdf,markers]`;
  `pyproject.toml` is the single source of dependency truth and carries
  classifiers and project URLs.

### Fixed

- Data loss: every document-replacing action (open, load, close) now confirms
  before discarding unsaved annotations, and a cancelled or failed save is not
  treated as permission to discard.
- Undo and redo mark the document dirty, so save-then-undo no longer closes
  without prompting.
- Opening a raster image cleared the model after the canvas was rebuilt, which
  left the previous document's shapes drawn over the new page.
- Edge-clamped circles were persisted as ellipses whose radius did not match
  their bbox.
- Loading a project into a PDF could give two shapes the same (page, id) after
  out-of-range pages were clamped.
- Saving reset the project file's permissions to owner-only, and the save
  pickers applied their extension after Qt's overwrite check, so an
  extension-less name could silently overwrite an existing file.
- Names arriving from a project file or a decoded barcode are stripped of
  control characters, and CSV cells starting with `=`, `+`, `-` or `@` are
  written as text rather than spreadsheet formulas.

### Removed

- The stale `meta_prompt.md` brief and the `.bak` copies of the app and tests.
- The `template-annotator-gui` (pythonw) entry point, which pointed at the CLI
  dispatcher and could not print.

## [1.0.0]

- Initial release: PyQt6 annotator for rectangles, circles and chamfer
  rectangles over images and PDFs, with JSON/CSV export and a headless CLI.
