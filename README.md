# Survey Plan Generator

A web service that turns raw field data from engineering and cadastral surveys
into ready-to-use survey plans. Plans are drawn as DXF (via
[ezdxf](https://ezdxf.mozman.at/)), converted to DWG with the
[ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)
so they can be edited in AutoCAD, rendered to PDF, and uploaded as a ZIP bundle.

This service is drawing-only: it validates a plan payload, generates the
drawing, and returns a download URL. User management, projects, and persistence
are handled by a separate API server.

## Supported plan types

| Type | Endpoint | Description |
|------|----------|-------------|
| Cadastral | `POST /cadastral/plan` | Property beacons, parcel boundaries, bearing/distance labels |
| Topographic | `POST /topographic/plan` | Spot heights, site boundary, TIN/grid contours |
| Route | `POST /route/plan` | Plan-and-profile sheet (see below) |
| Layout | `POST /layout/plan` | Estate subdivision schemes (see below) |

One further endpoint reads drawings rather than writing them:

| Endpoint | Description |
|----------|-------------|
| `POST /cad/inspect` | Recover survey data from an uploaded legacy DWG/DXF (see [Importing legacy drawings](#importing-legacy-drawings)) |

### Route plans

Route plans are drawn as the industry-standard **plan-and-profile sheet**:

- **Plan view (horizontal alignment)** — drawn when the payload carries
  station coordinates (`coordinates` whose ids match the `elevations` ids).
  The route is rotated to run left-to-right above the profile, with chainage
  ticks/labels, right-of-way edges (`route_parameters.right_of_way_width`),
  and a north arrow rotated to match.
- **Longitudinal profile** — existing ground level against chainage over a
  station/elevation grid, at the scales in
  `longitudinal_profile_parameters`.

Payloads without station coordinates draw the profile only (backward
compatible).

### Layout plans

Layout plans work in two modes:

- **Draw mode** — the payload provides the plot corner coordinate register
  (`coordinates`), the `plots` (corner ids per plot, with block/number/use),
  and optionally `roads`; the scheme is drawn as given.
- **Generate mode** — only the perimeter (`layout_boundary`) and design
  parameters (`layout_parameters`) are provided. The subdivision is designed
  automatically using the standard Nigerian pattern: a major spine road along
  the site's long axis, cross streets limiting block length, double-loaded
  blocks of frontage x depth plots (default 15 m x 30 m), commercial plots
  along the spine, open-space and facility reservations, and per-block plot
  numbering with a land-use schedule table.

Either way the exported ZIP includes `setting_out_coordinates.csv` — the
coordinates of every boundary beacon, plot corner, and road centerline point,
ready for field setting-out.

Perimeter bearings/distances are computed upstream by the AutoPlan API and
arrive in the payload as `layout_boundary.legs`; when absent, plans are
drawn without the perimeter leg labels.

All endpoints accept a JSON payload described by `models/plan.py`
(`PlanProps`) and respond with:

```json
{ "message": "Cadastral plan generated", "filename": "<plan name>", "url": "<zip url>" }
```

Invalid payloads return `400` with validation details. See
`tests/smoke_test.py` for complete example payloads for every plan type.

## Importing legacy drawings

`POST /cad/inspect` recovers survey data from an existing CAD drawing, for
restoring old plans where the surveyor holds a DWG and nothing else -- no DXF,
no CSV, no coordinate register.

Post the drawing as multipart `file` (DWG or DXF, 32 MB limit). Optional form
fields: `units` (a DXF `$INSUNITS` code, overriding the drawing's own) and
`ring_id` (also return that shape as a coordinate register).

The response reports what the drawing holds rather than guessing: every closed
shape with its layer, area, vertices and a ready-made coordinate register; the
point features and text labels; a per-layer inventory; the detected units; and
the coordinate extent. Choosing which shape is the boundary is left to the
caller.

Two rules shape the extractor:

- **Recompute, don't parse.** Geometry is ground truth; the text on a sheet is
  a derived label. Bearings, distances and areas are never read from the
  drawing's annotation -- they are recomputed from the recovered geometry. Text
  is read only for what geometry cannot supply: station ids, and spot
  elevations where a drawing carries the height as a label.
- **Report, don't guess.** A legacy boundary may be a polyline, an old-style
  polyline, a spline, loose segments that miss closure by millimetres, or any
  of those nested inside blocks. All of it is recovered and offered; geometry
  belonging to a block placed many times is treated as a repeated symbol (a
  beacon marker, a tree) and excluded.

### Scope

Import currently targets **cadastral** plans: a closed boundary plus its
beacon register. The extractor itself is plan-type agnostic, so the other plan
types are interpreters over the same intermediate rather than new parsers:

| Plan type | What it would need |
|-----------|--------------------|
| Topographic | Map a chosen ring to `topographic_boundary`, and read spot heights from `points` -- with the wrinkle that old drawings often place a point at Z=0 and put the height in a text label beside it. `CadPoint.label` already carries that text, so this is mostly interpretation. |
| Layout | Map the site ring to `layout_boundary`; plot corners come from `points`. Recovering individual plot polygons means selecting many rings instead of one, so the choosing step needs to become multi-select. |
| Route | The largest gap. A centreline is an *open* chain, and the extractor keeps only closed rings, so the intermediate needs open chains adding (`edgeminer.find_all_open_chains`). Worse, elevations cannot be recomputed from plan geometry: unless the centreline carries real Z values, they have to be read out of the profile's chainage/level table as text -- exactly the fragile parsing the design otherwise avoids. Worth checking a real route DWG for Z values before committing to that. |

### Testing

The importer needs the ODA File Converter, which lives in the image:

```bash
docker compose run --rm engine python tests/cad_import_test.py
```

The suite builds drawings that look like real legacy sheets -- boundary as a
closed polyline, as loose lines that miss closure, and buried in nested blocks,
each surrounded by title text, a north arrow and annotation -- and round-trips
every one through a real DWG.

## Large surveys

Point series are not sent as one JSON body. A plan whose points live in the
API's point store arrives as **NDJSON** -- the plan on the first line, then one
point per line -- either streamed in the request body or fetched from a URL for
a background job. Points are thinned as they are read, at one printed
millimetre of ground, so the number retained is bounded by the *sheet* rather
than the file: an A4 sheet holds about 44,000 one-millimetre cells whether the
survey has ten thousand points or ten million.

Thinning is invisible at plotting scale. Measured against a ten-times-finer
cell, total major-contour length moved 0.44% (2,963 m vs 2,976 m) with the same
number of contours. Spot heights are thinned separately, to a minimum printed
spacing, and the sheet states how many of the total it is showing.

Set `REDIS_URI` for the engine to report progress into the API's job record
while it reads, draws and exports. Without it the engine still generates
normally and simply reports nothing.

The supported dataset size is enforced by the API (`MAX_SURVEY_POINTS`,
default 2,000,000 points, and `MAX_UPLOAD_BYTES`, default 256 MB). The default
is provisional: it comes from a synthetic uniform grid, not from real GNSS or
LiDAR files, and should be revised once real ones have been measured.

## Project structure

```
app.py            Flask entry point and endpoints
gunicorn.conf.py  Production server settings (timeouts, worker recycling)
dxf_manager.py    Low-level DXF drawing primitives (ezdxf wrapper)
models/plan.py    Pydantic models: the JSON contract for plan payloads
models/cad.py     Models for drawings read back in (rings, points, labels)
cad_import.py     Legacy DWG/DXF import: extract survey data from a drawing
plans/base.py     Shared drawing logic (frame, title block, footers, north arrow)
plans/*.py        One generator per plan type
utils.py          Geometry and HTML→MText helpers
upload.py         Cloudinary upload helper
tests/            Smoke, regression and import tests
```

## Running locally

Requirements: Python 3.11+ and, for DWG output, the ODA File Converter on
your `PATH` (DXF generation and the smoke test work without it).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your Cloudinary URL

# generate sample plans without any credentials
python tests/smoke_test.py out/

# run the API
python app.py
```

## Configuration

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLOUDINARY_URL` | Upload target for generated bundles | required for `save()` |
| `PORT` | HTTP port | `8080` |
| `WEB_CONCURRENCY` | Gunicorn workers | `1` |
| `GUNICORN_TIMEOUT` | Request timeout (seconds) | `300` |
| `GUNICORN_MAX_REQUESTS` | Requests per worker before recycling | `50` |

Worker recycling is deliberate: plan generation allocates large numpy/ezdxf
buffers and CPython rarely returns that memory to the OS, so long-lived
workers slowly grow. Recycling keeps memory bounded on small machines.

## Docker

The Dockerfile installs the ODA File Converter and runs the service under
Gunicorn:

```bash
docker build -t survey-plan-generator .
docker run --env-file .env -p 8080:8080 survey-plan-generator
```

For local work `docker-compose.yml` mounts the source over `/app`, so engine
changes need no rebuild:

```bash
docker compose up                    # serve on http://localhost:8080
docker compose run --rm engine python tests/smoke_test.py
```

Notes for local use:

- The ODA File Converter is an **x86-64** AppImage and is executed during the
  build, so the image must be built for `linux/amd64`; on Apple Silicon it runs
  under emulation. The compose file sets this.
- Port 8080 often clashes with another stack -- `ENGINE_PORT=8081 docker
  compose up`.
- Gunicorn does not hot-reload. After changing engine code, `docker compose
  restart engine`.

## Deployment

Pushes to `main` trigger `.github/workflows/prod.yml`, which:

1. builds the Docker image and pushes it to Docker Hub as
   `<DOCKER_USERNAME>/autoplan-python:latest`, then
2. connects to the production Ubuntu server over SSH and restarts the
   service with Docker Compose (`docker compose pull && up -d`).

The workflow needs these repository secrets: `DOCKER_USERNAME`,
`DOCKER_PASSWORD`, `SERVER_HOST`, `SERVER_USERNAME`, `SERVER_SSH_KEY`,
and `SERVER_PORT`.

## Notes

- Fonts: text styles reference the font by file name (e.g.
  `Times New Roman.ttf`). Install the fonts you use in the runtime
  environment or PDF output falls back to a default font.
- **Geometry is drawn 1:1 in true ground coordinates.** A beacon at easting
  543210 is written to the DXF at x=543210, so the exported CAD file can be
  snapped, measured, and overlaid against other survey data directly. Plot
  scale is applied once, at render time, by `save_pdf(scale=...)`.
- The output sheet is a genuine plot at the requested scale (`scale`, default
  1:1000): the drawing frame is the printable paper area converted to model
  units, and the graphical scale bar labels true ground distances. If the
  survey will not fit the chosen sheet at that scale the plan is drawn at the
  next standard scale that does, and the title block states the scale actually
  used (set `fit_scale_to_sheet: false` to get an error instead).
- Text and symbol sizes are specified as printed millimetres and resolved at
  the plan scale, so selecting a scale yields a legible sheet with no manual
  resizing in CAD. Override per element with `text_heights` (in mm), or set
  `auto_scale_sizes: false` to size everything manually.

## License

MIT — see [LICENSE](LICENSE).
