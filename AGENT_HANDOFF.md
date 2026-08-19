# AutoPlan — Surveyor Feedback Backlog: Agent Handoff

Context for an agent continuing this work. The backlog comes from surveyor
tester feedback (`FEEDBACK_TASKS.md`), 12 tasks ordered easiest → hardest.
Work spans **three repositories** that are cloned side by side:

| Repo | Role | Stack |
|------|------|-------|
| `autoplan-python` | DXF drawing engine | Flask + ezdxf + numpy/scipy/contourpy/shapely |
| `autoplan-api` | Backend (users, persistence, orchestration) | Node/Express + TypeScript + MongoDB + Redis |
| `autoplan-web` | Frontend | Nuxt 3 / Vue 3 + Tailwind |

Data flow: web → api → (api computes legs/areas/embellishment sizes) → POST to
python `${type}/plan` → python returns a DXF/PDF URL.

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
| 8 | Auto-select legible text heights per scale | ⬜ Not started (planned) |
| 9 | Switch coordinate columns in-app (drag & drop) | ✅ Done |
| 10 | Optional bearing/distance & coordinate tables on the plan | ⬜ Not started |
| 11 | DWG file upload (cadastral boundary import) | ⬜ Not started |
| 12 | Handle very large datasets (async/streaming) | ⬜ Not started |

---

## What was changed (this session)

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
- These sizes are recomputed and **overwrite** the stored topo-setting values on every
  coordinate/boundary save (`plan.service.ts` ~lines 213-219 and ~310-323), so the
  static `point_label_scale: 0.2` defaults never reach the plot — the embellishment
  `*_PERCENT` constants are the real knobs.
- Regression test: `autoplan-python/tests/topographic_test.py` asserts spot-height
  entities exist + layer on, toggle hides them, interval label present/absent, and
  interval validation.

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
```
For visual DXF checks, `pip install matplotlib` and render with
`ezdxf.addons.drawing` (matplotlib backend). Key gotcha: drawer `self.scale =
1000 / plan.scale` (1.0 at 1:1000); geometry is in real eastings/northings.

### autoplan-api
`node_modules` was **not installed** in this session, so TS wasn't compiled here.
Run `npm install` then `npm run build` (`tsc -p tsconfig-build.json`) to type-check.
Changes are small and self-contained (enum entries + one constant).

### autoplan-web
```
npm install   # if needed
node_modules/.bin/nuxt build   # full SFC + type compile; used to verify all web changes
```
No `vue-tsc` installed, so `nuxt build` is the compile check. Builds were green (exit 0).

---

## Remaining tasks — plan

### Task 8 — Scale-driven legible text heights (Medium–Hard; python, maybe web)
The `autoplan-api` embellishment system is already a **partial** version of this
(area/scale-independent printed-mm sizing). Recommended: review **all** the
`*_PERCENT`/`*_TARGET_MM` constants in `plan.embellishments.ts` together against the
surveyor's reference ranges (1:500 → bearing/distance 1–1.3 mm, quoted coords 1.5–2 mm,
etc.), and confirm python's per-element heights (`label_size`, `plan_no_height =
label_size*1.3`, etc.) resolve from those. Keep manual override; make scale-driven the default.

### Task 10 — Optional bearing/distance & coordinate tables on the plan (Medium–Hard; all 3)
Data exists (parcel legs carry bearing/distance; beacon coords in payload). Add a
table-drawing primitive to `dxf_manager.py`; add `show_bearing_distance_table` /
`show_coordinate_table` booleans to python model + api (interface/model/validation) +
web toggles. Place beside the title block without overlap; size text via Task 8.

### Task 11 — DWG upload (Hard; web + python)
Python already round-trips DXF↔DWG via the ODA File Converter. Add a backend
util/endpoint: DWG→DXF, parse with `ezdxf`, extract closed boundary polyline →
beacon register, feed cadastral pipeline. Web: accept `.dwg`, route to that path
instead of `useSheetParser`. Validate closed ring / units / layer choice.

### Task 12 — Large datasets / async (Hard; api + python + web)
Async job queue (api has Redis) with job id + polling + progress UI; chunked/streamed
CSV parsing (web worker) to bound memory; algorithmic decimation/tiling/spatial index
for the TIN/grid/contour path; documented max size + graceful over-limit message.

---

## Conventions
- Commits go to `main` in each repo (matches existing history).
- Keep the three origin maps (Task 2) in sync when adding origins.
- Coordinate display is **display-only** — never round stored values, payloads, or CSV export.
