"""Streaming point ingest with scale-aware decimation (Task 12).

A GNSS or LiDAR survey can carry millions of points. Sending them as one JSON
body means the sender serialises the lot, the receiver parses the lot, and both
hold it in memory at once — which is why a million-row file never finished.

Instead the API streams **NDJSON**: the plan payload on the first line, then one
point per line. This module reads that stream and thins the points *as they
arrive*, so nothing ever holds the whole survey.

The thinning is the important part, and it is not a compromise. Geometry is
drawn at true ground coordinates and plotted at a known scale, so a point's
contribution to the sheet is bounded by what the scale can resolve: at 1:1000,
one millimetre of paper is one metre of ground, and two points a centimetre
apart on the ground land on the same speck of ink. Binning to one point per
millimetre-of-paper cell therefore changes nothing a plotter could print, while
bounding retained points by the *sheet area* rather than the file size — an A4
sheet holds about 44,000 one-millimetre cells whether the input has ten
thousand points or ten million.
"""

import json
import logging
import math
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Cell size for thinning, as printed millimetres. One millimetre is the finest
#: detail a plotted sheet resolves; anything finer is invisible.
DECIMATION_CELL_MM = 1.0

#: Hard ceiling on retained points per series, as a backstop for a drawing whose
#: declared scale is wildly out of step with its extent.
MAX_RETAINED_POINTS = 400_000

#: Keys used on the wire. Short deliberately: at a million points the
#: difference between ``northing`` and ``n`` is about 20 MB.
KEY_KIND = "k"
KEY_ID = "i"
KEY_NORTHING = "n"
KEY_EASTING = "e"
KEY_ELEVATION = "z"

KIND_COORDINATES = "c"
KIND_BOUNDARY = "b"


class PointStreamError(Exception):
    """The stream was malformed."""


class GridDecimator:
    """Keeps one point per cell of a square grid.

    The first point to land in a cell wins. Alternatives -- nearest to the cell
    centre, or the extreme value -- were considered and rejected: a survey's
    points arrive in the order they were walked, so "first" preserves the
    original station order within the kept set, which keeps recovered ids and
    traverse order meaningful. For contouring the choice is immaterial, since
    every candidate lies inside the same one-millimetre cell.
    """

    def __init__(self, cell_size: float, limit: int = MAX_RETAINED_POINTS):
        self.cell_size = cell_size if cell_size > 0 else 1.0
        self.limit = limit
        self._cells: Dict[Tuple[int, int], int] = {}
        self.kept: List[dict] = []
        self.seen = 0
        self.capped = False

    def add(self, point: dict) -> bool:
        """Offer a point; returns True when it was kept."""
        self.seen += 1

        easting = point.get("easting")
        northing = point.get("northing")
        if easting is None or northing is None:
            return False

        key = (int(math.floor(easting / self.cell_size)),
               int(math.floor(northing / self.cell_size)))
        if key in self._cells:
            return False

        if len(self.kept) >= self.limit:
            self.capped = True
            return False

        self._cells[key] = len(self.kept)
        self.kept.append(point)
        return True

    @property
    def dropped(self) -> int:
        return self.seen - len(self.kept)


def decimation_cell_size(scale: float, cell_mm: float = DECIMATION_CELL_MM) -> float:
    """Ground size, in metres, of a cell that prints at ``cell_mm``."""
    scale = scale or 1000
    return cell_mm * scale / 1000.0


def _point_from_line(record: dict) -> dict:
    return {
        "id": record.get(KEY_ID) or "",
        "northing": record.get(KEY_NORTHING),
        "easting": record.get(KEY_EASTING),
        "elevation": record.get(KEY_ELEVATION, 0.0) or 0.0,
    }


def iter_lines(stream, chunk_size: int = 1 << 20) -> Iterator[str]:
    """Yield complete lines from a byte stream without buffering all of it."""
    carry = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        carry += chunk
        *lines, carry = carry.split(b"\n")
        for line in lines:
            if line:
                yield line.decode("utf-8")
    if carry:
        yield carry.decode("utf-8")


def read_plan_stream(stream, cell_mm: float = DECIMATION_CELL_MM,
                     on_progress: Optional[Callable[[int], None]] = None) -> Tuple[dict, dict]:
    """Read an NDJSON plan stream into a plan payload.

    The first line is the plan itself, carrying every field *except* the bulk
    point arrays. Each following line is one point, tagged with the series it
    belongs to. Points are thinned as they arrive, so peak memory is set by the
    sheet, not the file.

    Returns ``(plan_payload, stats)``.
    """
    lines = iter_lines(stream)

    try:
        header = next(lines)
    except StopIteration:
        raise PointStreamError("The request body was empty") from None

    try:
        plan = json.loads(header)
    except json.JSONDecodeError as exc:
        raise PointStreamError(f"The first line is not a valid plan payload: {exc}") from exc
    if not isinstance(plan, dict):
        raise PointStreamError("The first line must be a plan object")

    cell = decimation_cell_size(plan.get("scale") or 1000, cell_mm)
    decimators = {
        KIND_COORDINATES: GridDecimator(cell),
        KIND_BOUNDARY: GridDecimator(cell),
    }

    malformed = 0
    read = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue

        decimator = decimators.get(record.get(KEY_KIND, KIND_COORDINATES))
        if decimator is None:
            malformed += 1
            continue
        decimator.add(_point_from_line(record))

        read += 1
        if on_progress is not None:
            on_progress(read)

    coordinates = decimators[KIND_COORDINATES]
    boundary = decimators[KIND_BOUNDARY]

    # A boundary is a handful of corners that define the parcel; thinning it
    # would move them. It only ever passes through the decimator so the two
    # series can share one reader.
    if coordinates.kept:
        plan["coordinates"] = coordinates.kept
    if boundary.kept:
        _attach_boundary(plan, boundary.kept)

    stats = {
        "cell_size": cell,
        "coordinates_received": coordinates.seen,
        "coordinates_kept": len(coordinates.kept),
        "boundary_received": boundary.seen,
        "boundary_kept": len(boundary.kept),
        "malformed": malformed,
        "capped": coordinates.capped or boundary.capped,
    }

    # The drawing says how much of the survey it is showing, so a thinned sheet
    # is never mistaken for the whole dataset.
    plan["point_totals"] = {
        "coordinates": coordinates.seen,
        "boundary": boundary.seen,
    }

    if coordinates.dropped:
        logger.info(
            "thinned %s points to %s at a %.3f m cell (1:%s)",
            f"{coordinates.seen:,}", f"{len(coordinates.kept):,}", cell,
            int(plan.get("scale") or 1000),
        )

    return plan, stats


def _attach_boundary(plan: dict, points: List[dict]) -> None:
    """Put boundary points on whichever boundary the plan type uses."""
    plan_type = plan.get("type")
    key = "layout_boundary" if plan_type == "layout" else "topographic_boundary"
    boundary = plan.get(key)
    if not isinstance(boundary, dict):
        boundary = {}
        plan[key] = boundary
    boundary["coordinates"] = points


def thin_for_display(points: Iterable, spacing: float,
                     position: Callable[[object], Tuple[float, float]]) -> List:
    """Thin a series so no two kept items are closer than ``spacing``.

    Used for spot-height labels, which have a hard legibility limit no amount
    of scaling changes: an A4 sheet holds roughly 1,800 readable elevation
    labels, so drawing a million produces an unopenable file rendering an
    unreadable sheet.

    This enforces a genuine minimum separation rather than one item per grid
    cell. Cell-based thinning gives the right *density* but still lets two
    items sit either side of a cell boundary, touching -- which is exactly the
    overlap the thinning exists to prevent. A cell index is still used to find
    candidates, so each point is only compared against the handful already kept
    in its own and adjoining cells.
    """
    if spacing <= 0:
        return list(points)

    kept: List = []
    cells: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
    neighbourhood = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]

    for point in points:
        x, y = position(point)
        if x is None or y is None:
            continue

        cx, cy = int(math.floor(x / spacing)), int(math.floor(y / spacing))
        crowded = False
        for dx, dy in neighbourhood:
            for px, py in cells.get((cx + dx, cy + dy), ()):
                if math.hypot(x - px, y - py) < spacing:
                    crowded = True
                    break
            if crowded:
                break

        if crowded:
            continue

        cells.setdefault((cx, cy), []).append((x, y))
        kept.append(point)

    return kept
