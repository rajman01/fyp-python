"""North arrow and origin grid regression test.

Run from the repository root:

    python tests/north_arrow_test.py [output_dir]

The arrow is not free-floating sheet furniture: it stands on the origin
easting line and its tip belongs on the frame's top edge, which is where
surveyors expect to find it. That easting is wherever the anchor beacon
happens to be, though, so the arrow can land behind the centred title -- and
it did, printing the arrowhead through the first line of the title block.

So there are two properties here, and fixing either one alone breaks the
sheet: the tip sits on the frame top edge, *and* nothing else is drawn in the
space the arrow occupies.

The origin grid ticks the arrow stands on are checked here too. Each tick
carries the coordinate that is constant along it -- the horizontal tick is a
line of equal northing, the vertical one a line of equal easting -- and the
two used to be written the other way round, which puts an easting against a
line whose easting varies along its whole length. On a document a surveyor
signs, that is worse than untidy.

The values also have to lie along the lines they label. A tick shorter than
its own value left the number hanging in the drawing, which is what "does not
fall directly on the line" meant.
"""

import os
import sys
import tempfile

import ezdxf
from ezdxf import bbox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smoke_test import cadastral_payload, layout_payload, topographic_payload  # noqa: E402
from plans import CadastralPlan, LayoutPlan, TopographicPlan  # noqa: E402

# Title lengths and font sizes both drive the title band's height, which is
# what the arrow has to stay clear of.
LONG_TITLE = ("Plan of <b>A Considerably Longer Property Name</b> "
              "Situate at Eti-Osa Local Government Area")


def _resolve(msp):
    """Yield every entity as actually drawn, block references included."""
    for e in msp:
        if e.dxftype() == "INSERT":
            try:
                yield from e.virtual_entities()
                continue
            except Exception:
                pass
        yield e


def _arrow_box(doc):
    msp = doc.modelspace()
    refs = [e for e in msp
            if e.dxftype() == "INSERT" and e.dxf.name == "NORTH_ARROW"]
    if len(refs) != 1:
        return None, None
    box = bbox.extents(list(refs[0].virtual_entities()))
    return refs[0], box


def check_arrow(out_dir):
    errors = []

    cases = [
        ("cadastral", CadastralPlan, cadastral_payload, {}),
        ("cadastral/long title", CadastralPlan, cadastral_payload,
         {"title": LONG_TITLE, "font_size": 12.0}),
        ("cadastral/max font", CadastralPlan, cadastral_payload,
         {"font_size": 14.0}),
        ("topographic", TopographicPlan, topographic_payload, {}),
        ("layout", LayoutPlan, layout_payload, {}),
    ]

    for name, cls, payload_fn, over in cases:
        plan = cls(**(payload_fn() | over))
        plan.draw()
        path = os.path.join(out_dir, f"{name.replace('/', '_')}.dxf")
        plan.save_dxf(path)

        doc = ezdxf.readfile(path)
        ref, box = _arrow_box(doc)
        if ref is None:
            errors.append(f"{name}: expected exactly one north arrow")
            continue

        frame_top = plan._frame_coords[3]
        frame_bottom = plan._frame_coords[1]
        gap = frame_top - box.extmax.y

        # 1. The tip is on the frame's top edge, not below it.
        if abs(gap) > 1e-6:
            percent = gap / (frame_top - frame_bottom) * 100
            errors.append(
                f"{name}: arrow tip is {gap:.2f} model units "
                f"({percent:.1f}% of the sheet) below the frame top")

        # 2. Nothing shares the space it occupies. The frame itself is
        #    excluded -- the tip is meant to touch that line.
        for e in _resolve(doc.modelspace()):
            if e.dxf.layer == "FRAME":
                continue
            try:
                ext = bbox.extents([e])
            except Exception:
                continue
            if ext is None or not ext.has_data:
                continue
            if (ext.extmin.x > box.extmax.x or ext.extmax.x < box.extmin.x
                    or ext.extmin.y > box.extmax.y or ext.extmax.y < box.extmin.y):
                continue
            # The arrow's own geometry resolves to entities in this sweep.
            if ext.extmin.isclose(box.extmin) and ext.extmax.isclose(box.extmax):
                continue
            if box.extmin.x <= ext.extmin.x and ext.extmax.x <= box.extmax.x \
                    and box.extmin.y <= ext.extmin.y and ext.extmax.y <= box.extmax.y:
                continue  # a part of the arrow block
            text = (getattr(e, "text", "") or getattr(e.dxf, "text", "") or "")
            errors.append(
                f"{name}: {e.dxftype()} on layer {e.dxf.layer} "
                f"overlaps the arrow {text[:40]!r}")

    return errors


def _grid_lines(msp):
    """The blue origin ticks, split by orientation."""
    horizontal, vertical = [], []
    for e in msp:
        if e.dxftype() != "LINE" or e.dxf.color != 5:
            continue
        a, b = e.dxf.start, e.dxf.end
        if abs(a.y - b.y) < 1e-9 and abs(a.x - b.x) > 1e-9:
            horizontal.append(e)
        elif abs(a.x - b.x) < 1e-9 and abs(a.y - b.y) > 1e-9:
            vertical.append(e)
    return horizontal, vertical


def check_origin_grid(out_dir):
    errors = []

    cases = [
        ("cadastral", CadastralPlan, cadastral_payload, {}),
        # A parcel pushed hard against the left margin: the value has to move
        # to the other side rather than print through the drawing.
        ("cadastral/font 12", CadastralPlan, cadastral_payload, {"font_size": 12.0}),
        ("topographic", TopographicPlan, topographic_payload, {}),
        ("layout", LayoutPlan, layout_payload, {}),
    ]

    for name, cls, payload_fn, over in cases:
        plan = cls(**(payload_fn() | over))
        plan.draw()
        path = os.path.join(out_dir, f"grid_{name.replace('/', '_')}.dxf")
        plan.save_dxf(path)

        doc = ezdxf.readfile(path)
        msp = doc.modelspace()
        ref = plan._north_arrow_reference()
        horizontal, vertical = _grid_lines(msp)

        labels = [e for e in msp if e.dxftype() == "TEXT"
                  and (e.dxf.text.endswith("mE") or e.dxf.text.endswith("mN"))]
        if len(labels) != 2:
            errors.append(f"{name}: expected one mE and one mN value, got "
                          f"{[e.dxf.text for e in labels]}")
            continue

        by_suffix = {e.dxf.text[-2:]: e for e in labels}
        if set(by_suffix) != {"mE", "mN"}:
            errors.append(f"{name}: expected one of each, got {list(by_suffix)}")
            continue

        # 1. The values are the anchor beacon's own coordinates.
        if by_suffix["mN"].dxf.text != f"{ref.northing}mN":
            errors.append(f"{name}: northing value is {by_suffix['mN'].dxf.text!r}, "
                          f"expected {ref.northing}mN")
        if by_suffix["mE"].dxf.text != f"{ref.easting}mE":
            errors.append(f"{name}: easting value is {by_suffix['mE'].dxf.text!r}, "
                          f"expected {ref.easting}mE")

        # 2. Each value labels the line along which it is constant: the
        #    northing goes on a horizontal tick, the easting on a vertical one.
        for suffix, lines, kind in (("mN", horizontal, "horizontal"),
                                    ("mE", vertical, "vertical")):
            text = by_suffix[suffix]
            box = bbox.extents([text])
            on_line = False
            for line in lines:
                a, b = line.dxf.start, line.dxf.end
                lo_x, hi_x = sorted((a.x, b.x))
                lo_y, hi_y = sorted((a.y, b.y))
                # Sits along the line's run, and within a text height of it.
                # Measured to whichever edge of the box faces the line -- the
                # value may sit on either side of it.
                gap = (min(abs(box.extmin.y - a.y), abs(box.extmax.y - a.y))
                       if kind == "horizontal" else
                       min(abs(box.extmin.x - a.x), abs(box.extmax.x - a.x)))
                within = (lo_x - 1e-6 <= box.extmin.x and box.extmax.x <= hi_x + text.dxf.height
                          if kind == "horizontal" else
                          lo_y - 1e-6 <= box.extmin.y and box.extmax.y <= hi_y + text.dxf.height)
                if gap <= text.dxf.height and within:
                    on_line = True
                    break
            if not on_line:
                errors.append(
                    f"{name}: {text.dxf.text!r} does not lie along a {kind} tick "
                    f"(box x[{box.extmin.x:.2f},{box.extmax.x:.2f}] "
                    f"y[{box.extmin.y:.2f},{box.extmax.y:.2f}])")

        # 3. Neither value is drawn upside down.
        for text in labels:
            rotation = text.dxf.rotation % 360
            if not (rotation <= 90 + 1e-6 or rotation >= 270 - 1e-6):
                errors.append(f"{name}: {text.dxf.text!r} is rotated {rotation:.0f} "
                              f"degrees and would read upside down")

        # 4. Nothing else is printed through them, and they stay in the frame.
        fl, fb, fr, ft = plan._frame_coords
        for text in labels:
            box = bbox.extents([text])
            if not (fl <= box.extmin.x and box.extmax.x <= fr
                    and fb <= box.extmin.y and box.extmax.y <= ft):
                errors.append(f"{name}: {text.dxf.text!r} runs outside the frame")
            for other in msp:
                if other is text or other.dxftype() not in ("TEXT", "MTEXT"):
                    continue
                try:
                    ob = bbox.extents([other])
                except Exception:
                    continue
                if ob is None or not ob.has_data:
                    continue
                if (ob.extmin.x > box.extmax.x or ob.extmax.x < box.extmin.x
                        or ob.extmin.y > box.extmax.y or ob.extmax.y < box.extmin.y):
                    continue
                label = getattr(other, "text", "") or getattr(other.dxf, "text", "")
                errors.append(f"{name}: {text.dxf.text!r} overlaps {label[:30]!r}")

    return errors


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_arrow_")
    os.makedirs(out_dir, exist_ok=True)

    failures = 0
    for name, fn in (("north arrow placement", check_arrow),
                     ("origin grid ticks", check_origin_grid)):
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
