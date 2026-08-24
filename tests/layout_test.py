"""Layout plan tests: generate mode and draw mode.

Run from the repository root:

    python tests/layout_test.py [output_dir]

Checks the subdivision geometry (plots inside the boundary, no overlaps,
standard plot sizes, area accounting), the setting-out CSV, and exports
DXFs for both modes. Boundary legs mimic what the AutoPlan API computes
and sends in real payloads.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ezdxf import bbox
from shapely.geometry import Polygon

from plans import LayoutPlan

BASE = {
    "id": "layout-test",
    "created_at": "2026-01-01T00:00:00Z",
    "user": "tester",
    "project": "project",
    "type": "layout",
    "title": "Layout Plan Of Unity Gardens Estate",
    "address": "Off Lekki-Epe Expressway, Ibeju-Lekki",
    "local_govt": "Ibeju-Lekki",
    "state": "Lagos",
    "scale": 1000,
    "beacon_size": 1,
    "label_size": 2.0,
    "font_size": 8,
    "footers": ["<p>LAYOUT PLAN OF</p><p><strong>UNITY GARDENS ESTATE</strong></p>"],
    "footer_size": 2.5,
}

BOUNDARY = {
    "coordinates": [
        {"id": "SAB 01", "northing": 712450.000, "easting": 543100.000},
        {"id": "SAB 02", "northing": 712445.500, "easting": 543298.750},
        {"id": "SAB 03", "northing": 712262.300, "easting": 543305.200},
        {"id": "SAB 04", "northing": 712255.800, "easting": 543112.400},
        {"id": "SAB 01", "northing": 712450.000, "easting": 543100.000},
    ],
    # As computed and attached by the AutoPlan API (traverse back computation)
    "legs": [{"from": {"id": "SAB 01", "northing": 712450, "easting": 543100}, "to": {"id": "SAB 02", "northing": 712445.5, "easting": 543298.75}, "distance": 198.801, "bearing": {"degrees": 91, "minutes": 17, "seconds": 49.349, "decimal": 91.297}, "delta_northing": -4.5, "delta_easting": 198.75}, {"from": {"id": "SAB 02", "northing": 712445.5, "easting": 543298.75}, "to": {"id": "SAB 03", "northing": 712262.3, "easting": 543305.2}, "distance": 183.314, "bearing": {"degrees": 177, "minutes": 59, "seconds": 0.946, "decimal": 177.984}, "delta_northing": -183.2, "delta_easting": 6.45}, {"from": {"id": "SAB 03", "northing": 712262.3, "easting": 543305.2}, "to": {"id": "SAB 04", "northing": 712255.8, "easting": 543112.4}, "distance": 192.91, "bearing": {"degrees": 268, "minutes": 4, "seconds": 8.685, "decimal": 268.069}, "delta_northing": -6.5, "delta_easting": -192.8}, {"from": {"id": "SAB 04", "northing": 712255.8, "easting": 543112.4}, "to": {"id": "SAB 01", "northing": 712450, "easting": 543100}, "distance": 194.595, "bearing": {"degrees": 356, "minutes": 20, "seconds": 47.497, "decimal": 356.347}, "delta_northing": 194.2, "delta_easting": -12.4}],
}


def generate_payload():
    return BASE | {
        "name": "layout generate",
        "layout_boundary": dict(BOUNDARY),
        "layout_parameters": {
            "plot": {"frontage": 15.0, "depth": 30.0, "min_area": 200.0},
            "roads": {"major_width": 15.0, "collector_width": 12.0, "access_width": 9.0,
                      "major_road_name": "Unity Avenue"},
            "blocks": {"double_loaded": True, "max_length": 90.0},
            "reserves": {"open_space_percent": 8.0, "commercial_along_major": True,
                         "facilities": ["school"]},
        },
    }


def draw_payload():
    return BASE | {
        "name": "layout draw",
        "layout_boundary": dict(BOUNDARY),
        "coordinates": [
            {"id": "PB 101", "northing": 712440.120, "easting": 543110.500},
            {"id": "PB 102", "northing": 712440.050, "easting": 543125.500},
            {"id": "PB 103", "northing": 712410.080, "easting": 543125.430},
            {"id": "PB 104", "northing": 712410.150, "easting": 543110.430},
            {"id": "PB 105", "northing": 712440.000, "easting": 543140.510},
            {"id": "PB 106", "northing": 712410.030, "easting": 543140.440},
            {"id": "PB 107", "northing": 712440.180, "easting": 543155.520},
            {"id": "PB 108", "northing": 712410.210, "easting": 543155.450},
            {"id": "RC 01", "northing": 712400.000, "easting": 543105.000},
            {"id": "RC 02", "northing": 712400.000, "easting": 543160.000},
        ],
        "plots": [
            {"block": "A", "number": 1, "ids": ["PB 101", "PB 102", "PB 103", "PB 104"],
             "area": 450.0, "use": "residential"},
            {"block": "A", "number": 2, "ids": ["PB 102", "PB 105", "PB 106", "PB 103"],
             "area": 450.0, "use": "residential"},
            {"block": "A", "number": 3, "ids": ["PB 105", "PB 107", "PB 108", "PB 106"],
             "area": 450.0, "use": "commercial"},
        ],
        "roads": [
            {"name": "Road 1", "width": 9.0, "centerline_ids": ["RC 01", "RC 02"]},
        ],
    }


def check_generate(plan: LayoutPlan) -> list:
    errors = []
    boundary = plan._boundary_polygon
    plots = plan.plots or []

    if not plots:
        return ["no plots generated"]

    polygons = [Polygon(plan._plot_points(p)) for p in plots]

    # 1. every plot inside the boundary
    tolerant = boundary.buffer(0.01)
    for p, poly in zip(plots, polygons):
        if not tolerant.contains(poly):
            errors.append(f"{p.label()} extends outside the boundary")

    # 2. no two plots overlap
    for i in range(len(polygons)):
        for j in range(i + 1, len(polygons)):
            inter = polygons[i].intersection(polygons[j]).area
            if inter > 0.01:
                errors.append(f"{plots[i].label()} overlaps {plots[j].label()} by {inter:.2f} sqm")

    # 3. interior plots are the standard module
    standard = [p for p in plots if p.use in ("residential", "commercial")
                and abs((p.area or 0) - 450.0) < 0.5]
    if len(standard) < len(plots) * 0.3:
        errors.append(f"too few standard 450 sqm plots: {len(standard)}/{len(plots)}")

    # 4. area accounting: plots + circulation == site area
    plots_area = sum(poly.area for poly in polygons)
    if plots_area > boundary.area:
        errors.append("plot area exceeds site area")

    # 5. reserves present
    uses = {p.use for p in plots}
    if "open_space" not in uses:
        errors.append("no open space reserved")
    if "school" not in uses:
        errors.append("no school facility reserved")
    if "commercial" not in uses:
        errors.append("no commercial plots along the major road")

    # 6. boundary legs from the payload survive into the plan
    if not plan.layout_boundary.legs:
        errors.append("boundary legs missing from payload")

    # 7. CSV covers boundary beacons, plot corners, and road points
    csv = plan.build_setting_out_csv()
    for token in ("SAB 01", "LP 1", "RC 1", "DESCRIPTION"):
        if token not in csv:
            errors.append(f"setting-out CSV missing '{token}'")

    # 8. every plot corner id resolves and plots are closed rings
    for p in plots:
        if len(p.ids) < 3:
            errors.append(f"{p.label()} has fewer than 3 corners")

    print(f"  plots: {len(plots)} | uses: { {u: sum(1 for p in plots if p.use == u) for u in uses} }")
    print(f"  site {boundary.area:,.0f} sqm | plots {plots_area:,.0f} sqm "
          f"({plots_area / boundary.area * 100:.1f}%) | roads/circ {boundary.area - plots_area:,.0f} sqm")
    print(f"  csv rows: {csv.count(chr(10)) - 1}")
    return errors


#: How much clear sheet the schedule must keep from the origin values, in
#: printed millimetres. Not zero: the two were only ~1 mm apart before, which
#: is no overlap by measurement and reads as one on paper -- the table's left
#: border ran straight down the origin tick.
SCHEDULE_CLEARANCE_MM = 2.0


def check_schedule_clears_origin_values(plan: LayoutPlan) -> list:
    """The land-use schedule keeps clear of the quoted origin values.

    The schedule sits in the band above the footer boxes, left-aligned with
    the site. The vertical origin value runs up the sheet through that same
    band, on a tick drawn at the origin's own easting -- so on a site whose
    western edge *is* its origin, aligning the table with the site put its
    left border straight down the tick and its first column against the
    value.
    """
    errors = []

    origin = plan._north_arrow_reference()
    if origin is None:
        return ["the plan quotes no origin, so nothing was checked"]

    schedule = [e for e in plan._drawer.msp
                if e.dxftype() == "TEXT" and str(e.dxf.text) in
                ("LAND USE", "TOTAL", "ROADS / CIRCULATION")]
    if not schedule:
        return ["no land-use schedule was drawn, so nothing was checked"]
    table = bbox.extents(schedule)

    # The tick itself, and the value resting against it.
    obstacles = [("the origin tick", origin.easting)]
    for entity in plan._drawer.msp:
        if entity.dxftype() != "TEXT" or not str(entity.dxf.text).endswith("mE"):
            continue
        value = bbox.extents([entity])
        if value is None or not value.has_data:
            continue
        if value.extmin.y > table.extmax.y or value.extmax.y < table.extmin.y:
            continue        # not in the schedule's band at all
        obstacles.append((str(entity.dxf.text), value.extmax.x))

    needed = SCHEDULE_CLEARANCE_MM * plan.mm_to_model
    for what, edge in obstacles:
        gap = table.extmin.x - edge
        if gap < needed:
            errors.append(
                f"the land-use schedule leaves {gap / plan.mm_to_model:.1f} mm "
                f"between itself and {what}, under the "
                f"{SCHEDULE_CLEARANCE_MM:.0f} mm it needs to read as separate")

    return errors


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_layout_")
    os.makedirs(out_dir, exist_ok=True)
    failures = 0

    print("== generate mode ==")
    plan = LayoutPlan(**generate_payload())
    plan.draw()
    errors = check_generate(plan)
    plan.save_dxf(os.path.join(out_dir, "layout_generate.dxf"))
    with open(os.path.join(out_dir, "setting_out.csv"), "w") as f:
        f.write(plan.build_setting_out_csv())
    for e in errors:
        failures += 1
        print("  FAIL:", e)
    if not errors:
        print("  OK")

    print("== schedule clears the origin values ==")
    errors = check_schedule_clears_origin_values(plan)
    for e in errors:
        failures += 1
        print("  FAIL:", e)
    if not errors:
        print("  OK")

    print("== draw mode ==")
    plan2 = LayoutPlan(**draw_payload())
    plan2.draw()
    plan2.save_dxf(os.path.join(out_dir, "layout_draw.dxf"))
    csv2 = plan2.build_setting_out_csv()
    for token in ("PB 101", "Block A Plot 1", "RC 01"):
        if token not in csv2:
            failures += 1
            print(f"  FAIL: draw-mode CSV missing '{token}'")
    print("  OK" if failures == 0 else "")

    print(f"\nOutput directory: {out_dir}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
