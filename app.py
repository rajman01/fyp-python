"""Flask entry point exposing the plan generation endpoints.

Each plan endpoint accepts a JSON payload (see ``models.plan.PlanProps``),
generates the drawing, and responds with the URL of the uploaded DXF/DWG/PDF
bundle.

``/cad/inspect`` is the odd one out: it takes an uploaded legacy DWG/DXF and
reports what survey data can be recovered from it (Task 11).
"""

import json
import logging
import os
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, request
from pydantic import ValidationError

from werkzeug.utils import secure_filename

from cad_import import CadImportError, inspect_drawing
from point_stream import PointStreamError, read_plan_stream
from progress import JobProgress
from models.plan import STANDARD_SCALES
from plans import CadastralPlan, LayoutPlan, RoutePlan, TopographicPlan

logging.basicConfig(level=logging.INFO)
# ezdxf logs an INFO line for every entity on a hidden layer (e.g. the TIN
# mesh when show_mesh is off) — hundreds of lines per render. Warnings only.
logging.getLogger("ezdxf").setLevel(logging.WARNING)

app = Flask(__name__)

#: Upload ceiling for legacy drawings. A survey DWG is normally well under a
#: megabyte; this leaves room for a drawing carrying scanned raster images
#: without letting the service be used as a file dump.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

ALLOWED_CAD_EXTENSIONS = (".dwg", ".dxf")


#: Content type used to stream a plan and its points together. See
#: ``point_stream`` for why a large survey cannot arrive as one JSON body.
NDJSON_CONTENT_TYPE = "application/x-ndjson"

#: Points for a background job are fetched from object storage; a large export
#: over a slow link should not be mistaken for a hang.
POINTS_FETCH_TIMEOUT = 300


def read_plan_request():
    """The plan payload for this request, however it was sent.

    Three shapes, smallest first:

    * ordinary JSON -- a plan whose points fit comfortably in a request body;
    * NDJSON -- the plan on the first line then one point per line, read
      incrementally so a million-point survey costs the same memory as a
      thousand-point one;
    * a JSON envelope carrying ``points_url`` -- the background-job path,
      where the API has written the same NDJSON to object storage and the
      engine streams it back. Passing by reference is what lets a queued job
      outlive the worker that prepared it.

    Returns ``(plan, progress)``.
    """
    content_type = (request.content_type or "").split(";")[0].strip().lower()

    if content_type == NDJSON_CONTENT_TYPE:
        plan, stats = read_plan_stream(request.stream)
        app.logger.info("streamed plan: %s", stats)
        return plan, JobProgress(None)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise PointStreamError("Request body must be a JSON object")

    points_url = data.get("points_url")
    if not points_url:
        return data, JobProgress(data.get("job_id"))

    progress = JobProgress(data.get("job_id"))
    progress.stage("reading survey points", fraction=0.0)

    try:
        with urlopen(points_url, timeout=POINTS_FETCH_TIMEOUT) as response:
            plan, stats = read_plan_stream(
                response, on_progress=progress.counter("reading survey points"),
            )
    except (URLError, HTTPError) as exc:
        raise PointStreamError(f"Could not fetch the survey points: {exc}") from exc

    app.logger.info("fetched plan points: %s", stats)
    progress.stage("reading survey points", fraction=0.15,
                   processed=stats["coordinates_received"],
                   total=stats["coordinates_received"])
    return plan, progress


def generate_plan(plan_cls, plan_label: str):
    """Validate the request payload, generate the plan, and upload it."""
    try:
        data, progress = read_plan_request()
    except PointStreamError as e:
        return jsonify({"error": str(e)}), 400

    try:
        plan = plan_cls(**data)
    except ValidationError as e:
        return jsonify({
            "error": "Invalid plan data",
            "details": json.loads(e.json(include_url=False)),
        }), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    progress.stage("drawing the plan", fraction=0.25)
    plan.draw()

    progress.stage("exporting DXF, DWG and PDF", fraction=0.75)
    key = plan.save()
    return jsonify({
        "message": f"{plan_label} plan generated",
        "filename": plan.name,
        # The object key. The archive is private, so there is no link to hand
        # back -- the API signs one for the plan's owner on request.
        "key": key,
    }), 200


@app.get("/")
def home():
    return jsonify({"service": "survey-plan-generator", "status": "ok"})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/cadastral/plan")
def generate_cadastral_plan():
    return generate_plan(CadastralPlan, "Cadastral")


@app.post("/topographic/plan")
def generate_topographic_plan():
    return generate_plan(TopographicPlan, "Topographic")


@app.post("/layout/plan")
def generate_layout_plan():
    return generate_plan(LayoutPlan, "Layout")


@app.post("/route/plan")
def generate_route_plan():
    return generate_plan(RoutePlan, "Route")


#: Plan types the scale endpoint answers for, by the same names the generate
#: routes use.
PLAN_TYPES = {
    "cadastral": CadastralPlan,
    "topographic": TopographicPlan,
    "layout": LayoutPlan,
    "route": RoutePlan,
}


@app.post("/<plan_type>/scale")
def plan_scale_options(plan_type: str):
    """Which standard scales this plan fits on its sheet, and which to use.

    The engine already falls back to the largest standard scale that fits when
    the requested one is too tight, and until now the only way to find that
    out was to generate the plan and read the warning. That put the caller in
    the position of offering a menu of scales without knowing which of them
    were real: pick 1:1000 for a 200 m site and you get 1:5000 and a note
    after the fact.

    The answer lives here rather than in the caller because of what it depends
    on -- the wrapped title's measured height, the schedule band's own column
    widths, the annotation margin of the longest station id. Those are the
    engine's own numbers, and a second implementation of them elsewhere would
    be a second set to keep in step.

    ``bounds`` may be given instead of shipping the survey: a topographic plan
    is a million spot heights whose only bearing on the answer is the box they
    occupy, and the caller can measure that box far more cheaply than it can
    send it.
    """
    plan_cls = PLAN_TYPES.get(plan_type)
    if plan_cls is None:
        return jsonify({"error": f"Unknown plan type '{plan_type}'"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    data = dict(data)
    bounds = data.pop("bounds", None)

    # Probed at the top of the ladder so building it never trips the
    # does-not-fit check on the way in. The answer does not depend on the
    # scale asked for -- see BasePlan.required_scale.
    data["scale"] = max(STANDARD_SCALES)
    data["fit_scale_to_sheet"] = True

    try:
        plan = plan_cls(**data)
    except ValidationError as e:
        return jsonify({
            "error": "Invalid plan data",
            "details": json.loads(e.json(include_url=False)),
        }), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if bounds is not None:
        try:
            plan._bounding_box = (
                float(bounds["min_easting"]), float(bounds["min_northing"]),
                float(bounds["max_easting"]), float(bounds["max_northing"]),
            )
        except (KeyError, TypeError, ValueError):
            return jsonify({
                "error": "bounds needs min_easting, min_northing, max_easting "
                         "and max_northing",
            }), 400

    fits = plan.fitting_scales()
    min_x, min_y, max_x, max_y = plan._bounding_box
    return jsonify({
        "scales": list(STANDARD_SCALES),
        "fits": fits,
        # What the sheet should default to: the one that fits and draws the
        # plan largest. Null means no scale on the ladder is enough, which is
        # a prompt for a bigger sheet, not a smaller scale.
        "recommended": fits[0] if fits else None,
        "required": plan.required_scale(),
        "page_size": getattr(plan.page_size, "value", plan.page_size),
        "page_orientation": getattr(plan.page_orientation, "value",
                                    plan.page_orientation),
        "ground": None if min_x is None else {
            "width": max_x - min_x,
            "height": max_y - min_y,
        },
    }), 200


@app.post("/cad/inspect")
def inspect_cad_upload():
    """Report the survey data recoverable from an uploaded DWG/DXF.

    Returns every closed shape found, with its layer, area and vertices, plus
    the point features, labels, detected units and coordinate extent, so the
    caller can present the choice rather than the service guessing which
    shape is the boundary.

    Optional form fields:

    ``units``     ``$INSUNITS`` code overriding the drawing's own units.
    ``ring_id``   when given, the response also carries ``coordinates``: that
                  ring turned into a plan coordinate register, ready to feed
                  the existing pipeline.
    """
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file was uploaded"}), 400

    filename = secure_filename(upload.filename)
    extension = os.path.splitext(filename)[1].lower()
    if extension not in ALLOWED_CAD_EXTENSIONS:
        return jsonify({
            "error": "Unsupported file type. Upload a DWG or DXF drawing."
        }), 400

    units_override = None
    raw_units = (request.form.get("units") or "").strip()
    if raw_units:
        try:
            units_override = int(raw_units)
        except ValueError:
            return jsonify({"error": "units must be a DXF $INSUNITS code"}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, filename)
        upload.save(path)

        try:
            inspection = inspect_drawing(path, file_name=filename,
                                         units_override=units_override)
        except CadImportError as exc:
            return jsonify({"error": str(exc)}), 400

    payload = inspection.model_dump(mode="json")

    ring_id = (request.form.get("ring_id") or "").strip()
    if ring_id:
        ring = next((r for r in inspection.rings if r.id == ring_id), None)
        if ring is None:
            return jsonify({"error": f"No shape with id '{ring_id}' in this drawing"}), 400
        payload["coordinates"] = [s.model_dump(mode="json") for s in ring.coordinates]
        payload["selected_ring_id"] = ring_id

    return jsonify(payload), 200


@app.errorhandler(413)
def payload_too_large(e):
    limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
    return jsonify({"error": f"The file is too large; the limit is {limit_mb} MB"}), 413


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Resource not found"}), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Something went wrong on our side"}), 500


@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.error("Unhandled exception: %s", e, exc_info=True)
    return jsonify({"error": "An unexpected error occurred"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
