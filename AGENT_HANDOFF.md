# AutoPlan — Surveyor Feedback Backlog: Agent Handoff

Context for an agent continuing this work. The backlog comes from surveyor
tester feedback (`FEEDBACK_TASKS.md`), 12 tasks ordered easiest → hardest.
Work spans **three repositories** that are cloned side by side:

| Repo | Role | Stack |
|------|------|-------|
| `autoplan-python` | DXF drawing engine | Flask + ezdxf + numpy/scipy/contourpy/shapely |
| `autoplan-api` | Backend (users, persistence, orchestration) | Node/Express + TypeScript + MongoDB + Redis |
| `autoplan-web` | Frontend | Nuxt 3 / Vue 3 + Tailwind |

Data flow: web → api → (api computes legs/areas, and embellishment sizes for
route/legacy plans only — see Task 8) → POST to python `${type}/plan` → python
returns a DXF/PDF URL.

> Note: `opengeoworks-website` in the workspace is **not** part of this backlog.

---

## Task status

| # | Task | Status |
|---|------|--------|
| 1 | State dropdown (36 states + FCT) | ✅ Done (pre-existing) |
| 2 | Origin with spaces, not underscores | ✅ Done + **UTM zones 31/32/33 added** |
| 3 | Smaller default beacon symbol | ✅ Done (pre-existing) |
| 4 | Drop thousands separators + precision control | ✅ Done |
| 5 | Hectares when area ≥ 1 ha | ✅ Done (pre-existing) |
| 6 | Spot heights "missing" | ✅ Done — was a **sizing** bug (too tiny), fixed + regression test |
| 7 | Contour interval field + label on map | ✅ Done (label + validation added) |
| 8 | Auto-select legible text heights per scale | ✅ Done — **plus a true-coordinate refactor**, see below |
| 9 | Switch coordinate columns in-app (drag & drop) | ✅ Done |
| 10 | Optional bearing/distance & coordinate tables on the plan | ✅ Done — all 3 repos |
| 11 | Legacy DWG import | ✅ Done for **cadastral** — all 3 repos (other plan types deliberately out of scope) |
| 12 | Handle very large datasets (async/streaming) | ✅ Done — all 3 repos (benchmarking outstanding) |

---

## Latest session (2026-08-20) — true coordinates + Task 8

### The problem underneath Task 8

The declared plan scale was **decorative**. `models/plan.py::get_drawing_scale()`
returned `1000/plan.scale` and `dxf_manager` multiplied it into *geometry and
text alike* — a uniform zoom of the whole model that changes no proportion.
The PDF was then rendered fit-to-page, which normalises any uniform zoom. So
1:500 and 1:2500 produced identical sheets differing only in the
`SCALE :- 1 : n` string, and a "1:1000" plan was really plotted at whatever
scale happened to fit (often ~1:1600–1:4000).

Two consequences: the exported DXF/DWG held **inflated coordinates** (at 1:500
a 100 m boundary was 200 units, and a beacon did not sit on its real easting),
and text heights could not be scale-driven because there was no real scale.

### Coordinate truth (`autoplan-python`)

- The uniform multiplier is **gone** from all ~60 sites in `dxf_manager.py`.
  Every coordinate and size passed to the drawer is now true model metres and
  is written to the DXF unchanged. `$INSUNITS=6` (metres) is finally honest.
- `SurveyDXFManager.__init__` takes `mm_to_model` instead of `scale`. It is
  **never** applied to geometry — only to the two DXF settings that are
  absolute model sizes with no caller value: `$PDSIZE` and the greenspace
  hatch pattern spacing. Both used to drift with scale.
- **The frame is now the sheet**: `BasePlan._sheet_frame_coords()` sizes it as
  the printable paper area (paper less the renderer's 20 mm margins) converted
  to model units, centred on the survey. `save_pdf(scale=...)` then renders
  with `fit_page=False`, so the sheet is a genuine 1:n plot — a 60 m leg
  measures 120 / 60 / 24 mm at 1:500 / 1:1000 / 1:2500.
- `plans/route.py` sets `true_scale = False`. A longitudinal profile has
  independent horizontal and vertical scales, so it has no single map scale;
  it keeps the content-fitted sheet **and** the extent-derived sizes. Route
  output is unchanged by this session.

### Task 8 — scale-driven text heights

- `models/plan.py::TEXT_HEIGHTS_MM` — per-element **printed** heights in mm,
  resolved to model units by `PlanProps.text_height()` as `mm * scale/1000`.
  At 1:500 this yields bearing/distance **1.25** and quoted coordinates
  **1.75** model units, matching the surveyor's stated 1.0–1.3 and 1.5–2.0.
  The printed size is now identical at every scale.
- Symbols follow the same rule: `BEACON_SYMBOL_MM` (1.6), `TOPO_POINT_SYMBOL_MM`.
- `BasePlan.height(element, legacy)` is the single resolver. It uses the mm
  table only when `auto_scale_sizes` **and** `true_scale` — a printed size only
  converts to model units on a sheet that has a scale, so fitted sheets (route)
  always take the extent-derived legacy value.
- **Overrides**: `text_heights: {"bearing_distance": 3.0}` per element (mm), or
  `auto_scale_sizes: false` to drive everything from the old
  `font_size`/`label_size`/`footer_size`/`beacon_size` fields.
- **Auto-fit**: when a survey will not fit its sheet at the requested scale,
  `_resolve_scale_for_sheet()` zooms out to the next entry in `STANDARD_SCALES`,
  logs a warning, records `plan.scale_adjusted_from`, and the title block states
  the scale actually drawn. `fit_scale_to_sheet: false` raises instead, with a
  message naming a workable scale. The engine only ever zooms **out**, never in.

### Sheet-layout fixes the true-scale frame exposed

- `bbox.extents()` under-measures MTEXT by a full line once the title wraps,
  which dropped the graphical scale onto the last title line. `draw_title_block`
  now measures with `ezdxf.tools.text_size.mtext_size`, and
  `SurveyDXFManager.measure_mtext()` exposes that to the sheet sizer.
- The title band is **measured, not assumed**: `_title_band_height()` adds up the
  wrapped title, the scale bar stack and the area/origin/notes block, so a
  six-line title reserves more room than a one-line title. Capped at
  `TITLE_HEIGHT_LIMIT` (45%) so a pathological title cannot eat the sheet.
- Scale-bar tick labels were sized as a fraction of the bar (~1.25 mm and
  shrinking); they now take a printed height (`scale_bar`, 1.8 mm).
- The layout land-use schedule assumed a deep bottom margin that a true-scale
  frame does not provide. It now has a reserved band above the footer boxes
  (`LayoutPlan._bottom_band_mm()`), which the auto-fit scale also accounts for.

### `autoplan-api`

- **Bug — hardcoded A4.** `plan.embellishments.ts` sized everything against
  `A4_PRINTABLE_GEOMEAN_MM` while `page_size` is user-selectable (A0–A5, Letter,
  Legal, wired through `StepEmbellishment.vue`). An A3 beacon printed at
  1.07 mm, A0 at 0.35 mm. `printableGeomeanMm(pageSize)` now derives it from the
  chosen sheet; every size is expressed as a `*_TARGET_MM` constant (the four
  remaining magic fractions are now documented at their A4 printed size, so A4
  output is byte-identical).
- **Bug — override clobbering.** `applySizes` overwrote
  `font_size`/`beacon_size`/`label_size`/`footer_size` on *every* coordinate and
  boundary save, so a manual size could never survive. It is now gated on
  `usesExtentSizing(plan)` — route plans, or `auto_scale_sizes === false`. For
  map plans python owns sizing and the api no longer touches these fields.
- New persisted fields: `auto_scale_sizes`, `text_heights`, `fit_scale_to_sheet`
  (interface + mongoose schema + `EditPlanInput`). The whole plan document is
  POSTed to python, so they flow through automatically.
- **Pre-existing test failure fixed.** `tests/fixtures.golden.json` still held
  `point_label_scale: 0.5`, the value from *before* the Task 6 spot-height fix,
  so `npm test` had been failing since that change. Verified it failed
  identically at untouched HEAD, then regenerated — only that one field moved
  (0.5 → 2.4).

### Verified

- `tests/scale_test.py` (new): true coordinates and true distances at 1:500 /
  1:1000 / 1:2500, printed heights constant across scales, the surveyor's
  reference ranges, both override paths, auto-fit + the honest title block, and
  that the frame equals the printable sheet.
- `smoke_test.py`, `topographic_test.py`, `layout_test.py` all pass.
- `autoplan-api`: `tsc -p tsconfig-build.json` clean, `npm test` green.
- All four plan types rendered to PNG and compared against the pre-change
  output. The old cadastral sheet had title text overlapping the parcel and
  unreadable leg labels; the new one is plot-ready.

### Known limitations (deliberate)

- The PDF renders **0.106% under** true scale: PyMuPDF writes the page box in
  whole points (A4 → 595×841 pt instead of 595.276×841.890). Documented in
  `save_pdf`. The DXF/DWG are exact, and the graphical scale bar is rendered in
  the same space, so measuring against the bar cancels the error.
- `ceil1` in `plan.embellishments.ts` rounds to 0.1 model units, which on an A0
  route sheet is ~10% oversize. Left alone to avoid churning the golden fixture
  for a rare case.
- No frontend UI for the per-element `text_heights` overrides — they are
  payload-only. Selecting a scale already drives the heights end to end.

---

## Task 10 — on-sheet schedules (2026-08-20)

A user can now print a **bearing/distance** schedule and/or a **coordinate**
schedule on the sheet, per plan type:

| Plan | Bearing & distance | Coordinates |
|------|--------------------|-------------|
| cadastral | every parcel leg | the beacon register |
| topographic | boundary (perimeter) legs | the boundary beacon register |
| layout | site boundary legs | boundary corners + the plot-corner register |

Route plans have neither -- a longitudinal profile carries no parcel or
boundary schedule -- so the option is hidden rather than shown doing nothing.

### Placement: a reserved band, not leftover space

The schedules take a band down the **right of the sheet**, between the title
stack and the footer boxes. `BasePlan._sheet_frame_coords()` narrows the
drawing area by exactly that width before centring the survey, and the
auto-fit scale accounts for it, so a table can never land on the drawing.
`_drawing_area()` gives the region inside the band, which is what the
north-arrow grid ticks anchor to.

- `models/plan.py` — `show_bearing_distance_table` / `show_coordinate_table`
  (both default `False`), plus the table metrics (`TABLE_ROW_SPACING`,
  `TABLE_CELL_PADDING`, `TABLE_GAP_MM`, decimals).
- `plans/base.py` — `TableSpec` / `ColumnRows` / `TableColumn`, the row
  builders (`_leg_rows`, `_coordinate_rows`), the flow/measure/cap pipeline
  (`_flow_tables`, `_measure_column`, `_table_columns`, `_table_band_mm`) and
  `draw_tables()`. Plan types supply content by overriding
  `_bearing_distance_table()` / `_coordinate_table()`.
- `dxf_manager.py` — `draw_table` gained `span_rows` (a title row runs the
  full width, with the column dividers broken around it) and a new
  `text_width()` helper; schedules draw on a new `TABLES` layer.

### Things worth knowing

- **Cell widths come from real font metrics** (`SurveyDXFManager.text_width`),
  not a per-character guess. The first cut used a character estimate and
  produced a band half the sheet wide.
- **Rows flow continuously.** A schedule fills what is left of the current
  column before starting a new one, and a split schedule repeats its title
  (marked `(CONT.)`) and headings. Splitting each table into fixed blocks
  first wasted a whole column on a four-leg bearing schedule.
- **The band is capped** at `TABLE_BAND_LIMIT` (40%) of the sheet width. When
  a register overruns that, every requested schedule still appears -- the row
  budget is shared between them (`_share_rows`) and a schedule that had to be
  cut says so in its title, e.g. `COORDINATES (FIRST 23 OF 96)`. Dropping a
  schedule the user switched on is the one outcome that leaves the sheet
  quietly wrong, so the cap never does that. A bigger sheet gets more rows:
  the 96-point register truncates on A4 and fits whole on A3.
- **Layout specs are memoised.** A layout generates its plots during `draw()`,
  after the sheet has been sized, so `_table_specs()` caches on first use to
  keep the reserved band and the drawn table in agreement. The full generated
  register always goes out in `setting_out_coordinates.csv`.
- Fixed in passing: `LayoutPlan.save()` overrode `BasePlan.save()` without
  passing `scale`, so layout PDFs were still fit-to-page rather than plotted
  at their true scale.

### API and web

- `autoplan-api` — both booleans added to `plan.interface.ts` (+ `EditPlanInput`),
  the mongoose schema, and `validateEditPlan`. The whole plan document is
  POSTed to python, so they flow through with no extra plumbing.
- `autoplan-web` — a **Plan Tables** section in
  `StepEmbellishment.vue` with a checkbox each, hidden for route plans via a
  new `planType` prop; the hint text under each checkbox names what that plan
  type will list. Wired through `edit.vue` (default state, prefill from the
  API, and the save payload).

### Tests

`tests/tables_test.py` — toggles (nothing drawn unless asked; each works
alone), values (the legs the drawing labels, the register's coordinates, and
no thousands separator), pagination, and a **programmatic no-overlap check**:
the drawn `TABLES` entities are asserted to share no area with the drawing,
the drawing labels, the title block or the footer boxes, and to stay inside
the frame — for all three plan types.

---

## Earlier session — Tasks 2, 4, 6, 7, 9

### Task 2 — UTM zones (all three repos)
`origin` is a **display-only label** printed in the title block — there is **no
reprojection** anywhere. Added `utm_zone_32` / `utm_zone_33` (Nigeria spans UTM
31N/32N/33N):
- `autoplan-python/models/plan.py` — `PlanOrigin` enum + `PLAN_ORIGIN_DISPLAY_NAMES`
- `autoplan-api/src/modules/plan/plan.interface.ts` — enum + `PLAN_ORIGIN_LABELS`
- `autoplan-web/app/utils/planOrigins.ts` — `PLAN_ORIGINS`
Each file's comment says "add it here and to the two maps above" — keep the three in sync.

### Task 7 — Contour interval label + validation (`autoplan-python` + `autoplan-web`)
- `dxf_manager.py::draw_title_block` gained an optional `notes: list` rendered below area/origin.
- `plans/base.py` — added `_title_block_notes()` hook (empty by default), passed into `draw_title_block`.
- `plans/topographic.py` — overrides `_title_block_notes()` to emit `CONTOUR INTERVAL :- <n> M`, only when contours are drawn (`show_contours and (tin or grid)`), `:g` formatting.
- `models/plan.py::TopographicSettingProps` — `model_validator` rejects `contour_interval <= 0` / `major_contour <= 0` when contours are shown/generated.
- `autoplan-web/.../StepTopoSettings.vue` — inline validation (`> 0`) mirroring the backend, blocks save, clears live.

### Task 6 — Spot heights were TINY, not missing (`autoplan-api` + regression test in python)
Root cause: text/symbol sizes are **auto-computed from drawing area** in
`autoplan-api/src/modules/plan/plan.embellishments.ts`, sized so they print at a
fixed millimetre size regardless of scale (`printed_mm ≈ PERCENT × 209`, A4
printable geomean). `POINT_LABEL_SCALE_PERCENT` was `0.0014` → **~0.29 mm** (beacon
is 1.6 mm, contour labels ~1.0 mm). Fixed to a documented target:
`POINT_LABEL_TARGET_MM = 1.5` → `PERCENT ≈ 0.00718`.
- Regression test: `autoplan-python/tests/topographic_test.py` asserts spot-height
  entities exist + layer on, toggle hides them, interval label present/absent, and
  interval validation.

> Superseded in part by the latest session: that `PERCENT × 209` model now only
> governs route/legacy plans, and the hardcoded 209 was a bug on non-A4 sheets.
> Map plans take spot-height size from `TEXT_HEIGHTS_MM["spot_height"]` (1.5 mm),
> so the printed size the Task 6 review settled on is preserved.

### Task 4 — Drop thousands separators + precision control (`autoplan-web`)
- New `app/utils/formatCoordinate.ts` — no grouping ever; modes `raw` / `dp:0..4` / `sf:3..6`
  (sig-figs rendered as plain decimals, not exponential); `localStorage` persistence.
- `app/pages/project/[id]/plan/[plan]/index.vue` — replaced the `Intl.NumberFormat`
  formatter (the app's only grouping source) and added a **Precision** dropdown by the
  Coordinate Table header. Display-only: CSV export and the payload keep full precision.

### Task 9 — In-app column mapping, drag & drop (`autoplan-web`)
- New `app/utils/columnMapping.ts` — `detectColumns` (header sniff), `autoDetectMapping`
  (match ID/Northing/Easting/Elevation by header name, positional fallback), `applyMapping`,
  and `sessionStorage` persistence keyed by a file "signature".
- New `app/components/CoordinateColumnMapper.vue` — modal with a **drag-and-drop** tray →
  labeled field slots (also **tap-to-place** for touch/keyboard/a11y), live preview,
  required-field validation.
- Wired into `StepCoordinates.vue` (ID/N/E) and `StepTopoPointsTable.vue` (ID/N/E/**Elevation
  required** — this hardens Task 6: elevation can't silently land as 0). Removed the old
  fixed-position parsing + dead header-sniff helpers.

---

## How to build / test

### autoplan-python
Needs Python ≥ 3.11 (numpy 2.x). Deps aren't vendored — create a venv:
```
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tests/smoke_test.py           # all 4 plan types generate
.venv/bin/python tests/topographic_test.py     # Task 6/7 regression
.venv/bin/python tests/layout_test.py          # layout generate + draw
.venv/bin/python tests/scale_test.py           # true coordinates + Task 8
.venv/bin/python tests/tables_test.py          # Task 10 schedules + no-overlap

# Task 11 needs the ODA File Converter, which lives in the image:
docker compose run --rm engine python tests/cad_import_test.py
```
For visual checks, render the PDF to PNG with PyMuPDF (already a dependency)
rather than installing matplotlib — `plan._drawer.save_pdf(path, paper_size=...,
orientation=..., scale=plan.plot_scale_mm_per_unit if plan.true_scale else None)`
then `fitz.open(path)[0].get_pixmap(dpi=110).save(...)`.

Key facts: geometry is in **true eastings/northings** at 1:1 — there is no
coordinate multiplier any more. Annotation is sized from printed millimetres via
`plan.mm_to_model` (`scale/1000`). The frame equals the printable sheet, so the
PDF is a real 1:n plot.

### autoplan-api
```
npm install
npx tsc -p tsconfig-build.json --noEmit   # type-check
npm test                                   # golden-fixture computations
```
Both were run and are green. `npm test --update` (via
`npx ts-node -r tsconfig-paths/register tests/computations.test.ts --update`)
re-captures the golden fixtures — check the diff before committing it.

### autoplan-web
```
npm install   # if needed
node_modules/.bin/nuxt build   # full SFC + type compile; used to verify all web changes
```
No `vue-tsc` installed, so `nuxt build` is the compile check. Builds were green (exit 0).
Untouched this session.

---

## Remaining tasks — plan

### Task 11 — Legacy DWG import — **cadastral path shipped 2026-08-20**

Built to the revised `FEEDBACK_TASKS.md` §11. A surveyor uploads a legacy DWG
(or DXF) and gets a cadastral plan without ever producing a spreadsheet.

**Flow:** web `StepCoordinates` → `POST /plan/cad/inspect` (api) → `POST
/cad/inspect` (engine) → the import screen → the existing coordinate table.

- `autoplan-python/models/cad.py` — the neutral intermediate: `CadRing`,
  `CadPoint`, `CadLabel`, `CadLayer`, `CadStation`, `CadInspection`, plus the
  `$INSUNITS` → metres table.
- `autoplan-python/cad_import.py` — `inspect_drawing()` (load, flatten,
  extract, report) and `ring_to_coordinates()`.
- `autoplan-python/app.py` — `POST /cad/inspect`, multipart, 32 MB cap.
- `autoplan-api` — `plan.route.ts` uses `express.raw({type:
  'multipart/form-data'})` and forwards the body untouched. **No multipart
  dependency was added**: the API is a proxy here, and its job is the two
  things it owns, authentication and the size limit. The global
  `express.json()`/`urlencoded()` do not match multipart, so the body reaches
  the raw handler intact.
- `autoplan-web` — `CadImportModal.vue` (units confirmation → boundary choice →
  coordinate preview), `CadRingPreview.vue` (SVG thumbnail of each candidate),
  `utils/cadImport.ts` (types and formatting only).

**Decisions worth keeping**

- **Recompute, don't parse.** No bearing/distance text is ever read. The import
  produces an ordered ring only; `backComputation` derives legs, bearings,
  distances and area, exactly as for a typed-in plan.
- **The register is computed server-side, for every ring, in the inspect
  response.** The browser renders `ring.coordinates` rather than rebuilding it
  — station naming, ordering and de-duplication are import rules and one
  implementation of them is enough. `tests/cad_import.e2e.ts` asserts the exact
  field contract the UI reads, so an engine-side rename cannot break the
  browser silently.
- **Ring order follows the drawing, not a convention.** When the sheet carries
  station ids the ring starts at the lowest and runs whichever way keeps them
  ascending, so a restored plan reproduces the original traverse. Only an
  unlabelled ring falls back to clockwise-from-south-west. The first corner is
  not cosmetic — the plan anchors its north arrow and quoted coordinates to it.
- **Repeated blocks are symbols, not geometry.** A block inserted three or more
  times is furniture, so its contents are excluded from ring candidates.
  Without this, four beacon markers contributed four candidate "parcels" and
  buried the real boundary.
- **Loop search is bounded.** `edgeminer.find_all_loops` is O(n!); it runs per
  layer, only under `MAX_EDGES_FOR_LOOP_SEARCH`, and under a timeout. Layers
  too busy to search say so in `warnings` rather than hanging.
- **Cell/label matching uses real font metrics and text height**, not
  per-character guesses.

**Gotchas**

- ODA lives only in the container. `docker compose run --rm engine python
  tests/cad_import_test.py`; on the host the suite exits 2 and says so.
- `docker compose up` does **not** hot-reload — gunicorn holds the old module.
  `docker compose restart engine` after changing engine code. This bit during
  development: the API contract test failed against a stale server.
- Host port 8080 often clashes; `ENGINE_PORT=8081 docker compose up`.
- The image must be `linux/amd64` (the ODA AppImage is x86-64 and is executed
  during the build); it runs under emulation on Apple Silicon.

**Tests**

- `autoplan-python/tests/cad_import_test.py` — 13 checks, every one round-tripped
  through a real DWG: closed polyline, loose lines that miss closure, nested
  blocks, feet, missing units, symbol filtering, ring ordering, drawing report,
  error messages, and DWG → cadastral plan end to end.
- `autoplan-api/tests/cad_import.e2e.ts` — the real route wiring against a
  running engine: body forwarded intact, ring selection, units override, engine
  errors surfaced, and the UI field contract.

**Scope: cadastral only, by decision.** Import stops at a closed boundary plus
its beacon register, and that is where it stays for now — not an unfinished
edge. Extending it is a real piece of work with a real reason to wait: the
messiness of genuine legacy drawings is the specification, and we have not yet
seen any beyond the synthetic fixtures.

The extractor is plan-type agnostic, so what each remaining type needs is
mostly interpretation over the same intermediate — except route, which is not.

| Plan type | What it would take | Size |
|-----------|--------------------|------|
| Topographic | Map a chosen ring to `topographic_boundary`; spot heights come from `points`. The wrinkle: old drawings often place the point at Z=0 and put the height in a text label beside it, so elevation comes from `CadPoint.label` (already populated by the label matcher) rather than the Z coordinate. Decide per drawing which to trust — a warning when Z is uniformly 0 but labels parse as numbers would catch it. | Small |
| Layout | Map the site ring to `layout_boundary`; plot corners come from `points`. Recovering *individual plot polygons* means selecting many rings rather than one, so the choose-a-boundary step has to become multi-select — a UI change more than an engine change. | Medium |
| Route | The real gap, and two of them. (1) A centreline is an **open** chain; `_rings_from_loose_edges` calls `find_all_loops` and discards everything that does not close, so the intermediate needs open chains adding — `edgeminer.find_all_open_chains` / `find_all_simple_chains` exist and are cheaper than loop finding. (2) **Elevations cannot be recomputed.** Bearings and distances fall out of plan geometry; levels do not. Unless the centreline carries real Z values, they have to be read out of the profile's chainage/level table as text — matching text to grid columns, `12+500` chainage notation, thirty-year-old fonts — which is precisely the fragile parsing the whole design avoids. **Check a real route DWG for Z values on the centreline before committing to that**; if they are there, the problem collapses to (1). | Large |

Whoever picks this up: get real legacy drawings from testers first. The
fixtures reproduce the kinds of mess that were anticipated, not the mess that
exists, and that gap is the main risk in every row above.

### Task 12 — Large datasets — **shipped 2026-08-21**

The reported failure was a ~1M-row topographic CSV that never finished. Two
parts of the pipeline actually *failed* rather than being slow, and a third did
work that could never be seen. All three are fixed.

**Hard failures removed**

- `plan.embellishments.ts` computed extents with `Math.max(...eastings)`.
  Spreading an array passes every element as an argument: fine at 100k,
  `RangeError` by 500k. Replaced with a single-pass `range()`.
- Coordinates lived inside the plan document, capping a survey at ~200,000
  points before MongoDB refused the write. They now live in `plan_points`,
  bucketed ~1000 per document.

**The organising idea.** Separate the survey dataset (large, stored once, never
in the plan document) from the drawing input (small, derived, sized to the
plotting scale). An A4 sheet holds ~1,800 legible spot-height labels, so a
million points is 2M DXF entities rendering an unreadable smear.

**Pipeline**

| Stage | What changed |
|-------|--------------|
| Upload | Delimited files over 2,000 rows stream to the API as the raw request body and are parsed there (`coordinate-parser.ts` / `coordinate-stream.ts`). XLSX stays client-side — it is a zip of XML and cannot be streamed. |
| Storage | Bucketed `plan_points`; the plan keeps a 200-point preview, a count, an extent and a recorded `size`. |
| API → engine | Inline plans: NDJSON streamed in the request body. Background jobs: NDJSON written to object storage, engine fetches by URL. |
| Engine | Points thinned **as they are read**, at one printed millimetre of ground. Retained points are bounded by *sheet area*, not file size — ~44,000 cells on A4 regardless of input. |

**Background jobs.** Surveys at or above `ASYNC_POINT_THRESHOLD` (default
25,000 points) return `202` with a job id instead of blocking. `src/worker.ts`
(`npm run worker`) pops from a Redis list and runs `runPlanJob`; the client
polls `GET /plan/job/:job_id`.

Progress is written by **both** services into the same Redis hash — that is why
the job id travels with the payload. The engine cannot report back up the HTTP
call, because the API is blocked waiting for exactly that response. A real
trace:

```
exporting points 8% -> 19% -> 39% -> sending to the drawing engine 40%
-> reading survey points 40% -> exporting DXF, DWG and PDF 84% -> complete 100%
```

Plain Redis list and hash rather than BullMQ: the surface is "one worker pops
the next id and reports progress", Redis was already running, and an unused
scheduler/retry/dashboard is only cost.

**Decisions worth keeping**

- **Decimation is scale-aware, and verified not to change the drawing.**
  Contour length moved 0.44% (2,963 m vs 2,976 m) with 10x finer thinning.
- **Spot heights are thinned separately** to a true minimum separation, and the
  sheet states `SPOT HEIGHTS SHOWN :- 144 OF 1,000,000`.
- **A preview can never overwrite a survey.** `editCoordinates` refuses a save
  carrying fewer points than are stored; the browser only ever holds 200.
- **Cadastral legs read the full register**, not the preview — computing a
  parcel from whichever corners fell in the first 200 points would put a wrong
  area on a legal document.
- `size` (document + points bytes) is measured with `$bsonSize` server-side.

**Gotchas**

- The engine needs `REDIS_URI` to report progress; compose points it at
  `host.docker.internal`. Without it the engine still draws, and simply reports
  nothing.
- `CLOUDINARY_URL` must be set on the **API** as well as the engine — background
  jobs hand points over by reference. Missing, large plans refuse with a clear
  message.
- Two bugs here were invisible to `tsc` and only a live database exposed them:
  fields declared on `IPlan` but not on the mongoose schema are silently
  dropped, and a `.replace()` without an assertion can no-op — `recordPlanSize`
  was defined but never called.

**Measured:** 1,000,000 points -> 40,000 kept -> 144 spot heights, 1.4 MB DXF,
38 s. 200k streamed Node -> Flask -> drawn -> uploaded in 13.9 s. A 40,000-point
background job completes in ~12 s.

**Limits (enforcement done, number provisional).** `MAX_SURVEY_POINTS`
(default 2,000,000) and `MAX_UPLOAD_BYTES` (default 256 MB), both env-tunable
and enforced in `uploadCoordinates`:

- an oversized `Content-Length` is refused before a byte is read;
- the stream enforces the same ceilings for clients that send no length;
- exceeding a limit **refuses** the file rather than truncating it — a plan
  drawn from part of a survey, with nothing to say so, is worse than a
  refusal — and `CoordinateLimitError` is mapped to a `413` so the message
  written for the user actually reaches them, instead of becoming a generic
  500;
- a refused upload clears whatever it had written, so no partial survey is
  left behind.

**Still open:** the *number* is provisional. It comes from the synthetic
benchmark (a uniform grid), not from real GNSS or LiDAR files, which cluster,
duplicate and carry outliers in ways that change the cost of triangulation and
of the decimator's memory. Run two or three real files, measure, and revise
`MAX_SURVEY_POINTS` — then state it in the README as a supported ceiling.

**Migration.** `npm run migrate:points` moves existing plans into the point
store: verify-then-truncate, idempotent, `--dry-run`, `--rollback`, `--plan <id>`.

**Tests**

- `autoplan-api`: `test:parser` (30 checks), `test:points`, `test:migrate`,
  `test:async` (needs Mongo + Redis + engine + `CLOUDINARY_URL`).
- `autoplan-python`: `tests/large_dataset_test.py` — a real million-point run.

## Conventions
- Commits go to `main` in each repo (matches existing history).
- Keep the three origin maps (Task 2) in sync when adding origins.
- Coordinate display is **display-only** — never round stored values, payloads, or CSV export.
- **Geometry is always true ground coordinates.** Never scale coordinates on the
  way into `dxf_manager`; if something needs to change size with the plot scale
  it is annotation, and it belongs in `TEXT_HEIGHTS_MM` as a printed millimetre
  size resolved through `BasePlan.height()`.
- Anything sized in model units that a reader is meant to *see* (text, symbols,
  offsets, table cells, hatch spacing) should be derived from millimetres. A bare
  constant in metres will silently change printed size with the scale.
- Sheet furniture that always draws (title stack, schedules) must **reserve** its
  band up front so the auto-fit scale accounts for it, rather than being squeezed
  into whatever space is left.
- Measure text with `SurveyDXFManager.text_width()` / `measure_mtext()` when
  sizing anything around it. Per-character estimates are what produced both the
  title/scale-bar collision and an oversized schedule band.
- A feature the user switched on must appear. When something does not fit,
  shrink it and say so on the sheet (`(FIRST n OF m)`) rather than dropping it.
