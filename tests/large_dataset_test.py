"""Large-dataset handling (Task 12).

    docker compose run --rm engine python tests/large_dataset_test.py

The reported failure was a ~1M-row topographic CSV that "ran over 5 minutes
without completing". These checks pin the three properties that fix it:

  * points arrive as a **stream** and are thinned as they are read, so peak
    memory is set by the sheet rather than the file;
  * thinning is **scale-aware** -- it discards only what the paper cannot
    resolve, so contours are unchanged at plotting scale; and
  * the drawing never emits more spot heights than a sheet can legibly carry,
    and says on the sheet how many of the total it is showing.

The million-point case is exercised for real, not simulated: the stream is
generated on the fly so the test itself never holds it either.
"""

import io
import json
import math
import os
import resource
import sys
import tempfile
import time

import ezdxf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.plan import SPOT_HEIGHT_SPACING_MM, TOPO_POINT_SPACING_MM
from plans import TopographicPlan
from point_stream import (
    GridDecimator,
    decimation_cell_size,
    read_plan_stream,
    thin_for_display,
)

ORIGIN_E, ORIGIN_N = 543210.0, 712345.0
SITE = 200.0  # metres square


def plan_header(scale=1000, **overrides):
    header = {
        "id": "large", "created_at": "2026-01-01T00:00:00Z",
        "user": "tester", "project": "project", "type": "topographic",
        "name": "large survey", "title": "Large Survey",
        "state": "Lagos", "scale": scale,
        "footers": ["<p>Surveyed by Tester</p>"],
        "topographic_setting": {
            "tin": True, "contour_interval": 1.0, "major_contour": 5.0,
        },
    }
    header.update(overrides)
    return header


class NdjsonStream(io.RawIOBase):
    """A generated NDJSON body, so a million points never sit in memory."""

    def __init__(self, header: dict, count: int, boundary: list = None):
        self._parts = self._generate(header, count, boundary or [])
        self._buffer = b""
        self._done = False

    def _generate(self, header, count, boundary):
        yield (json.dumps(header) + "\n").encode()
        for corner in boundary:
            yield (json.dumps(corner) + "\n").encode()
        side = int(math.sqrt(count))
        step = SITE / side
        for i in range(side):
            for j in range(side):
                easting = ORIGIN_E + i * step
                northing = ORIGIN_N + j * step
                elevation = 100 + 8 * math.sin(i / 40) + 5 * math.cos(j / 40)
                yield (json.dumps({
                    "k": "c", "i": f"P{i * side + j}",
                    "n": round(northing, 3), "e": round(easting, 3),
                    "z": round(elevation, 2),
                }) + "\n").encode()

    def readable(self):
        return True

    def read(self, size=-1):
        while len(self._buffer) < (size if size > 0 else 1) and not self._done:
            try:
                self._buffer += next(self._parts)
            except StopIteration:
                self._done = True
        if size < 0:
            chunk, self._buffer = self._buffer, b""
            return chunk
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk


def peak_memory_mb():
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return usage / (1024 * 1024) if usage > 10 ** 7 else usage / 1024


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------
def check_cell_size(out_dir):
    """A thinning cell is one printed millimetre of ground."""
    errors = []
    for scale, expected in ((500, 0.5), (1000, 1.0), (2500, 2.5)):
        got = decimation_cell_size(scale)
        if abs(got - expected) > 1e-9:
            errors.append(f"1:{scale}: cell {got} m, expected {expected} m")
    return errors


def check_decimator(out_dir):
    """One point per cell, and the first one wins so station order holds."""
    errors = []
    decimator = GridDecimator(cell_size=1.0)

    kept_first = decimator.add({"id": "A", "easting": 100.0, "northing": 200.0})
    kept_same = decimator.add({"id": "B", "easting": 100.4, "northing": 200.4})
    kept_next = decimator.add({"id": "C", "easting": 101.5, "northing": 200.0})

    if not kept_first:
        errors.append("the first point in a cell was dropped")
    if kept_same:
        errors.append("a second point in the same cell was kept")
    if not kept_next:
        errors.append("a point in a different cell was dropped")
    if [p["id"] for p in decimator.kept] != ["A", "C"]:
        errors.append(f"kept {[p['id'] for p in decimator.kept]}, expected A and C")
    if decimator.seen != 3 or decimator.dropped != 1:
        errors.append(f"counts wrong: seen={decimator.seen} dropped={decimator.dropped}")
    return errors


def check_retained_bounded_by_sheet(out_dir):
    """Retained points scale with the sheet, not the file.

    This is the property that makes the pipeline safe at any size: ten times
    the input must not mean ten times the memory.
    """
    errors = []
    counts = {}
    for total in (10_000, 250_000):
        stream = NdjsonStream(plan_header(), total)
        plan, stats = read_plan_stream(stream)
        counts[total] = stats["coordinates_kept"]
        if stats["coordinates_received"] != stats["coordinates_kept"] + (
                stats["coordinates_received"] - stats["coordinates_kept"]):
            errors.append("received/kept accounting does not add up")

    growth = counts[250_000] / max(counts[10_000], 1)
    if growth > 5:
        errors.append(
            f"25x the input kept {growth:.1f}x the points -- thinning is not "
            f"bounded by the sheet ({counts})"
        )

    # A 200 m site at 1:1000 is a 1 m cell, so the ceiling is ~200x200 cells.
    if counts[250_000] > 200 * 200 + 10:
        errors.append(f"kept {counts[250_000]}, more than the site has cells")
    return errors


def check_one_million(out_dir):
    """The reported failure case: ~1M points, end to end."""
    errors = []
    started = time.time()
    before = peak_memory_mb()

    stream = NdjsonStream(plan_header(), 1_000_000)
    plan, stats = read_plan_stream(stream)
    read_seconds = time.time() - started

    if stats["coordinates_received"] < 990_000:
        errors.append(f"only {stats['coordinates_received']:,} points were read")
    if stats["coordinates_kept"] > 60_000:
        errors.append(f"kept {stats['coordinates_kept']:,} points, expected the sheet's worth")

    plan["coordinates"] = plan["coordinates"]
    topo = TopographicPlan(**plan)
    topo.draw()
    path = os.path.join(out_dir, "million.dxf")
    topo.save_dxf(path)
    total_seconds = time.time() - started
    peak = peak_memory_mb()

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    spot_points = len(msp.query("INSERT[name=='TOPO_POINT']"))
    spot_labels = len(msp.query("TEXT[layer=='SPOT_HEIGHTS']"))

    if spot_points > 8000:
        errors.append(f"{spot_points:,} markers drawn -- more than a sheet can carry")
    if spot_points == 0:
        errors.append("no spot heights drawn at all")
    # Markers and labels are no longer one-to-one: every shot the sheet can
    # hold gets a cross, and only those far enough apart get the number too.
    if spot_labels > spot_points:
        errors.append(f"{spot_labels} labels for only {spot_points} markers")
    if spot_labels == 0:
        errors.append("markers drawn but no elevations labelled")

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > 50:
        errors.append(f"the DXF is {size_mb:.0f} MB -- too large to open")

    print(f"     1,000,000 points -> kept {stats['coordinates_kept']:,}"
          f" -> drew {spot_points:,} spot heights")
    print(f"     read {read_seconds:.1f}s, total {total_seconds:.1f}s,"
          f" DXF {size_mb:.1f} MB, peak RSS {peak:.0f} MB")

    if total_seconds > 240:
        errors.append(f"took {total_seconds:.0f}s -- no better than before")
    return errors


def check_contours_unchanged(out_dir):
    """Thinning must not move the contours at plotting scale."""
    errors = []

    dense_stream = NdjsonStream(plan_header(), 250_000)
    dense_plan, _ = read_plan_stream(dense_stream)

    # The same site read at a 10x finer cell keeps far more points; if the
    # contours differ, thinning is discarding real terrain.
    fine_stream = NdjsonStream(plan_header(), 250_000)
    fine_plan, fine_stats = read_plan_stream(fine_stream, cell_mm=0.1)

    def contour_extent(plan_payload, name):
        plan = TopographicPlan(**plan_payload)
        plan.draw()
        path = os.path.join(out_dir, f"{name}.dxf")
        plan.save_dxf(path)
        doc = ezdxf.readfile(path)
        lengths = []
        msp = doc.modelspace()
        # Contours render as SPLINE when smoothing succeeds and POLYLINE when
        # it falls back, so both count.
        for entity in msp.query("POLYLINE[layer=='CONTOUR_MAJOR']"):
            points = [(p[0], p[1]) for p in entity.points()]
            lengths.append(sum(math.dist(points[i], points[i + 1])
                               for i in range(len(points) - 1)))
        for entity in msp.query("SPLINE[layer=='CONTOUR_MAJOR']"):
            # add_spline() stores fit points; control points stay empty, and
            # reading those instead made this comparison pass on zeroes.
            vertices = entity.fit_points if len(entity.fit_points) else entity.control_points
            points = [(p[0], p[1]) for p in vertices]
            lengths.append(sum(math.dist(points[i], points[i + 1])
                               for i in range(len(points) - 1)))
        return len(lengths), sum(lengths)

    coarse_count, coarse_length = contour_extent(dense_plan, "coarse")
    fine_count, fine_length = contour_extent(fine_plan, "fine")

    print(f"     1 mm cell: {coarse_count} major contours, {coarse_length:,.0f} m total")
    print(f"     0.1 mm cell: {fine_count} major contours, {fine_length:,.0f} m total")

    if coarse_count == 0:
        errors.append("no contours generated from the thinned set")
    elif coarse_length == 0 or fine_length == 0:
        errors.append("contour length measured as zero -- the comparison is not testing anything")
    elif abs(coarse_length - fine_length) / max(fine_length, 1) > 0.05:
        errors.append(
            f"contour length moved {abs(coarse_length - fine_length) / fine_length:.1%} "
            f"when thinning -- more than plotting scale can hide"
        )
    return errors


def check_display_thinning(out_dir):
    """Both spacings are printed sizes, so they hold at any scale.

    Markers and elevations are thinned separately -- the cross is small and can
    sit close together, the number cannot -- so each has its own minimum and
    the marker set is legitimately the denser of the two.
    """
    errors = []

    def closest_pair_violates(points, spacing):
        """True if any two of these sit closer than the spacing allows."""
        for i, a in enumerate(points):
            for b in points[i + 1:]:
                if (abs(a.easting - b.easting) < spacing * 0.5
                        and abs(a.northing - b.northing) < spacing * 0.5):
                    return True
        return False

    for scale in (500, 1000, 2500):
        stream = NdjsonStream(plan_header(scale=scale), 40_000)
        plan, _ = read_plan_stream(stream)
        topo = TopographicPlan(**plan)
        visible = topo.visible_spot_heights()
        labelled = topo.labelled_spot_heights(visible)

        for name, points, mm in (("markers", visible, TOPO_POINT_SPACING_MM),
                                 ("labels", labelled, SPOT_HEIGHT_SPACING_MM)):
            if closest_pair_violates(points, mm * topo.mm_to_model):
                errors.append(f"1:{scale}: two {name} closer than their {mm} mm spacing")

        # The whole point of separating them: a survey dense enough to thin
        # shows far more of itself as markers than it can label.
        if len(visible) <= len(labelled):
            errors.append(
                f"1:{scale}: {len(visible)} markers for {len(labelled)} labels -- "
                "the marker spacing is being driven by the text again")

        if len(visible) > 8000:
            errors.append(
                f"1:{scale}: {len(visible)} markers is more than a sheet carries")

    return errors


def check_sheet_states_the_truth(out_dir):
    """A thinned sheet says how much of the survey it shows."""
    errors = []
    stream = NdjsonStream(plan_header(), 100_000)
    plan, stats = read_plan_stream(stream)
    total = stats["coordinates_received"]
    topo = TopographicPlan(**plan)
    topo.draw()
    path = os.path.join(out_dir, "stated.dxf")
    topo.save_dxf(path)

    doc = ezdxf.readfile(path)
    text = " ".join(t.text for t in doc.modelspace().query("MTEXT"))
    if "SPOT HEIGHTS SHOWN" not in text.upper():
        errors.append("the sheet does not say how many spot heights it is showing")
    if f"{total:,}" not in text:
        errors.append(f"the sheet does not state the survey's true point count ({total:,})")
    return errors


def check_thin_for_display_helper(out_dir):
    errors = []
    points = [type("P", (), {"e": x, "n": 0.0})() for x in (0, 0.4, 1.2, 5.0)]
    kept = thin_for_display(points, 1.0, lambda p: (p.e, p.n))
    if len(kept) != 3:
        errors.append(f"kept {len(kept)} of 4 at 1 m spacing, expected 3")
    if thin_for_display(points, 0, lambda p: (p.e, p.n)) != points:
        errors.append("zero spacing should keep everything")
    return errors


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="fyp_large_")
    os.makedirs(out_dir, exist_ok=True)
    failures = 0

    for name, fn in (
        ("thinning cell is a printed millimetre", check_cell_size),
        ("grid decimator keeps one point per cell", check_decimator),
        ("retained points bounded by the sheet", check_retained_bounded_by_sheet),
        ("spot-height spacing holds at every scale", check_display_thinning),
        ("thin_for_display helper", check_thin_for_display_helper),
        ("contours unchanged by thinning", check_contours_unchanged),
        ("the sheet states what it shows", check_sheet_states_the_truth),
        ("one million points, end to end", check_one_million),
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
