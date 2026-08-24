"""On-sheet schedule (Task 10) regression tests.

Run from the repository root:

    python tests/tables_test.py [output_dir]

Task 10 asks for an option to print a bearing/distance schedule and/or a
coordinate schedule on the plan, so the sheet is self-contained for
submission. The checks here cover the two things that make that usable:

  * the schedules carry the right values -- the same legs the drawing labels
    and the same coordinates as the register; and
  * they are placed **without overlapping anything else on the sheet**. The
    drawing area is narrowed by exactly the band the tables occupy, so this
    asserts the drawn table entities share no space with the drawing, the
    title block or the footer boxes, and stay inside the frame.
"""

import math
import os
import re
import sys
import tempfile

import ezdxf
from ezdxf import bbox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plans import CadastralPlan, LayoutPlan, TopographicPlan

BASE = {
    "id": "tables-test",
    "created_at": "2026-01-01T00:00:00Z",
    "user": "tester",
    "project": "project",
    "title": "Plan Of Table Test",
    "address": "1 Example Close",
    "local_govt": "Eti-Osa",
    "state": "Lagos",
    "scale": 1000,
    "footers": ["<p>Surveyed by <b>Tester</b></p>", "<p>Checked by QA</p>"],
}

ORIGIN_E, ORIGIN_N = 543210.0, 712345.0
CORNERS = [
    ("PB1", ORIGIN_E, ORIGIN_N),
    ("PB2", ORIGIN_E + 100, ORIGIN_N),
    ("PB3", ORIGIN_E + 100, ORIGIN_N + 80),
    ("PB4", ORIGIN_E, ORIGIN_N + 80),
]
TABLES_ON = {"show_bearing_distance_table": True, "show_coordinate_table": True}


def leg(a, b):
    (ida, ea, na), (idb, eb, nb) = a, b
    bearing = math.degrees(math.atan2(eb - ea, nb - na)) % 360
    return {
        "from": {"id": ida, "easting": ea, "northing": na},
        "to": {"id": idb, "easting": eb, "northing": nb},
        "distance": math.hypot(eb - ea, nb - na),
        "bearing": {"degrees": int(bearing), "minutes": 0, "decimal": bearing},
    }


def legs_of(corners):
    return [leg(corners[i], corners[(i + 1) % len(corners)]) for i in range(len(corners))]


def coords_of(corners):
    return [{"id": i, "easting": e, "northing": n} for i, e, n in corners]


def cadastral(**overrides):
    return BASE | {
        "type": "cadastral", "name": "tables cadastral",
        "coordinates": coords_of(CORNERS),
        "parcels": [{"name": "P1", "ids": [c[0] for c in CORNERS],
                     "area": 8000.0, "legs": legs_of(CORNERS)}],
    } | overrides


def topographic(**overrides):
    points = []
    for i in range(5):
        for j in range(5):
            points.append({
                "id": f"T{i * 5 + j + 1}",
                "easting": ORIGIN_E + i * 25,
                "northing": ORIGIN_N + j * 20,
                "elevation": round(100 + i + j, 2),
            })
    return BASE | {
        "type": "topographic", "name": "tables topo",
        "coordinates": points,
        "topographic_boundary": {
            "coordinates": coords_of(CORNERS), "area": 8000.0,
            "legs": legs_of(CORNERS),
        },
        "topographic_setting": {"tin": True, "contour_interval": 1.0, "major_contour": 5.0},
    } | overrides


def layout(**overrides):
    big = [
        ("LB1", ORIGIN_E, ORIGIN_N),
        ("LB2", ORIGIN_E + 200, ORIGIN_N),
        ("LB3", ORIGIN_E + 200, ORIGIN_N + 150),
        ("LB4", ORIGIN_E, ORIGIN_N + 150),
    ]
    return BASE | {
        "type": "layout", "name": "tables layout", "scale": 2500,
        "layout_mode": "auto",
        "layout_boundary": {
            "coordinates": coords_of(big), "area": 30000.0, "legs": legs_of(big),
        },
    } | overrides


def _build(cls, payload, out_dir, name):
    plan = cls(**payload)
    plan.draw()
    path = os.path.join(out_dir, f"{name}.dxf")
    plan.save_dxf(path)
    return plan, ezdxf.readfile(path)


def _layer_box(doc, layers):
    entities = [e for e in doc.modelspace() if e.dxf.layer in layers]
    if not entities:
        return None
    return bbox.extents(entities, fast=True)


def _overlaps(a, b, tolerance=1e-6):
    """Do two 2D boxes share any area (beyond a rounding tolerance)?"""
    if a is None or b is None:
        return False
    return (a.extmin.x < b.extmax.x - tolerance and b.extmin.x < a.extmax.x - tolerance
            and a.extmin.y < b.extmax.y - tolerance and b.extmin.y < a.extmax.y - tolerance)


CASES = (
    ("cadastral", CadastralPlan, cadastral, {"PARCELS", "BEACONS"}),
    ("topographic", TopographicPlan, topographic, {"BOUNDARY", "BEACONS", "SPOT_HEIGHTS"}),
    ("layout", LayoutPlan, layout, {"BOUNDARY", "PARCELS", "ROADS", "BEACONS"}),
)


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------
def check_toggles(out_dir):
    """Nothing is drawn unless asked for, and both toggles work alone."""
    errors = []
    for name, cls, payload, _ in CASES:
        _, off = _build(cls, payload(), out_dir, f"{name}_off")
        if _layer_box(off, {"TABLES"}) is not None:
            errors.append(f"{name}: schedules drawn even though both toggles are off")

        _, bearing = _build(cls, payload(show_bearing_distance_table=True),
                            out_dir, f"{name}_bearing")
        text = [e.dxf.text for e in bearing.modelspace().query("TEXT[layer=='TABLES']")]
        if not any("BEARING" in t for t in text):
            errors.append(f"{name}: bearing/distance schedule missing when switched on")
        if any("NORTHING" in t for t in text):
            errors.append(f"{name}: coordinate schedule drawn when only bearing was asked for")

        _, coords = _build(cls, payload(show_coordinate_table=True),
                           out_dir, f"{name}_coords")
        text = [e.dxf.text for e in coords.modelspace().query("TEXT[layer=='TABLES']")]
        if not any("NORTHING" in t for t in text):
            errors.append(f"{name}: coordinate schedule missing when switched on")
        if any("DIST." in t for t in text):
            errors.append(f"{name}: bearing schedule drawn when only coordinates were asked for")

    return errors


def check_no_overlap(out_dir):
    """The schedules share no space with the drawing, title or footers."""
    errors = []
    for name, cls, payload, drawing_layers in CASES:
        plan, doc = _build(cls, payload(**TABLES_ON), out_dir, f"{name}_tables")

        tables = _layer_box(doc, {"TABLES"})
        if tables is None:
            errors.append(f"{name}: no schedule drawn")
            continue

        for label, layers in (
            ("the drawing", drawing_layers),
            ("the drawing labels", {"LABELS"}),
            ("the title block", {"TITLE_BLOCK"}),
            ("the footer boxes", {"FOOTER"}),
        ):
            other = _layer_box(doc, layers)
            if _overlaps(tables, other):
                errors.append(f"{name}: the schedules overlap {label}")

        frame = _layer_box(doc, {"FRAME"})
        if frame is not None:
            inside = (frame.extmin.x - 1e-6 <= tables.extmin.x
                      and tables.extmax.x <= frame.extmax.x + 1e-6
                      and frame.extmin.y - 1e-6 <= tables.extmin.y
                      and tables.extmax.y <= frame.extmax.y + 1e-6)
            if not inside:
                errors.append(f"{name}: the schedules fall outside the drawing frame")

    return errors


def check_values(out_dir):
    """Rows carry the real legs and the real coordinates."""
    errors = []

    plan, doc = _build(CadastralPlan, cadastral(**TABLES_ON), out_dir, "values_cadastral")
    text = {e.dxf.text for e in doc.modelspace().query("TEXT[layer=='TABLES']")}

    for line in ("PB1-PB2", "PB2-PB3", "PB3-PB4", "PB4-PB1"):
        if line not in text:
            errors.append(f"cadastral: leg {line} missing from the bearing schedule")
    if "100.00" not in text or "80.00" not in text:
        errors.append("cadastral: leg distances missing from the bearing schedule")

    # Coordinates: full precision, and no thousands separator anywhere (Task 4).
    if f"{ORIGIN_N:.3f}" not in text or f"{ORIGIN_E:.3f}" not in text:
        errors.append("cadastral: beacon coordinates missing from the coordinate schedule")
    if any("," in t for t in text):
        errors.append("cadastral: a schedule value carries a thousands separator")

    # Topographic and layout list their boundary, not their spot heights/plots.
    _, topo = _build(TopographicPlan, topographic(**TABLES_ON), out_dir, "values_topo")
    topo_text = {e.dxf.text for e in topo.modelspace().query("TEXT[layer=='TABLES']")}
    if "PB1-PB2" not in topo_text:
        errors.append("topographic: boundary legs missing from the schedule")
    if any(t.startswith("T1") for t in topo_text):
        errors.append("topographic: spot heights leaked into the boundary schedule")

    _, lay = _build(LayoutPlan, layout(**TABLES_ON), out_dir, "values_layout")
    lay_text = {e.dxf.text for e in lay.modelspace().query("TEXT[layer=='TABLES']")}
    if "LB1-LB2" not in lay_text:
        errors.append("layout: boundary legs missing from the schedule")
    if "LB1" not in lay_text:
        errors.append("layout: boundary coordinates missing from the schedule")

    return errors


def check_pagination(out_dir):
    """A register too long for one column flows into more, and the drawing
    still gets most of the sheet."""
    errors = []

    many = [("PB%d" % (i + 1), ORIGIN_E + (i % 12) * 8, ORIGIN_N + (i // 12) * 8)
            for i in range(96)]
    payload = BASE | {
        "type": "cadastral", "name": "long register",
        "coordinates": coords_of(many),
        "parcels": [{"name": "P1", "ids": [c[0] for c in many[:4]],
                     "area": 8000.0, "legs": legs_of(many[:4])}],
        "show_coordinate_table": True,
    }
    plan, doc = _build(CadastralPlan, payload, out_dir, "pagination")

    columns = plan._table_columns(1e9)
    band = plan._table_band_mm()
    printable_w, _ = plan.printable_area()

    if len(plan._flow_tables(plan._table_specs(),
            (plan._frame_coords[3] - plan._frame_coords[1]) * 0.5)) < 2:
        errors.append("a 96-row register should have flowed into more than one column")
    if band > printable_w * 0.45:
        errors.append(f"the schedule band took {band:.0f} mm of a {printable_w:.0f} mm sheet")

    tables = _layer_box(doc, {"TABLES"})
    parcels = _layer_box(doc, {"PARCELS"})
    if _overlaps(tables, parcels):
        errors.append("a paginated schedule overlaps the drawing")

    return errors


def check_no_double_annotation(out_dir):
    """A bearing and a distance are written once, not twice.

    The legs carry their own bearing and distance labels, and the schedule
    lists the same figures. Printing both puts the drawing's copy between the
    stations, where it has the least room and collides with the beacon ids --
    so when the schedule is switched on the legs go bare.
    """
    errors = []

    # A leg distance, exactly as add_leg_labels writes it: "42.86m". The grid
    # origin labels ("538420.0mE") are the reason this is anchored at both
    # ends rather than a substring search.
    is_distance = re.compile(r"^\d+\.\d{2}m$").match

    def leg_labels(doc):
        """The distances the drawing itself writes along the legs."""
        msp = doc.modelspace()
        text = [e.dxf.text for e in msp.query("TEXT") if e.dxf.layer != "TABLES"]
        text += [e.text for e in msp.query("MTEXT") if e.dxf.layer != "TABLES"]
        return [t for t in text if is_distance(t.strip())]

    for name, cls, payload, _ in CASES:
        if name == "layout":       # its parcels carry no legs in this fixture
            continue

        _, off = _build(cls, payload(), out_dir, f"{name}_legs_off")
        bare = leg_labels(off)
        if not bare:
            errors.append(f"{name}: no leg labels at all with the schedule off")

        _, on = _build(cls, payload(show_bearing_distance_table=True),
                       out_dir, f"{name}_legs_on")
        repeated = leg_labels(on)
        if repeated:
            errors.append(
                f"{name}: {len(repeated)} leg label(s) still drawn alongside the "
                f"schedule, e.g. {repeated[0]!r}")

        # Only the bearing schedule stands in for them -- a coordinate
        # register lists no distances, so the legs must keep their own.
        _, coords_only = _build(cls, payload(show_coordinate_table=True),
                                out_dir, f"{name}_legs_coords")
        if not leg_labels(coords_only):
            errors.append(
                f"{name}: legs went bare for a coordinate register, which "
                f"lists no bearings or distances to replace them")

    return errors


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_tables_")
    os.makedirs(out_dir, exist_ok=True)
    failures = 0

    for name, fn in (
        ("toggles", check_toggles),
        ("no overlap with the sheet", check_no_overlap),
        ("schedule values", check_values),
        ("pagination", check_pagination),
        ("legs are not labelled twice", check_no_double_annotation),
    ):
        print(f"== {name} ==")
        errors = fn(out_dir)
        for e in errors:
            failures += 1
            print("  FAIL:", e)
        if not errors:
            print("  OK")

    print(f"\nOutput directory: {out_dir}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
