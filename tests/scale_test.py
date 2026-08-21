"""True-coordinate and scale-driven sizing regression tests.

Run from the repository root:

    python tests/scale_test.py [output_dir]

Covers the two properties the drawing engine now guarantees:

  * **True coordinates.** Geometry is written to the DXF at real ground
    values, unscaled, so a beacon at easting 543210 sits at x=543210 whatever
    the plan scale. The engine used to multiply every coordinate by
    1000/scale, which put a 1:500 plan's geometry at twice its real position
    and made the exported CAD file unusable for measurement or eSVY.

  * **Scale-driven text heights (Task 8).** Every text element is specified as
    a printed size in millimetres and resolved to model units at the plan's
    scale, so selecting a scale produces a legible sheet with no manual
    resizing in AutoCAD, and the printed size is identical at every scale.

Also checks the surveyor's reference heights, the manual overrides, and the
auto-fit that keeps the title block honest when a survey will not fit its
sheet.
"""

import math
import os
import sys
import tempfile

import ezdxf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.plan import TEXT_HEIGHTS_MM
from plans import CadastralPlan

BASE = {
    "id": "scale-test",
    "created_at": "2026-01-01T00:00:00Z",
    "user": "tester",
    "project": "project",
    "type": "cadastral",
    "title": "Plan Of Scale Test",
    "address": "1 Example Close",
    "local_govt": "Eti-Osa",
    "state": "Lagos",
    "plan_number": "SC/2026/01",
    "footers": ["<p>Surveyed by <b>Tester</b></p>", "<p>Checked by QA</p>"],
}

# A 60 m x 40 m parcel: small enough to fit A4 portrait at 1:500, so the
# scale under test is the scale actually drawn.
ORIGIN_E, ORIGIN_N = 543210.0, 712345.0
WIDTH, HEIGHT = 60.0, 40.0
CORNERS = [
    ("PB1", ORIGIN_E, ORIGIN_N),
    ("PB2", ORIGIN_E + WIDTH, ORIGIN_N),
    ("PB3", ORIGIN_E + WIDTH, ORIGIN_N + HEIGHT),
    ("PB4", ORIGIN_E, ORIGIN_N + HEIGHT),
]


def leg(a, b):
    (ida, ea, na), (idb, eb, nb) = a, b
    bearing = math.degrees(math.atan2(eb - ea, nb - na)) % 360
    return {
        "from": {"id": ida, "easting": ea, "northing": na},
        "to": {"id": idb, "easting": eb, "northing": nb},
        "distance": math.hypot(eb - ea, nb - na),
        "bearing": {"degrees": int(bearing), "minutes": 0, "decimal": bearing},
    }


def payload(corners=CORNERS, **overrides):
    data = BASE | {
        "name": "scale test",
        "scale": 1000,
        "coordinates": [{"id": i, "easting": e, "northing": n} for i, e, n in corners],
        "parcels": [{
            "name": "P1",
            "ids": [c[0] for c in corners],
            "area": WIDTH * HEIGHT,
            "legs": [leg(corners[i], corners[(i + 1) % len(corners)]) for i in range(len(corners))],
        }],
    }
    data.update(overrides)
    return data


def _build(out_dir, name, **overrides):
    plan = CadastralPlan(**payload(**overrides))
    plan.draw()
    path = os.path.join(out_dir, f"{name}.dxf")
    plan.save_dxf(path)
    return plan, ezdxf.readfile(path)


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------
def check_true_coordinates(out_dir):
    """Beacons land on their real eastings/northings at every scale."""
    errors = []
    expected_e = sorted({round(e, 3) for _, e, _ in CORNERS})
    expected_n = sorted({round(n, 3) for _, _, n in CORNERS})

    for scale in (500, 1000, 2500):
        plan, doc = _build(out_dir, f"coords_{scale}", scale=scale)
        if plan.scale != scale:
            errors.append(f"1:{scale}: plan was redrawn at 1:{int(plan.scale)}, cannot judge coordinates")
            continue

        inserts = doc.modelspace().query("INSERT[name=='BEACON_POINT']")
        got_e = sorted({round(i.dxf.insert.x, 3) for i in inserts})
        got_n = sorted({round(i.dxf.insert.y, 3) for i in inserts})

        if got_e != expected_e:
            errors.append(f"1:{scale}: eastings {got_e} != {expected_e}")
        if got_n != expected_n:
            errors.append(f"1:{scale}: northings {got_n} != {expected_n}")
        if doc.header["$INSUNITS"] != 6:
            errors.append(f"1:{scale}: $INSUNITS is {doc.header['$INSUNITS']}, expected 6 (metres)")

    return errors


def check_true_distances(out_dir):
    """A leg measures its real ground length in the DXF, not a scaled one."""
    errors = []
    for scale in (500, 1000, 2500):
        plan, doc = _build(out_dir, f"dist_{scale}", scale=scale)
        if plan.scale != scale:
            continue
        parcels = doc.modelspace().query("LWPOLYLINE[layer=='PARCELS']")
        if not parcels:
            errors.append(f"1:{scale}: no parcel polyline found")
            continue

        points = [(p[0], p[1]) for p in parcels[0].get_points("xy")]
        widths = {round(abs(b[0] - a[0]), 3) for a, b in zip(points, points[1:]) if abs(b[1] - a[1]) < 1e-6}
        if WIDTH not in widths:
            errors.append(f"1:{scale}: parcel edge lengths {sorted(widths)} do not include the real {WIDTH} m")

    return errors


def check_printed_text_heights(out_dir):
    """Every element prints at its target millimetre size, at every scale."""
    errors = []
    elements = ("bearing_distance", "quoted_coordinate", "plan_number", "surveyor_name", "title")

    for scale in (500, 1000, 2500):
        plan = CadastralPlan(**payload(scale=scale))
        if plan.scale != scale:
            errors.append(f"1:{scale}: redrawn at 1:{int(plan.scale)}")
            continue

        for element in elements:
            model_height = plan.height(element, 0.0)
            printed_mm = model_height * 1000.0 / plan.scale
            target = TEXT_HEIGHTS_MM[element]
            if abs(printed_mm - target) > 0.01:
                errors.append(
                    f"1:{scale}: {element} prints at {printed_mm:.2f} mm, expected {target} mm"
                )

    return errors


def check_surveyor_reference_heights(out_dir):
    """The heights match the ranges the surveyor asked for at 1:500.

    Quoted as model units on a 1:500 sheet: bearing/distance 1.0-1.3 and
    quoted coordinates 1.5-2.0.
    """
    errors = []
    plan = CadastralPlan(**payload(scale=500))
    if plan.scale != 500:
        return [f"parcel did not fit at 1:500 (drawn at 1:{int(plan.scale)})"]

    ranges = {"bearing_distance": (1.0, 1.3), "quoted_coordinate": (1.5, 2.0)}
    for element, (low, high) in ranges.items():
        height = plan.height(element, 0.0)
        if not low <= height <= high:
            errors.append(
                f"{element} is {height:.2f} model units at 1:500, outside the surveyor's {low}-{high}"
            )

    return errors


def check_overrides(out_dir):
    """A user can still override any height, per element or wholesale."""
    errors = []

    override = CadastralPlan(**payload(scale=500, text_heights={"bearing_distance": 4.0}))
    if abs(override.height("bearing_distance", 0.0) - 4.0 * 0.5) > 1e-6:
        errors.append("per-element override in text_heights was not honoured")
    if abs(override.height("quoted_coordinate", 0.0) - TEXT_HEIGHTS_MM["quoted_coordinate"] * 0.5) > 1e-6:
        errors.append("overriding one element changed the others")

    legacy = CadastralPlan(**payload(scale=500, auto_scale_sizes=False, label_size=9.9, beacon_size=7.7))
    if abs(legacy.height("bearing_distance", legacy.label_size) - 9.9) > 1e-6:
        errors.append("auto_scale_sizes=False did not fall back to label_size")
    if abs(legacy.beacon_symbol_size - 7.7) > 1e-6:
        errors.append("auto_scale_sizes=False did not fall back to beacon_size")

    return errors


def check_size_control_groups(out_dir):
    """Each of the app's size controls moves its own group and nothing else.

    ``font_size`` used to be a single multiplier over every text element, so
    nudging the title -- a presentation choice -- silently resized the
    bearings and quoted coordinates, whose heights the surveyor specified in
    the Task 8 feedback precisely so they would not need adjusting. The four
    controls in the app are labelled Title Size, Label Size, Footer Size and
    Beacon Size, and each now governs only what its label claims.
    """
    errors = []

    TITLE = ("title", "title_note", "scale_bar")
    ANNOTATION = ("bearing_distance", "quoted_coordinate", "beacon_label",
                  "spot_height", "contour_label", "grid_label", "table", "general")
    FOOTER = ("plan_number", "surveyor_name")

    def heights(**over):
        # Compared as printed millimetres, not model units. A control that
        # enlarges the title block can push a survey onto the next standard
        # scale, and that changes every model-unit height on the sheet without
        # changing what any of them measure on paper -- which is the property
        # that actually matters here.
        plan = CadastralPlan(**payload(scale=500, **over))
        mm = plan.mm_to_model
        return ({k: plan.height(k, 0.0) / mm for k in TITLE + ANNOTATION + FOOTER},
                plan.beacon_symbol_size / mm)

    base, base_beacon = heights()

    cases = [
        ("font_size", {"font_size": 9.0}, TITLE),
        ("label_size", {"label_size": 5.0}, ANNOTATION),
        ("footer_size", {"footer_size": 5.0}, FOOTER),
    ]
    for control, over, owned in cases:
        moved, beacon = heights(**over)
        for key, value in moved.items():
            changed = abs(value - base[key]) > 1e-6
            if key in owned and not changed:
                errors.append(f"{control} did not change {key}")
            if key not in owned and changed:
                errors.append(f"{control} changed {key}, which it does not own "
                              f"({base[key]:.3f} -> {value:.3f})")
        if abs(beacon - base_beacon) > 1e-6:
            errors.append(f"{control} changed the beacon symbol")

    # Beacon Size owns the symbol and no text at all.
    moved, beacon = heights(beacon_size=4.0)
    if abs(beacon - 4.0) > 1e-6:
        errors.append(f"beacon_size did not set the symbol ({beacon:.3f})")
    for key, value in moved.items():
        if abs(value - base[key]) > 1e-6:
            errors.append(f"beacon_size changed the text height of {key}")

    # Ground-metre values from before Task 8 must not be read as millimetres:
    # a stored 0.25 means "unset", not a quarter-millimetre label.
    for control, legacy in (("label_size", 0.25), ("footer_size", 0.5),
                            ("beacon_size", 0.18)):
        moved, beacon = heights(**{control: legacy})
        if moved != base or abs(beacon - base_beacon) > 1e-6:
            errors.append(f"legacy {control}={legacy} was read as a printed size")

    # Within a group the surveyor's ratios survive: the control scales the
    # group, it does not flatten it to a single height.
    moved, _ = heights(label_size=5.0)
    ratio = TEXT_HEIGHTS_MM["quoted_coordinate"] / TEXT_HEIGHTS_MM["bearing_distance"]
    got = moved["quoted_coordinate"] / moved["bearing_distance"]
    if abs(got - ratio) > 1e-6:
        errors.append(f"label_size flattened the group ratios ({got:.3f} vs {ratio:.3f})")

    return errors


def check_frame_clearance(out_dir):
    """The sheet is chosen to hold the drawing *and* the labels around it.

    The survey extent is where the points stop, not where the drawing stops:
    every beacon carries its id beside it. Fitting the bare bounding box put
    "SBD 1204" 1.4 mm off the frame on a real plan, because the margin was a
    flat 6 mm guess and that label is 15 mm wide.
    """
    errors = []
    from ezdxf import bbox

    # Long station ids, as Nigerian plans use, against short ones.
    def clearance(ids):
        pts = [(ids[i], 543210.0 + (i % 2) * 120, 712345.0 + (i // 2) * 90)
               for i in range(len(ids))]
        data = payload(scale=1000)
        data["coordinates"] = [{"id": i, "easting": e, "northing": n} for i, e, n in pts]
        data["parcels"] = [{"name": "P", "ids": [i for i, _, _ in pts], "area": 10000.0,
                            "legs": []}]
        plan = CadastralPlan(**data)
        plan.draw()
        path = os.path.join(out_dir, f"clearance_{len(ids[0])}.dxf")
        plan.save_dxf(path)

        doc = ezdxf.readfile(path)
        fl, _, fr, _ = plan._frame_coords
        mm = plan.mm_to_model
        nearest = min(
            min(ext.extmin.x - fl, fr - ext.extmax.x) / mm
            for ext in (bbox.extents([e]) for e in doc.modelspace()
                        if e.dxf.layer in ("LABELS", "PARCELS", "BEACONS"))
            if ext is not None and ext.has_data
        )
        return plan, nearest

    for ids, label in (([f"SBD 120{i}" for i in range(1, 5)], "long ids"),
                       (["P1", "P2", "P3", "P4"], "short ids")):
        plan, nearest = clearance(ids)
        # A label that close to the border reads as a printing error.
        if nearest < 4.0:
            errors.append(f"{label}: drawing ink comes within {nearest:.2f} mm "
                          "of the frame")
        # The margin must actually account for the label, not a flat guess.
        if plan._annotation_margin_mm() <= 6.0:
            errors.append(f"{label}: margin is still the bare "
                          f"{plan._annotation_margin_mm():.1f} mm constant")

    long_plan, _ = clearance([f"SBD 120{i}" for i in range(1, 5)])
    short_plan, _ = clearance(["P1", "P2", "P3", "P4"])
    if long_plan._annotation_margin_mm() <= short_plan._annotation_margin_mm():
        errors.append("longer station ids did not earn more room than short ones")

    return errors


def check_scale_bar_labels(out_dir):
    """The scale bar's numbers sit above its ticks, not through them.

    They were placed by their top edge one tick-height up, which left the
    bottom 40% of every glyph lying over the tick it labelled.
    """
    from ezdxf import bbox

    errors = []
    plan = CadastralPlan(**payload(scale=1000))
    plan.draw()
    path = os.path.join(out_dir, "scale_bar.dxf")
    plan.save_dxf(path)

    doc = ezdxf.readfile(path)
    block = next((b for b in doc.blocks if b.name.startswith("GRAPHICAL_SCALE")), None)
    if block is None:
        return ["no graphical scale block was drawn"]

    ticks = [e for e in block if e.dxftype() == "LINE"
             and abs(e.dxf.start.x - e.dxf.end.x) < 1e-9]
    labels = [e for e in block if e.dxftype() == "TEXT"]
    if not ticks or not labels:
        return ["the graphical scale has no ticks or no labels"]

    tick_top = max(max(t.dxf.start.y, t.dxf.end.y) for t in ticks)
    for text in labels:
        box = bbox.extents([text])
        if box.extmin.y < tick_top - 1e-9:
            errors.append(f"scale label {text.dxf.text!r} crosses its tick "
                          f"(text bottom {box.extmin.y:.2f} < tick top {tick_top:.2f})")

    return errors


def check_scale_autofit(out_dir):
    """A survey too large for its sheet is zoomed out, and says so.

    The title block must state the scale the sheet was actually drawn at --
    the engine used to squeeze the drawing onto the paper at whatever scale
    happened to fit while still printing the requested one.
    """
    errors = []
    big = [
        ("PB1", ORIGIN_E, ORIGIN_N),
        ("PB2", ORIGIN_E + 400, ORIGIN_N),
        ("PB3", ORIGIN_E + 400, ORIGIN_N + 300),
        ("PB4", ORIGIN_E, ORIGIN_N + 300),
    ]
    plan = CadastralPlan(**payload(corners=big, scale=500))

    if plan.scale == 500:
        errors.append("a 400 x 300 m survey should not have fitted A4 portrait at 1:500")
    if plan.scale_adjusted_from != 500:
        errors.append(f"scale_adjusted_from is {plan.scale_adjusted_from}, expected 500")
    if f"1 : {int(plan.scale)}" not in plan.build_title():
        errors.append(f"title block does not state the drawn scale 1:{int(plan.scale)}")

    # Opting out must raise instead of silently redrawing.
    try:
        CadastralPlan(**payload(corners=big, scale=500, fit_scale_to_sheet=False))
        errors.append("fit_scale_to_sheet=False should have raised for an oversized survey")
    except ValueError as exc:
        if "does not fit" not in str(exc):
            errors.append(f"unhelpful fit error: {exc}")

    return errors


def check_sheet_frame(out_dir):
    """The frame is the printable sheet, so the plot is a true 1:scale."""
    errors = []
    for scale in (500, 1000, 2500):
        plan = CadastralPlan(**payload(scale=scale))
        if plan.scale != scale:
            continue
        left, bottom, right, top = plan._frame_coords
        printable_w_mm, printable_h_mm = plan.printable_area()

        got_w_mm = (right - left) / plan.mm_to_model
        got_h_mm = (top - bottom) / plan.mm_to_model
        if abs(got_w_mm - printable_w_mm) > 0.01 or abs(got_h_mm - printable_h_mm) > 0.01:
            errors.append(
                f"1:{scale}: frame is {got_w_mm:.1f} x {got_h_mm:.1f} mm on paper, "
                f"expected {printable_w_mm} x {printable_h_mm}"
            )

    return errors


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_scale_")
    os.makedirs(out_dir, exist_ok=True)
    failures = 0

    for name, fn in (
        ("true coordinates", check_true_coordinates),
        ("true distances", check_true_distances),
        ("printed text heights", check_printed_text_heights),
        ("surveyor reference heights", check_surveyor_reference_heights),
        ("manual overrides", check_overrides),
        ("size control groups", check_size_control_groups),
        ("frame clearance", check_frame_clearance),
        ("scale bar labels", check_scale_bar_labels),
        ("scale auto-fit", check_scale_autofit),
        ("sheet frame is the paper", check_sheet_frame),
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
