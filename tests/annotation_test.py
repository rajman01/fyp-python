"""Where the annotation lands.

Run from the repository root:

    python tests/annotation_test.py [output_dir]

A survey plan is drawn once and read many times, and everything that makes
it readable is a label: a station id, a leg's bearing, the distance along
it. Each one is anchored to something -- the id to its beacon, the bearing
to its leg -- but the anchor only says roughly where the text belongs. The
drawing used to take the anchor literally, writing every label at a fixed
offset from it, and on a parcel whose legs are short relative to its text
those offsets coincide: distances over station ids, bearings across the
line they measure.

Labels are placed now (see ``label_placement.py``), which means the sheet
that comes out depends on what else was already on it. These are checks on
that sheet rather than on the positions the placer happened to pick:

  * nothing overlaps anything; and
  * a bearing still sits nearer its leg than the station ids do, because
    dodging a collision by pushing the bearing out past a beacon label
    solves the overlap and loses what the bearing was saying.
"""

import math
import os
import re
import sys
import tempfile

from ezdxf import bbox
from ezdxf.math import Matrix44

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from label_placement import overlaps as shapes_overlap
from plans import CadastralPlan
from tests.tables_test import (
    BASE, CASES, TABLES_ON, _build, coords_of, legs_of,
)


def _oriented_box(entity, measure, shrink=0.06):
    """The sheet a label really occupies.

    ``bbox.extents`` gives the axis-aligned box *around* a rotated label,
    which for a leg label following a diagonal claims up to twice the text's
    own area -- enough to report collisions between labels a reader can see
    are clear apart. Turning the label back to horizontal, measuring it
    there and turning the box forward again measures the text itself, which
    is also what the placer works in.

    ``shrink`` pulls the box in slightly so that labels which merely graze
    are not called a collision.
    """
    if entity.dxftype() == "MTEXT":
        # ezdxf's bbox collapses the run of spaces that spreads a bearing
        # across its leg, and then reports the label as three lines tall. The
        # sheet has one line of exactly that padded width, so measure the
        # string that is actually drawn instead of asking for its extents.
        height = float(entity.dxf.char_height)
        centre = (entity.dxf.insert.x, entity.dxf.insert.y)
        return _turn(centre, measure(entity.text, height) / 2 * (1 - shrink),
                     height * 1.35 / 2 * (1 - shrink), centre,
                     math.radians(float(entity.dxf.rotation or 0.0)))

    extents = bbox.extents([entity])
    if extents is None or not extents.has_data:
        return None

    angle = math.radians(float(entity.dxf.get("rotation", 0.0) or 0.0))
    pivot = ((extents.extmin.x + extents.extmax.x) / 2,
             (extents.extmin.y + extents.extmax.y) / 2)

    if abs(angle) > 1e-9:
        flat = entity.copy()
        flat.transform(Matrix44.chain(
            Matrix44.translate(-pivot[0], -pivot[1], 0),
            Matrix44.z_rotate(-angle),
            Matrix44.translate(pivot[0], pivot[1], 0),
        ))
        extents = bbox.extents([flat])
        if extents is None or not extents.has_data:
            return None

    centre = ((extents.extmin.x + extents.extmax.x) / 2,
              (extents.extmin.y + extents.extmax.y) / 2)
    return _turn(centre,
                 (extents.extmax.x - extents.extmin.x) / 2 * (1 - shrink),
                 (extents.extmax.y - extents.extmin.y) / 2 * (1 - shrink),
                 pivot, angle)


def _turn(centre, half_w, half_h, pivot, angle):
    """A box of the given half-extents about ``centre``, turned ``angle``
    about ``pivot``."""
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = []
    for dx, dy in ((-half_w, -half_h), (half_w, -half_h),
                   (half_w, half_h), (-half_w, half_h)):
        x, y = centre[0] + dx - pivot[0], centre[1] + dy - pivot[1]
        corners.append((pivot[0] + cos_a * x - sin_a * y,
                        pivot[1] + sin_a * x + cos_a * y))
    return corners


def check_labels_do_not_collide(out_dir):
    """No label sits on another label, on a beacon, or across a parcel line.

    The drawing is annotated from a fixed formula -- distance inside the
    polygon, bearing outside, both at the leg midpoint, id up and right of the
    station -- and on any parcel whose legs are short relative to its text
    those positions coincide. The placer is what turns the formula into a
    preference, so this checks the outcome it exists for rather than the
    positions it happened to choose.
    """
    errors = []

    def annotation(doc, measure):
        """Every drawn label, as the sheet it occupies. Schedules excluded:
        they have a reserved band and are checked by tables_test."""
        boxes = []
        for entity in doc.modelspace():
            if entity.dxftype() not in ("TEXT", "MTEXT"):
                continue
            if entity.dxf.layer in ("TABLES", "TEXT"):
                continue
            corners = _oriented_box(entity, measure)
            if corners is not None:
                boxes.append((_describe(entity), corners))
        return boxes

    def _describe(entity):
        text = entity.dxf.text if entity.dxftype() == "TEXT" else entity.text
        return " ".join(text.split())

    for name, cls, payload, _ in PLACEMENT_CASES:
        for label, extra in (("bare", {}), ("with schedules", TABLES_ON)):
            plan, doc = _build(cls, payload(**extra), out_dir,
                               f"{name}_collide_{label.split()[0]}")
            boxes = annotation(doc, plan._drawer.text_width)

            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    (first, a), (second, b) = boxes[i], boxes[j]
                    if shapes_overlap(a, b):
                        errors.append(
                            f"{name} ({label}): {first!r} and {second!r} overlap")

    return errors


# ----------------------------------------------------------------------
# The bearing belongs against the line
# ----------------------------------------------------------------------
BEARING = re.compile(r"^\d+°\s+\d+'$")
DISTANCE = re.compile(r"^\d+\.\d{2}m$")


def _segments(doc):
    """The drawing's own lines: parcel edges and boundaries."""
    out = []
    for poly in doc.modelspace().query("LWPOLYLINE"):
        if poly.dxf.layer not in ("PARCELS", "BOUNDARY"):
            continue
        points = [(p[0], p[1]) for p in poly.get_points("xy")]
        if poly.closed and len(points) > 2:
            points = points + points[:1]
        out.extend(zip(points, points[1:]))
    return out


def _offset(point, segment):
    """(distance from the line, position along it, which side), or None when
    the point is off the end of the segment -- a label beyond a leg's last
    station is not annotating that leg."""
    (x1, y1), (x2, y2) = segment
    run_x, run_y = x2 - x1, y2 - y1
    length = math.hypot(run_x, run_y)
    if length < 1e-9:
        return None
    unit = (run_x / length, run_y / length)
    along = (point[0] - x1) * unit[0] + (point[1] - y1) * unit[1]
    if not 0.0 <= along <= length:
        return None
    across = (point[0] - x1) * -unit[1] + (point[1] - y1) * unit[0]
    return abs(across), along, (1 if across >= 0 else -1)


def _nearest(point, segments):
    """The line a label reads as belonging to, and where it sits relative to
    it."""
    best = None
    for segment in segments:
        placed = _offset(point, segment)
        if placed is None:
            continue
        if best is None or placed[0] < best[1][0]:
            best = (segment, placed)
    return best


#: The shape that made the problem visible: thirteen stations round a small
#: parcel, named the way a Nigerian cadastral plan names them. The legs are
#: short relative to the text, so a bearing, a distance and two station ids
#: are all competing for the same few millimetres -- which is when the order
#: they are placed in starts to show. A four-corner parcel has room for
#: everything and proves nothing.
CROWDED = [
    ("SBD 1201", 538420.000, 729310.000), ("SBD 1202", 538462.315, 729316.842),
    ("SBD 1203", 538508.774, 729321.235), ("SBD 1204", 538551.442, 729328.671),
    ("SBD 1205", 538548.917, 729372.448), ("SBD 1206", 538544.653, 729408.224),
    ("SBD 1207", 538504.291, 729405.512), ("SBD 1208", 538461.774, 729401.883),
    ("SBD 1209", 538417.639, 729398.146), ("SBD 1210", 538419.155, 729368.417),
    ("SBD 1211", 538420.512, 729339.633), ("SBD 1212", 538464.108, 729359.377),
    ("SBD 1213", 538506.517, 729363.361),
]


def crowded(**overrides):
    corners = [(i, e, n) for i, e, n in CROWDED]
    return BASE | {
        "type": "cadastral", "name": "crowded cadastral",
        "coordinates": coords_of(corners),
        "parcels": [{"name": "P1", "ids": [c[0] for c in corners],
                     "area": 9345.296, "legs": legs_of(corners)}],
    } | overrides


PLACEMENT_CASES = CASES + (
    ("crowded cadastral", CadastralPlan, crowded, {"PARCELS", "BEACONS"}),
)


def check_bearing_hugs_its_leg(out_dir):
    """A bearing stays in the nearest position to its leg that it will accept.

    Moving a bearing out does clear a collision, and it is what the placer
    would do left to its own devices: an id anchored to its station has fewer
    places to go than a label that can slide along a leg, so letting the
    constrained label go first is the obvious rule. It is the wrong one here.
    A bearing is legible as *that leg's* bearing only because of where it
    sits, while a station id is legible anywhere near its own beacon -- so the
    leg labels claim the strip along the line, and the ids give way.

    Checked on the cadastral sheets, where everything competing for that strip
    is a label that can move. A topographic sheet also has spot heights in it,
    which are pinned to their shots and cannot, so a bearing there is
    sometimes genuinely displaced and the check would be asserting the
    fixture rather than the rule.
    """
    errors = []

    for name, cls, payload, _ in PLACEMENT_CASES:
        if cls is not CadastralPlan:
            continue
        for label, extra in (("bare", {}), ("with schedules", TABLES_ON)):
            plan, doc = _build(cls, payload(**extra), out_dir,
                               f"{name}_hug_{label.split()[0]}")
            segments = _segments(doc)
            if not segments:
                continue

            # The innermost position, plus room for the label's own depth and
            # for the reader to see it has not been nudged.
            limit = plan.leg_label_offset() * 1.2

            for entity in doc.modelspace():
                if entity.dxftype() not in ("TEXT", "MTEXT"):
                    continue
                if entity.dxf.layer in ("TABLES", "TEXT"):
                    continue
                text = entity.dxf.text if entity.dxftype() == "TEXT" else entity.text
                flat = " ".join(text.split())
                if not BEARING.match(flat):
                    continue
                extents = bbox.extents([entity])
                if extents is None or not extents.has_data:
                    continue
                centre = ((extents.extmin.x + extents.extmax.x) / 2,
                          (extents.extmin.y + extents.extmax.y) / 2)

                found = _nearest(centre, segments)
                if found is None:
                    continue
                across = found[1][0]
                if across > limit:
                    errors.append(
                        f"{name} ({label}): {flat!r} sits {across:.2f} from its "
                        f"leg, past the {limit:.2f} it should have kept -- "
                        f"something that could have moved did not")

    return errors


def _stations(plan):
    """The points whose ids the plan draws beside them."""
    boundary = getattr(plan, "topographic_boundary", None) or \
        getattr(plan, "layout_boundary", None)
    if boundary is not None and boundary.coordinates:
        return boundary.coordinates
    return plan.coordinates or []


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_annot_")
    os.makedirs(out_dir, exist_ok=True)
    failures = 0

    for name, fn in (
        ("labels do not collide", check_labels_do_not_collide),
        ("bearings sit nearest their leg", check_bearing_hugs_its_leg),
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
