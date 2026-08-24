"""Putting annotation where it can actually be read.

Every label on a survey plan is anchored to something -- a beacon id to its
beacon, a bearing to the leg it describes -- but the anchor only says roughly
where the text belongs, not exactly. Writing each one at the first position
that comes to hand is what puts a distance on top of a station id, or a
bearing across the parcel line it is annotating: nothing ever asks whether
the spot is already taken.

So this module keeps a record of what the sheet is already using, and lets a
caller offer a short list of positions it would accept, best first. The
geometry is exact rather than approximate. Leg labels rotate to follow their
legs, and testing a rotated label by its axis-aligned bounding box reserves
up to twice the area it really occupies -- on a busy sheet that is the
difference between "there is no room" and a perfectly good gap.
"""
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import math

Point = Tuple[float, float]
Poly = Sequence[Point]


# ----------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------
def rect_corners(cx: float, cy: float, width: float, height: float,
                 angle_deg: float = 0.0) -> List[Point]:
    """The four corners of a box of ``width`` x ``height`` centred on
    (``cx``, ``cy``) and turned ``angle_deg`` degrees anticlockwise."""
    angle = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    half_w, half_h = width / 2.0, height / 2.0
    return [
        (cx + cos_a * dx - sin_a * dy, cy + sin_a * dx + cos_a * dy)
        for dx, dy in ((-half_w, -half_h), (half_w, -half_h),
                       (half_w, half_h), (-half_w, half_h))
    ]


def bounds(poly: Poly) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _axes(poly: Poly) -> Iterable[Point]:
    """One outward normal per edge -- the candidate separating axes.

    A two-point ``poly`` is a line segment: it has a single edge, so it
    contributes one axis and the loop stops there rather than handing back
    the same line twice.
    """
    count = len(poly)
    for index in range(count):
        x1, y1 = poly[index]
        x2, y2 = poly[(index + 1) % count]
        edge_x, edge_y = x2 - x1, y2 - y1
        length = math.hypot(edge_x, edge_y)
        if length > 1e-12:
            yield (-edge_y / length, edge_x / length)
        if count == 2:
            return


def _span(poly: Poly, axis: Point) -> Tuple[float, float]:
    dots = [p[0] * axis[0] + p[1] * axis[1] for p in poly]
    return min(dots), max(dots)


def overlaps(first: Poly, second: Poly) -> bool:
    """Do two convex shapes share any area? A segment counts as a shape.

    The separating-axis test: two convex shapes miss each other if and only
    if some edge normal of one of them projects them into disjoint intervals.
    """
    for axis in list(_axes(first)) + list(_axes(second)):
        first_lo, first_hi = _span(first, axis)
        second_lo, second_hi = _span(second, axis)
        if first_hi <= second_lo or second_hi <= first_lo:
            return False
    return True


# ----------------------------------------------------------------------
# The sheet
# ----------------------------------------------------------------------
class LabelSpace:
    """What the drawing has already claimed, indexed by a uniform grid.

    A plan can carry thousands of shapes and as many labels, and testing each
    candidate position against all of them is quadratic. The grid narrows
    every test to the handful of shapes near the position being considered.
    """

    def __init__(self, cell: float):
        #: Sized to the labels being placed, not to the sheet: a cell much
        #: smaller than a label files it under many keys, and one much larger
        #: hands back neighbours that were never close.
        self._cell = cell if cell > 0 else 1.0
        self._cells: Dict[Tuple[int, int], List[Poly]] = {}

    # -- filling it in ------------------------------------------------
    def reserve(self, poly: Poly) -> None:
        """Mark a convex shape as taken."""
        poly = list(poly)
        for key in self._keys(bounds(poly)):
            self._cells.setdefault(key, []).append(poly)

    def reserve_outline(self, points: Sequence[Point], closed: bool = False) -> None:
        """Mark a polyline -- a parcel edge, a boundary, a road."""
        pts = [(p[0], p[1]) for p in points]
        if len(pts) < 2:
            return
        edges = zip(pts, pts[1:] + pts[:1]) if closed else zip(pts, pts[1:])
        for start, end in edges:
            self._reserve_segment(start, end)

    def _reserve_segment(self, start: Point, end: Point) -> None:
        # A long segment crosses far more cells than its two endpoints fall
        # in, and filing it under the bounding box of the whole thing would
        # put a diagonal parcel edge in every cell of the sheet. Cut it into
        # cell-sized pieces so each piece is filed where it actually lies.
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(1, int(length / self._cell) + 1)
        for step in range(steps):
            t_start, t_end = step / steps, (step + 1) / steps
            self.reserve([
                (start[0] + (end[0] - start[0]) * t_start,
                 start[1] + (end[1] - start[1]) * t_start),
                (start[0] + (end[0] - start[0]) * t_end,
                 start[1] + (end[1] - start[1]) * t_end),
            ])

    # -- asking about it ----------------------------------------------
    def conflicts(self, poly: Poly) -> int:
        """How many reserved shapes a position would run into."""
        poly = list(poly)
        seen = set()
        hits = 0
        for key in self._keys(bounds(poly)):
            for other in self._cells.get(key, ()):
                marker = id(other)
                if marker in seen:
                    continue
                seen.add(marker)
                if overlaps(poly, other):
                    hits += 1
        return hits

    def is_clear(self, poly: Poly) -> bool:
        return self.conflicts(poly) == 0

    def place(self, candidates: Sequence[Poly],
              crowded_ok: bool = True) -> Optional[int]:
        """Index of the first candidate that lands on empty sheet.

        With nowhere clear, ``crowded_ok`` decides between the least crowded
        position and no position at all. Which of those is right depends on
        whether the label is the only copy of its figure -- see the callers.
        """
        fallback: Optional[Tuple[int, int]] = None
        for index, poly in enumerate(candidates):
            hits = self.conflicts(poly)
            if hits == 0:
                return index
            if fallback is None or hits < fallback[0]:
                fallback = (hits, index)
        if crowded_ok and fallback is not None:
            return fallback[1]
        return None

    def _keys(self, box: Tuple[float, float, float, float]) -> Iterable[Tuple[int, int]]:
        min_x, min_y, max_x, max_y = box
        cell = self._cell
        for i in range(int(math.floor(min_x / cell)), int(math.floor(max_x / cell)) + 1):
            for j in range(int(math.floor(min_y / cell)), int(math.floor(max_y / cell)) + 1):
                yield (i, j)
