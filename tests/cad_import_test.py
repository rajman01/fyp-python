"""Legacy CAD import regression tests (Task 11).

Run inside the service container, which carries the ODA File Converter:

    docker compose run --rm engine python tests/cad_import_test.py

The fixtures are built to look like real legacy drawings rather than tidy
ones: the boundary appears as a closed polyline in one, as four loose lines
that miss closure in another, and buried inside a nested block in a third,
each surrounded by the usual noise of a finished sheet -- title text, a north
arrow, hatch boxes and annotation on other layers.

Every case is round-tripped through DWG, because DWG is what surveyors
actually hold; a test that only ever reads DXF would not exercise the path
this feature exists for.
"""

import math
import os
import sys
import tempfile

import ezdxf
from ezdxf.addons import odafc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cad_import import (
    CadImportError,
    inspect_drawing,
    ring_to_coordinates,
)
from models.cad import RingSource
from plans import CadastralPlan

# A 100 m x 80 m parcel on a UTM zone 31 grid.
ORIGIN_E, ORIGIN_N = 543210.0, 712345.0
CORNERS = [
    ("PB1", ORIGIN_E, ORIGIN_N),
    ("PB2", ORIGIN_E + 100, ORIGIN_N),
    ("PB3", ORIGIN_E + 100, ORIGIN_N + 80),
    ("PB4", ORIGIN_E, ORIGIN_N + 80),
]
EXPECTED_AREA = 8000.0


# ----------------------------------------------------------------------
# Fixture drawings
# ----------------------------------------------------------------------
def _beacon_block(doc):
    if "BEACON" not in doc.blocks:
        block = doc.blocks.new("BEACON")
        block.add_lwpolyline([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)],
                             close=True)
    return "BEACON"


def _add_sheet_noise(doc, msp):
    """The clutter every finished plan sheet carries."""
    msp.add_text("PLAN OF SURVEY", height=5,
                 dxfattribs={"layer": "TITLE"}).set_placement((ORIGIN_E, ORIGIN_N + 140))
    msp.add_text("SCALE 1:500", height=3,
                 dxfattribs={"layer": "TITLE"}).set_placement((ORIGIN_E, ORIGIN_N + 130))
    # A north arrow drawn as loose lines on its own layer
    msp.add_line((ORIGIN_E - 30, ORIGIN_N), (ORIGIN_E - 30, ORIGIN_N + 20),
                 dxfattribs={"layer": "NORTH"})
    msp.add_line((ORIGIN_E - 33, ORIGIN_N + 14), (ORIGIN_E - 30, ORIGIN_N + 20),
                 dxfattribs={"layer": "NORTH"})
    # A small annotation box that must not be mistaken for a parcel
    msp.add_lwpolyline([(ORIGIN_E + 120, ORIGIN_N), (ORIGIN_E + 120.4, ORIGIN_N),
                        (ORIGIN_E + 120.4, ORIGIN_N + 0.4), (ORIGIN_E + 120, ORIGIN_N + 0.4)],
                       close=True, dxfattribs={"layer": "ANNO"})


def _add_beacons(doc, msp, corners=CORNERS, label_offset=1.0):
    name = _beacon_block(doc)
    for station, easting, northing in corners:
        msp.add_blockref(name, (easting, northing), dxfattribs={"layer": "BEACONS"})
        msp.add_text(station, height=2, dxfattribs={"layer": "BEACON_TEXT"}).set_placement(
            (easting + label_offset, northing + label_offset))


def make_closed_polyline_dxf(path, insunits=6):
    """The tidy case: boundary as one closed LWPOLYLINE."""
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = insunits
    msp = doc.modelspace()
    msp.add_lwpolyline([(e, n) for _, e, n in CORNERS], close=True,
                       dxfattribs={"layer": "BOUNDARY"})
    _add_beacons(doc, msp)
    _add_sheet_noise(doc, msp)
    doc.saveas(path)
    return path


def make_loose_lines_dxf(path, gap=0.005):
    """The common legacy case: boundary drawn as separate lines that do not
    quite meet."""
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 6
    msp = doc.modelspace()
    pts = [(e, n) for _, e, n in CORNERS]
    msp.add_line(pts[0], pts[1], dxfattribs={"layer": "BOUNDARY"})
    msp.add_line(pts[1], pts[2], dxfattribs={"layer": "BOUNDARY"})
    msp.add_line(pts[2], pts[3], dxfattribs={"layer": "BOUNDARY"})
    # last segment stops short of the first corner
    msp.add_line(pts[3], (pts[0][0], pts[0][1] + gap), dxfattribs={"layer": "BOUNDARY"})
    _add_beacons(doc, msp)
    _add_sheet_noise(doc, msp)
    doc.saveas(path)
    return path


def make_nested_block_dxf(path):
    """Boundary inside a block, inside another block."""
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 6
    inner = doc.blocks.new("PARCEL_GEOMETRY")
    inner.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True,
                         dxfattribs={"layer": "BOUNDARY"})
    outer = doc.blocks.new("SHEET")
    outer.add_blockref("PARCEL_GEOMETRY", (0, 0))
    msp = doc.modelspace()
    msp.add_blockref("SHEET", (ORIGIN_E, ORIGIN_N), dxfattribs={"layer": "SHEET"})
    _add_beacons(doc, msp)
    _add_sheet_noise(doc, msp)
    doc.saveas(path)
    return path


def make_feet_dxf(path):
    """A drawing recorded in feet."""
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 2  # feet
    msp = doc.modelspace()
    # 100 x 80 *feet*
    msp.add_lwpolyline([(1000, 2000), (1100, 2000), (1100, 2080), (1000, 2080)],
                       close=True, dxfattribs={"layer": "BOUNDARY"})
    doc.saveas(path)
    return path


def to_dwg(dxf_path):
    dwg_path = os.path.splitext(dxf_path)[0] + ".dwg"
    odafc.convert(dxf_path, dwg_path, version="R2000")
    return dwg_path


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _boundary_ring(inspection):
    """The ring a user would pick: the largest one."""
    return inspection.rings[0] if inspection.rings else None


def _ring_matches_parcel(ring, tolerance=0.01):
    expected = {(round(e, 2), round(n, 2)) for _, e, n in CORNERS}
    got = {(round(v.easting, 2), round(v.northing, 2)) for v in ring.vertices}
    return expected == got


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------
def check_closed_polyline(out_dir):
    """The tidy case, through a real DWG round trip."""
    errors = []
    dwg = to_dwg(make_closed_polyline_dxf(os.path.join(out_dir, "closed.dxf")))
    inspection = inspect_drawing(dwg)

    if inspection.file_format != "dwg":
        errors.append(f"format reported as {inspection.file_format}, expected dwg")

    ring = _boundary_ring(inspection)
    if ring is None:
        return errors + ["no ring found in the tidy drawing"]

    if not _ring_matches_parcel(ring):
        errors.append(f"ring vertices wrong: {[(v.easting, v.northing) for v in ring.vertices]}")
    if abs(ring.area - EXPECTED_AREA) > 0.01:
        errors.append(f"area {ring.area}, expected {EXPECTED_AREA}")
    if ring.source != RingSource.POLYLINE:
        errors.append(f"source {ring.source}, expected polyline")
    if ring.layer != "BOUNDARY":
        errors.append(f"layer {ring.layer}, expected BOUNDARY")

    # The tiny annotation box must not be offered as a parcel.
    if any(r.area < 1.0 for r in inspection.rings):
        errors.append("a sub-square-metre annotation box was offered as a boundary")

    return errors


def check_station_ids_recovered(out_dir):
    """The surveyor's own beacon numbering is read off the sheet."""
    errors = []
    dwg = to_dwg(make_closed_polyline_dxf(os.path.join(out_dir, "ids.dxf")))
    inspection = inspect_drawing(dwg)
    ring = _boundary_ring(inspection)
    if ring is None:
        return ["no ring found"]

    register = ring_to_coordinates(ring, inspection.points)
    ids = [row.id for row in register]
    if sorted(ids) != ["PB1", "PB2", "PB3", "PB4"]:
        errors.append(f"station ids {ids}, expected the drawing's own PB1..PB4")

    # Ids must land on the right corners, not merely be present.
    by_id = {row.id: (row.easting, row.northing) for row in register}
    for station, easting, northing in CORNERS:
        got = by_id.get(station)
        if got is None:
            errors.append(f"{station} missing from the register")
        elif abs(got[0] - easting) > 0.01 or abs(got[1] - northing) > 0.01:
            errors.append(f"{station} at {got}, expected ({easting}, {northing})")

    if len(set(ids)) != len(ids):
        errors.append("the register carries duplicate station ids")

    return errors


def check_ring_order(out_dir):
    """The register reproduces the surveyor's own numbering, not the order the
    geometry happened to be drawn or chained in."""
    errors = []
    for name, builder in (("closed", make_closed_polyline_dxf),
                          ("loose", make_loose_lines_dxf),
                          ("nested", make_nested_block_dxf)):
        dwg = to_dwg(builder(os.path.join(out_dir, f"order_{name}.dxf")))
        inspection = inspect_drawing(dwg)
        ring = _boundary_ring(inspection)
        if ring is None:
            errors.append(f"{name}: no ring found")
            continue
        ids = [row.id for row in ring_to_coordinates(ring, inspection.points)]
        if ids != ["PB1", "PB2", "PB3", "PB4"]:
            errors.append(f"{name}: register runs {ids}, expected PB1..PB4 in order")
    return errors


def check_unlabelled_ring_is_deterministic(out_dir):
    """A ring with no station ids still imports the same way every time."""
    errors = []
    dxf = os.path.join(out_dir, "unlabelled.dxf")
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 6
    # Drawn counter-clockwise, starting from the north-east corner.
    msp = doc.modelspace()
    msp.add_lwpolyline([(ORIGIN_E + 100, ORIGIN_N + 80), (ORIGIN_E, ORIGIN_N + 80),
                        (ORIGIN_E, ORIGIN_N), (ORIGIN_E + 100, ORIGIN_N)],
                       close=True, dxfattribs={"layer": "BOUNDARY"})
    doc.saveas(dxf)

    inspection = inspect_drawing(to_dwg(dxf))
    register = ring_to_coordinates(_boundary_ring(inspection), inspection.points)

    first = (register[0].easting, register[0].northing)
    if first != (ORIGIN_E, ORIGIN_N):
        errors.append(f"unlabelled ring starts at {first}, expected the south-west corner")

    ids = [row.id for row in register]
    if ids != ["PB1", "PB2", "PB3", "PB4"]:
        errors.append(f"generated ids {ids}, expected PB1..PB4")

    # Clockwise from the south-west corner: SW -> NW -> NE -> SE.
    if (register[1].easting, register[1].northing) != (ORIGIN_E, ORIGIN_N + 80):
        errors.append("unlabelled ring was not wound clockwise")

    return errors


def check_loose_lines(out_dir):
    """A boundary drawn as separate lines that miss closure is rebuilt."""
    errors = []
    dwg = to_dwg(make_loose_lines_dxf(os.path.join(out_dir, "loose.dxf"), gap=0.005))
    inspection = inspect_drawing(dwg)

    ring = _boundary_ring(inspection)
    if ring is None:
        return ["a boundary drawn as loose lines was not rebuilt"]

    if abs(ring.area - EXPECTED_AREA) > 1.0:
        errors.append(f"rebuilt area {ring.area}, expected about {EXPECTED_AREA}")
    if ring.source != RingSource.CHAINED:
        errors.append(f"source {ring.source}, expected chained")
    if ring.gap_closed is None or ring.gap_closed <= 0:
        errors.append("the bridged gap was not reported to the user")
    if len(ring.vertices) != 4:
        errors.append(f"{len(ring.vertices)} vertices, expected 4")

    return errors


def check_gap_too_wide(out_dir):
    """A boundary with a real break is not silently invented."""
    dwg = to_dwg(make_loose_lines_dxf(os.path.join(out_dir, "wide.dxf"), gap=5.0))
    inspection = inspect_drawing(dwg)
    ring = _boundary_ring(inspection)
    if ring is not None and abs(ring.area - EXPECTED_AREA) < 1.0:
        return ["a 5 m break in the boundary was closed as if it were a rounding gap"]
    return []


def check_nested_block(out_dir):
    """Geometry buried in nested blocks is found at its world position."""
    errors = []
    dwg = to_dwg(make_nested_block_dxf(os.path.join(out_dir, "nested.dxf")))
    inspection = inspect_drawing(dwg)

    ring = _boundary_ring(inspection)
    if ring is None:
        return ["boundary inside a nested block was not found"]
    if not _ring_matches_parcel(ring):
        errors.append(
            "block geometry was not transformed to world coordinates: "
            f"{[(round(v.easting, 1), round(v.northing, 1)) for v in ring.vertices]}"
        )
    return errors


def check_units(out_dir):
    """A drawing in feet is converted, and the unit can be overridden."""
    errors = []
    dwg = to_dwg(make_feet_dxf(os.path.join(out_dir, "feet.dxf")))

    inspection = inspect_drawing(dwg)
    if inspection.units != "feet":
        errors.append(f"units reported as {inspection.units}, expected feet")
    ring = _boundary_ring(inspection)
    if ring is None:
        return errors + ["no ring found in the imperial drawing"]

    # 100 ft x 80 ft = 8000 sq ft = 743.22 sq m
    expected_m2 = 8000 * (0.3048 ** 2)
    if abs(ring.area - expected_m2) > 0.5:
        errors.append(f"area {ring.area} sq m, expected about {expected_m2:.1f}")
    if abs(ring.vertices[0].easting - 1000 * 0.3048) > 0.01:
        errors.append("coordinates were not converted from feet to metres")

    # Overriding the unit to metres must scale everything back up.
    override = inspect_drawing(dwg, units_override=6)
    if abs(override.rings[0].area - EXPECTED_AREA) > 0.01:
        errors.append(f"units_override ignored: area {override.rings[0].area}")

    return errors


def check_missing_units_warns(out_dir):
    """A drawing with no recorded units says so rather than assuming quietly."""
    dwg = to_dwg(make_closed_polyline_dxf(os.path.join(out_dir, "nounits.dxf"), insunits=0))
    inspection = inspect_drawing(dwg)
    if not any("units" in w.lower() for w in inspection.warnings):
        return ["a drawing with no units produced no warning"]
    return []


def check_reporting(out_dir):
    """The user is told what is in the drawing, not just given a boundary."""
    errors = []
    dwg = to_dwg(make_closed_polyline_dxf(os.path.join(out_dir, "report.dxf")))
    inspection = inspect_drawing(dwg)

    layers = {layer.name for layer in inspection.layers}
    for expected in ("BOUNDARY", "BEACONS", "BEACON_TEXT", "TITLE"):
        if expected not in layers:
            errors.append(f"layer {expected} missing from the report")

    boundary = next((l for l in inspection.layers if l.name == "BOUNDARY"), None)
    if boundary is None or boundary.ring_count < 1:
        errors.append("the BOUNDARY layer does not report its ring")

    if inspection.min_easting is None:
        errors.append("no coordinate extent reported for the user to sanity-check")
    elif not (543100 < inspection.min_easting < 543300):
        errors.append(f"extent looks wrong: min easting {inspection.min_easting}")

    if len(inspection.labels) < 4:
        errors.append(f"only {len(inspection.labels)} labels found, expected the beacon ids")

    return errors


def check_errors(out_dir):
    """Unreadable input fails with something a user can act on."""
    errors = []

    bad = os.path.join(out_dir, "notes.txt")
    with open(bad, "w") as handle:
        handle.write("not a drawing")
    try:
        inspect_drawing(bad)
        errors.append("a .txt upload was accepted")
    except CadImportError as exc:
        if "dwg" not in str(exc).lower() and "dxf" not in str(exc).lower():
            errors.append(f"unhelpful message for a wrong file type: {exc}")

    empty = os.path.join(out_dir, "empty.dxf")
    ezdxf.new("R2000").saveas(empty)
    try:
        inspect_drawing(empty)
        errors.append("an empty drawing was accepted")
    except CadImportError as exc:
        if "empty" not in str(exc).lower():
            errors.append(f"unhelpful message for an empty drawing: {exc}")

    return errors


def check_end_to_end(out_dir):
    """Imported coordinates generate a cadastral plan, unchanged."""
    errors = []
    dwg = to_dwg(make_closed_polyline_dxf(os.path.join(out_dir, "e2e.dxf")))
    inspection = inspect_drawing(dwg)
    ring = _boundary_ring(inspection)
    register = ring_to_coordinates(ring, inspection.points)

    # Legs are recomputed from the geometry -- never read from the sheet.
    legs = []
    for index, row in enumerate(register):
        nxt = register[(index + 1) % len(register)]
        dx = nxt.easting - row.easting
        dy = nxt.northing - row.northing
        bearing = math.degrees(math.atan2(dx, dy)) % 360
        legs.append({
            "from": {"id": row.id, "easting": row.easting, "northing": row.northing},
            "to": {"id": nxt.id, "easting": nxt.easting, "northing": nxt.northing},
            "distance": math.hypot(dx, dy),
            "bearing": {"degrees": int(bearing), "minutes": 0, "decimal": bearing},
        })

    plan = CadastralPlan(**{
        "id": "cad-import", "created_at": "2026-01-01T00:00:00Z",
        "user": "tester", "project": "project", "type": "cadastral",
        "name": "imported", "title": "Plan From Legacy DWG",
        "state": "Lagos", "scale": 1000,
        "coordinates": [row.model_dump() for row in register],
        "parcels": [{"name": "P1", "ids": [r.id for r in register],
                     "area": EXPECTED_AREA, "legs": legs}],
        "footers": ["<p>Imported from DWG</p>"],
        "show_coordinate_table": True,
    })
    plan.draw()
    path = os.path.join(out_dir, "imported_plan.dxf")
    plan.save_dxf(path)

    doc = ezdxf.readfile(path)
    inserts = doc.modelspace().query("INSERT[name=='BEACON_POINT']")
    if len(inserts) != 4:
        errors.append(f"generated plan has {len(inserts)} beacons, expected 4")

    drawn = {(round(i.dxf.insert.x, 2), round(i.dxf.insert.y, 2)) for i in inserts}
    expected = {(round(e, 2), round(n, 2)) for _, e, n in CORNERS}
    if drawn != expected:
        errors.append(f"plan beacons at {sorted(drawn)}, expected {sorted(expected)}")

    table = {t.dxf.text for t in doc.modelspace().query("TEXT[layer=='TABLES']")}
    if "PB1" not in table:
        errors.append("the imported station ids did not reach the on-sheet schedule")

    return errors


def check_dxf_accepted(out_dir):
    """DXF uploads work too -- the same path, minus the conversion."""
    dxf = make_closed_polyline_dxf(os.path.join(out_dir, "plain.dxf"))
    inspection = inspect_drawing(dxf)
    if inspection.file_format != "dxf":
        return [f"format reported as {inspection.file_format}, expected dxf"]
    if not inspection.rings:
        return ["no ring found in a DXF upload"]
    return []


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_cad_")
    os.makedirs(out_dir, exist_ok=True)

    if not odafc.is_installed():
        print("ODA File Converter not found -- run this inside the service container:")
        print("  docker compose run --rm engine python tests/cad_import_test.py")
        sys.exit(2)

    failures = 0
    for name, fn in (
        ("closed polyline boundary (DWG)", check_closed_polyline),
        ("station ids recovered from the sheet", check_station_ids_recovered),
        ("register follows the drawing's numbering", check_ring_order),
        ("unlabelled ring is deterministic", check_unlabelled_ring_is_deterministic),
        ("boundary drawn as loose lines", check_loose_lines),
        ("a real break is not bridged", check_gap_too_wide),
        ("boundary inside nested blocks", check_nested_block),
        ("units converted and overridable", check_units),
        ("missing units warns", check_missing_units_warns),
        ("drawing contents reported", check_reporting),
        ("unreadable input", check_errors),
        ("DXF accepted too", check_dxf_accepted),
        ("end to end: DWG -> cadastral plan", check_end_to_end),
    ):
        print(f"== {name} ==")
        errors = fn(out_dir)
        for error in errors:
            failures += 1
            print("  FAIL:", error)
        if not errors:
            print("  OK")

    print(f"\nOutput directory: {out_dir}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
