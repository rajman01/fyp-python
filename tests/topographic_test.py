"""Topographic plan regression tests.

Run from the repository root:

    python tests/topographic_test.py [output_dir]

Covers two surveyor-feedback fixes:

  * Spot heights must render on the map. Asserts the SPOT_HEIGHTS layer holds
    the expected point + label entities and is switched on, and that the
    ``show_spot_heights`` toggle actually hides them.
  * The contour interval must be labelled on the sheet, and an invalid
    interval (<= 0) must be rejected with a clear message.
"""

import math
import os
import sys
import tempfile

import ezdxf
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plans import TopographicPlan
from models.plan import TopographicSettingProps

BASE = {
    "id": "topo-test",
    "created_at": "2026-01-01T00:00:00Z",
    "user": "tester",
    "project": "project",
    "type": "topographic",
    "title": "Topographic Plan Of Test Site",
    "address": "1 Example Close",
    "local_govt": "Eti-Osa",
    "state": "Lagos",
    "scale": 1000,
    "footers": ["<p>Surveyed by <b>Tester</b></p>"],
}

# 6 x 6 grid of spot heights = 36 points.
GRID_N = 6
SQUARE = [
    ("PB1", 543210.0, 712345.0),
    ("PB2", 543310.0, 712345.0),
    ("PB3", 543310.0, 712425.0),
    ("PB4", 543210.0, 712425.0),
]


def spot_points():
    coords = []
    n = 0
    for i in range(GRID_N):
        for j in range(GRID_N):
            e = 543210.0 + i * 20
            no = 712345.0 + j * 16
            z = 100 + 3 * math.sin(i / 2) + 2 * math.cos(j / 2)
            n += 1
            coords.append({"id": f"T{n}", "easting": e, "northing": no, "elevation": round(z, 2)})
    return coords


def topo_payload(**setting_overrides):
    settings = {
        "tin": True,
        "grid": False,
        "contour_interval": 0.5,
        "major_contour": 2.0,
    }
    settings.update(setting_overrides)
    return BASE | {
        "name": "topo test",
        "coordinates": spot_points(),
        "topographic_boundary": {
            "coordinates": [{"id": i, "easting": e, "northing": n} for i, e, n in SQUARE],
            "area": 8000.0,
        },
        "topographic_setting": settings,
    }


def _build(out_dir, name, **setting_overrides):
    plan = TopographicPlan(**topo_payload(**setting_overrides))
    plan.draw()
    path = os.path.join(out_dir, f"{name}.dxf")
    plan.save_dxf(path)
    return ezdxf.readfile(path)


def _spot_counts(doc):
    msp = doc.modelspace()
    inserts = sum(1 for e in msp if e.dxftype() == "INSERT" and e.dxf.name == "TOPO_POINT")
    labels = sum(1 for e in msp.query("TEXT") if e.dxf.layer == "SPOT_HEIGHTS")
    return inserts, labels


def _title_block_text(doc):
    return "\n".join(e.text for e in doc.modelspace().query("MTEXT"))


def check_spot_heights(out_dir):
    errors = []
    expected = GRID_N * GRID_N  # 36

    # Enabled: points + labels present, layer on.
    doc = _build(out_dir, "topo_spot_on", show_spot_heights=True)
    inserts, labels = _spot_counts(doc)
    if inserts != expected:
        errors.append(f"expected {expected} spot-height points, got {inserts}")
    if labels != expected:
        errors.append(f"expected {expected} spot-height labels, got {labels}")
    if not doc.layers.get("SPOT_HEIGHTS").is_on():
        errors.append("SPOT_HEIGHTS layer should be on when show_spot_heights=True")

    # Disabled: entities still drawn but the layer is switched off (hidden).
    doc_off = _build(out_dir, "topo_spot_off", show_spot_heights=False)
    if doc_off.layers.get("SPOT_HEIGHTS").is_on():
        errors.append("SPOT_HEIGHTS layer should be off when show_spot_heights=False")

    print(f"  spot heights: {inserts} points + {labels} labels, layer on")
    return errors


def check_contour_interval_label(out_dir):
    errors = []
    doc = _build(out_dir, "topo_interval", tin=True, show_contours=True, contour_interval=0.5)
    text = _title_block_text(doc)
    if "CONTOUR INTERVAL :- 0.5 M" not in text:
        errors.append(f"contour interval note missing from title block; got: {text!r}")

    # No note when contours are not drawn.
    doc_none = _build(out_dir, "topo_no_contours", tin=False, grid=False, show_contours=False)
    if "CONTOUR INTERVAL" in _title_block_text(doc_none):
        errors.append("contour interval note should be absent when no contours are drawn")

    print("  contour interval label: present when drawn, absent otherwise")
    return errors


def check_interval_validation():
    errors = []
    for bad in (0, -1, -0.5):
        try:
            TopographicSettingProps(show_contours=True, contour_interval=bad, major_contour=2.0)
            errors.append(f"contour_interval={bad} should have been rejected")
        except ValidationError:
            pass
    try:
        TopographicSettingProps(show_contours=True, contour_interval=0.5, major_contour=0)
        errors.append("major_contour=0 should have been rejected")
    except ValidationError:
        pass
    # Valid settings must still pass.
    try:
        TopographicSettingProps(show_contours=True, contour_interval=0.5, major_contour=2.0)
    except ValidationError as e:
        errors.append(f"valid contour settings rejected: {e}")

    print("  interval validation: rejects <= 0, accepts positive")
    return errors


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_topo_")
    os.makedirs(out_dir, exist_ok=True)
    failures = 0

    for name, fn in (
        ("spot heights", lambda: check_spot_heights(out_dir)),
        ("contour interval label", lambda: check_contour_interval_label(out_dir)),
        ("interval validation", check_interval_validation),
    ):
        print(f"== {name} ==")
        errors = fn()
        for e in errors:
            failures += 1
            print("  FAIL:", e)
        if not errors:
            print("  OK")

    print(f"\nOutput directory: {out_dir}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
