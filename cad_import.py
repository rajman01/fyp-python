"""Extract survey data from a legacy CAD drawing (Task 11).

The use case is restoring old drawings: the surveyor holds a DWG and nothing
else -- no DXF, no CSV, no coordinate register -- and needs the survey data out
of a sheet that was only ever meant to be printed.

Two principles shape this module.

**Recompute, don't parse.** Geometry is ground truth; the text on the sheet is
a derived label. Bearings, distances and areas are *never* read from the
drawing's annotation -- they are recomputed from the geometry downstream by the
API's ``backComputation``. That removes any need to understand ``090° 00'`` vs
``N90°00'E`` vs ``90-00-00``, and makes the result self-consistent instead of
trusting thirty-year-old typing. Text is read only for what geometry cannot
provide: station ids and spot elevations carried as labels.

**Report, don't guess.** A legacy boundary may be a polyline, an old-style
polyline, a spline, loose line segments that miss closure by millimetres, or
any of those nested inside blocks. Every plausible ring is extracted and
returned with its layer, area and vertex count so the user can choose, rather
than a layer-name heuristic silently picking the wrong one.
"""

import logging
import math
import os
import re
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import ezdxf
import ezdxf.edgeminer as edgeminer
import ezdxf.edgesmith as edgesmith
from ezdxf.addons import odafc
from ezdxf.document import Drawing

from models.cad import (
    INSUNITS_NAMES,
    INSUNITS_TO_METRES,
    CadInspection,
    CadLabel,
    CadLayer,
    CadPoint,
    CadRing,
    CadStation,
    CadVertex,
    RingSource,
)

logger = logging.getLogger(__name__)

#: Largest gap, in drawing units, that may be bridged when chaining loose
#: segments into a ring. Old drawings routinely miss closure by a hair; a
#: tolerance far larger than that would start inventing boundaries.
DEFAULT_GAP_TOL = 0.02

#: Loop finding is a recursive backtracking search with O(n!) worst case, so it
#: is only attempted on layers small enough to stay tractable and is always run
#: under a timeout. Bigger layers fall back to sequential chaining, which is
#: linear.
MAX_EDGES_FOR_LOOP_SEARCH = 120
LOOP_SEARCH_TIMEOUT = 5.0

#: Guards against pathological drawings.
MAX_ENTITIES = 200_000
MAX_BLOCK_DEPTH = 8
MAX_RINGS = 60
MAX_POINTS = 20_000
MAX_LABELS = 20_000

#: Chord height used when flattening curves into vertices, in drawing units.
CURVE_FLATTENING_DISTANCE = 0.05

#: A label is taken to belong to the nearest point feature within this multiple
#: of the label's text height. Station ids sit right against their symbol.
LABEL_SEARCH_HEIGHTS = 4.0

#: Rings smaller than this (square metres) are dropped -- they are hatch
#: islands or annotation boxes, not parcels.
MIN_RING_AREA = 1.0

#: A block inserted at least this many times is a symbol -- a beacon marker, a
#: tree, a manhole -- so the geometry inside it is furniture rather than survey
#: geometry and is not offered as a candidate boundary. A boundary stored in a
#: block is placed once, so it is unaffected.
SYMBOL_BLOCK_MIN_REFS = 3


class CadImportError(Exception):
    """A drawing could not be read or holds nothing usable."""


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------
def read_drawing(path: str) -> Tuple[Drawing, str]:
    """Open a DWG or DXF and return ``(document, format)``.

    DWG is a binary format that ezdxf cannot read directly; the ODA File
    Converter turns it into a DXF first. ODA ships in the service image -- it
    is the same converter already used to write the DWG half of every export.
    """
    extension = os.path.splitext(path)[1].lower().lstrip(".")

    if extension == "dwg":
        if not odafc.is_installed():
            raise CadImportError(
                "DWG support needs the ODA File Converter, which is not installed "
                "on this server. Convert the drawing to DXF and upload that instead."
            )
        try:
            return odafc.readfile(path), "dwg"
        except IOError as exc:
            raise CadImportError(f"Could not read the DWG file: {exc}") from exc
        except odafc.ODAFCError as exc:
            raise CadImportError(
                f"The DWG file could not be converted, so it may be corrupt "
                f"or an unsupported version: {exc}"
            ) from exc

    if extension == "dxf":
        try:
            return ezdxf.readfile(path), "dxf"
        except IOError as exc:
            raise CadImportError(f"Could not read the DXF file: {exc}") from exc
        except ezdxf.DXFStructureError as exc:
            raise CadImportError(f"The DXF file is not valid: {exc}") from exc

    raise CadImportError(f"Unsupported file type '.{extension}'. Upload a DWG or DXF.")


def detect_units(doc: Drawing) -> Tuple[int, str, float]:
    """Drawing units from ``$INSUNITS``, as ``(code, name, metres_factor)``."""
    try:
        code = int(doc.header.get("$INSUNITS", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    return code, INSUNITS_NAMES.get(code, f"code {code}"), INSUNITS_TO_METRES.get(code, 1.0)


# ----------------------------------------------------------------------
# Flattening
# ----------------------------------------------------------------------
class FlatEntity(NamedTuple):
    """A drawable entity plus the block it came out of, if any.

    Provenance matters: geometry that came from a block inserted many times is
    a repeated symbol, and offering four beacon markers as candidate parcels
    would bury the one ring the user actually wants.
    """
    entity: object
    block: str = ""


def count_block_references(msp) -> Dict[str, int]:
    """How many times each block is placed in modelspace."""
    counts: Dict[str, int] = {}
    for entity in msp.query("INSERT"):
        name = entity.dxf.name or ""
        counts[name] = counts.get(name, 0) + 1
    return counts


def flatten_entities(msp, warnings: List[str]) -> List[FlatEntity]:
    """All drawable entities with block references exploded into place.

    A legacy boundary is as likely to live inside a block -- often nested --
    as in modelspace, and ``virtual_entities()`` returns the block's contents
    already transformed into world coordinates.
    """
    flattened: List[FlatEntity] = []
    stack: List[Tuple[object, int, str]] = [(entity, 0, "") for entity in msp]
    truncated = False

    while stack:
        entity, depth, block = stack.pop()
        if len(flattened) >= MAX_ENTITIES:
            truncated = True
            break

        if entity.dxftype() == "INSERT":
            name = entity.dxf.name or ""
            if depth < MAX_BLOCK_DEPTH:
                try:
                    stack.extend((child, depth + 1, block or name)
                                 for child in entity.virtual_entities())
                except Exception as exc:  # corrupt or unresolvable block reference
                    logger.warning("could not explode block %s: %s", name, exc)
            # The insert point itself is a feature: beacon symbols are blocks.
            flattened.append(FlatEntity(entity, block))
            continue

        flattened.append(FlatEntity(entity, block))

    if truncated:
        warnings.append(
            f"The drawing holds more than {MAX_ENTITIES:,} entities; only the first "
            f"{MAX_ENTITIES:,} were read."
        )
    return flattened


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------
def _polygon_area(points: Sequence[Tuple[float, float]]) -> float:
    """Unsigned shoelace area."""
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _perimeter(points: Sequence[Tuple[float, float]]) -> float:
    return sum(math.dist(points[i], points[(i + 1) % len(points)])
               for i in range(len(points)))


def _centroid(points: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _dedupe_ring(points: Sequence[Tuple[float, float]],
                 tol: float) -> List[Tuple[float, float]]:
    """Drop consecutive duplicates and any repeated closing vertex.

    A ring is stored open -- the closing edge is implied -- which is the
    convention the plan pipeline uses for parcels.
    """
    cleaned: List[Tuple[float, float]] = []
    for point in points:
        if not cleaned or math.dist(cleaned[-1], point) > tol:
            cleaned.append(point)
    while len(cleaned) > 1 and math.dist(cleaned[0], cleaned[-1]) <= tol:
        cleaned.pop()
    return cleaned


def _entity_ring_points(entity, tol: float) -> Optional[List[Tuple[float, float]]]:
    """Vertices of an already-closed entity, or ``None`` if it is not one."""
    kind = entity.dxftype()

    if kind == "LWPOLYLINE":
        if not entity.closed:
            return None
        return [(float(x), float(y)) for x, y in entity.get_points("xy")]

    if kind == "POLYLINE":
        if not entity.is_closed or not entity.is_2d_polyline:
            return None
        return [(float(v.dxf.location.x), float(v.dxf.location.y))
                for v in entity.vertices]

    if kind == "SPLINE":
        if not entity.closed:
            return None
        return [(float(p.x), float(p.y))
                for p in entity.flattening(CURVE_FLATTENING_DISTANCE)]

    return None


# ----------------------------------------------------------------------
# Ring extraction
# ----------------------------------------------------------------------
def _rings_from_closed_entities(entities: Iterable[FlatEntity],
                                tol: float) -> List[Tuple[str, List[Tuple[float, float]]]]:
    rings = []
    for entity, _block in entities:
        try:
            points = _entity_ring_points(entity, tol)
        except Exception as exc:
            logger.warning("could not read %s: %s", entity.dxftype(), exc)
            continue
        if not points:
            continue
        cleaned = _dedupe_ring(points, tol)
        if len(cleaned) >= 3:
            rings.append((entity.dxf.layer, cleaned))
    return rings


def _rings_from_loose_edges(entities: Sequence[FlatEntity], tol: float,
                            warnings: List[str]) -> List[Tuple[str, List[Tuple[float, float]], float]]:
    """Chain loose segments into closed rings, one layer at a time.

    Searching per layer keeps the loop finder tractable: it is a recursive
    backtracking search, so throwing a whole messy drawing at it can hang. A
    boundary is drawn on one layer in practice, so this loses nothing.
    """
    by_layer: Dict[str, List] = {}
    for entity, _block in entities:
        if entity.dxftype() in ("LINE", "ARC", "LWPOLYLINE", "POLYLINE", "SPLINE"):
            by_layer.setdefault(entity.dxf.layer, []).append(entity)

    found: List[Tuple[str, List[Tuple[float, float]], float]] = []
    for layer, layer_entities in by_layer.items():
        try:
            edges = list(edgesmith.edges_from_entities_2d(layer_entities, gap_tol=tol))
        except Exception as exc:
            logger.warning("could not build edges for layer %s: %s", layer, exc)
            continue
        if len(edges) < 3:
            continue
        if len(edges) > MAX_EDGES_FOR_LOOP_SEARCH:
            warnings.append(
                f"Layer '{layer}' holds {len(edges)} separate segments, too many to "
                f"search for closed shapes; any boundary drawn there as loose lines "
                f"was not rebuilt."
            )
            continue

        try:
            deposit = edgeminer.Deposit(edges, gap_tol=tol)
            loops = edgeminer.find_all_loops(deposit, timeout=LOOP_SEARCH_TIMEOUT)
        except edgeminer.TimeoutError:
            warnings.append(
                f"Searching layer '{layer}' for closed shapes timed out; any boundary "
                f"drawn there as loose lines was not rebuilt."
            )
            continue
        except Exception as exc:
            logger.warning("loop search failed on layer %s: %s", layer, exc)
            continue

        for loop in loops:
            try:
                vertices = list(edgesmith.chain_vertices(loop, gap_tol=tol))
            except Exception:
                continue
            points = _dedupe_ring([(float(v.x), float(v.y)) for v in vertices], tol)
            if len(points) < 3:
                continue
            found.append((layer, points, _largest_gap(loop)))

    return found


def _largest_gap(loop: Sequence) -> float:
    """Widest gap the chain had to bridge, in drawing units.

    Reported so the user can see that a ring was closed across a real break in
    the drawing rather than being genuinely closed.
    """
    gaps = []
    for index in range(len(loop)):
        end = loop[index].end
        start = loop[(index + 1) % len(loop)].start
        gaps.append(math.dist((end.x, end.y), (start.x, start.y)))
    return max(gaps, default=0.0)


def _collect_rings(entities: Sequence[FlatEntity], tol: float, scale: float,
                   block_refs: Dict[str, int], warnings: List[str]) -> List[CadRing]:
    """Every plausible boundary in the drawing, largest first.

    Geometry from a repeated block is skipped: a beacon marker placed at every
    corner would otherwise contribute a candidate ring per corner and bury the
    boundary the user is looking for.
    """
    survey_entities = [
        record for record in entities
        if block_refs.get(record.block, 0) < SYMBOL_BLOCK_MIN_REFS
    ]
    skipped = len(entities) - len(survey_entities)
    if skipped:
        logger.info("skipped %d entities belonging to repeated symbol blocks", skipped)

    candidates: List[Tuple[str, List[Tuple[float, float]], RingSource, Optional[float]]] = []

    for layer, points in _rings_from_closed_entities(survey_entities, tol):
        candidates.append((layer, points, RingSource.POLYLINE, None))

    closed_signatures = {_ring_signature(points) for _, points, _, _ in candidates}
    for layer, points, gap in _rings_from_loose_edges(survey_entities, tol, warnings):
        if _ring_signature(points) in closed_signatures:
            continue  # the same shape already found as a single closed entity
        candidates.append((layer, points, RingSource.CHAINED, gap))

    rings: List[CadRing] = []
    for index, (layer, points, source, gap) in enumerate(candidates):
        scaled = [(x * scale, y * scale) for x, y in points]
        area = _polygon_area(scaled)
        if area < MIN_RING_AREA:
            continue
        cx, cy = _centroid(scaled)
        rings.append(CadRing(
            id=f"ring-{index + 1}",
            layer=layer or "0",
            source=source,
            vertices=[CadVertex(easting=x, northing=y) for x, y in scaled],
            area=round(area, 3),
            perimeter=round(_perimeter(scaled), 3),
            centroid_easting=round(cx, 3),
            centroid_northing=round(cy, 3),
            gap_closed=None if gap is None else round(gap * scale, 4),
        ))

    rings.sort(key=lambda r: r.area, reverse=True)
    if len(rings) > MAX_RINGS:
        warnings.append(
            f"Found {len(rings)} closed shapes; showing the {MAX_RINGS} largest."
        )
        rings = rings[:MAX_RINGS]

    # Re-id after sorting so the numbering the user sees runs largest first.
    for index, ring in enumerate(rings):
        ring.id = f"ring-{index + 1}"
    return rings


def _ring_signature(points: Sequence[Tuple[float, float]]) -> Tuple:
    """Order-independent fingerprint, so the same shape found twice (once as a
    closed polyline, once by chaining its segments) is only offered once."""
    return tuple(sorted((round(x, 4), round(y, 4)) for x, y in points))


# ----------------------------------------------------------------------
# Points and labels
# ----------------------------------------------------------------------
def _collect_labels(entities: Sequence[FlatEntity], scale: float) -> List[CadLabel]:
    labels: List[CadLabel] = []
    for entity, _block in entities:
        kind = entity.dxftype()
        if kind == "TEXT":
            text = (entity.dxf.text or "").strip()
            position = entity.dxf.insert
            height = float(getattr(entity.dxf, "height", 0.0) or 0.0)
        elif kind == "MTEXT":
            text = (entity.plain_text() or "").strip()
            position = entity.dxf.insert
            height = float(getattr(entity.dxf, "char_height", 0.0) or 0.0)
        else:
            continue

        if not text:
            continue
        labels.append(CadLabel(
            text=text,
            easting=float(position.x) * scale,
            northing=float(position.y) * scale,
            height=height * scale,
            layer=entity.dxf.layer or "0",
        ))
        if len(labels) >= MAX_LABELS:
            break
    return labels


def _collect_points(entities: Sequence[FlatEntity], scale: float) -> List[CadPoint]:
    points: List[CadPoint] = []
    for entity, _block in entities:
        kind = entity.dxftype()
        if kind == "POINT":
            location = entity.dxf.location
            block = ""
        elif kind == "INSERT":
            location = entity.dxf.insert
            block = entity.dxf.name or ""
        else:
            continue

        points.append(CadPoint(
            easting=float(location.x) * scale,
            northing=float(location.y) * scale,
            elevation=float(getattr(location, "z", 0.0) or 0.0) * scale,
            layer=entity.dxf.layer or "0",
            block=block,
        ))
        if len(points) >= MAX_POINTS:
            break
    return points


def attach_labels(points: List[CadPoint], labels: Sequence[CadLabel],
                  fallback_radius: float) -> None:
    """Give each point the nearest label that plausibly annotates it.

    A station id sits right against its symbol, so the search radius is scaled
    from the label's own text height. Each label is used once, nearest pair
    first, so two adjacent beacons cannot both claim the same id.
    """
    if not points or not labels:
        return

    pairs = []
    for label_index, label in enumerate(labels):
        radius = label.height * LABEL_SEARCH_HEIGHTS if label.height else fallback_radius
        radius = max(radius, fallback_radius)
        for point_index, point in enumerate(points):
            distance = math.dist((label.easting, label.northing),
                                 (point.easting, point.northing))
            if distance <= radius:
                pairs.append((distance, label_index, point_index))

    pairs.sort()
    used_labels, used_points = set(), set()
    for _, label_index, point_index in pairs:
        if label_index in used_labels or point_index in used_points:
            continue
        points[point_index].label = labels[label_index].text
        used_labels.add(label_index)
        used_points.add(point_index)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def inspect_drawing(path: str, file_name: Optional[str] = None,
                    units_override: Optional[int] = None) -> CadInspection:
    """Read a DWG/DXF and report everything usable in it.

    Coordinates come back in metres. ``units_override`` forces the unit
    interpretation when the drawing's header is missing or wrong.
    """
    doc, file_format = read_drawing(path)
    warnings: List[str] = []

    code, name, factor = detect_units(doc)
    if units_override is not None:
        code = units_override
        name = INSUNITS_NAMES.get(code, f"code {code}")
        factor = INSUNITS_TO_METRES.get(code, 1.0)
    elif code == 0:
        warnings.append(
            "The drawing does not record its units; metres were assumed. Check the "
            "coordinate range below and change the units if it looks wrong."
        )

    msp = doc.modelspace()
    block_refs = count_block_references(msp)
    entities = flatten_entities(msp, warnings)
    if not entities:
        raise CadImportError("The drawing is empty -- there is nothing to import.")

    tol = DEFAULT_GAP_TOL / factor if factor else DEFAULT_GAP_TOL

    rings = _collect_rings(entities, tol, factor, block_refs, warnings)
    labels = _collect_labels(entities, factor)
    points = _collect_points(entities, factor)

    inspection = CadInspection(
        file_name=file_name or os.path.basename(path),
        file_format=file_format,
        dxf_version=doc.dxfversion,
        units=name,
        units_code=code,
        units_factor=factor,
        rings=rings,
        points=points,
        labels=labels,
        warnings=warnings,
    )

    _set_extents(inspection)
    diagonal = _extent_diagonal(inspection)
    attach_labels(points, labels, fallback_radius=max(diagonal * 0.002, 0.5))
    for ring in rings:
        ring.coordinates = ring_to_coordinates(ring, points)
    inspection.layers = _summarise_layers(entities, rings, points, labels)

    if not rings:
        warnings.append(
            "No closed shape was found. The boundary may be on a layer with too many "
            "loose segments to rebuild, or it may not be closed in the drawing."
        )

    return inspection


def _set_extents(inspection: CadInspection) -> None:
    xs: List[float] = []
    ys: List[float] = []
    for ring in inspection.rings:
        xs.extend(v.easting for v in ring.vertices)
        ys.extend(v.northing for v in ring.vertices)
    for point in inspection.points:
        xs.append(point.easting)
        ys.append(point.northing)
    if not xs:
        return
    inspection.min_easting = round(min(xs), 3)
    inspection.max_easting = round(max(xs), 3)
    inspection.min_northing = round(min(ys), 3)
    inspection.max_northing = round(max(ys), 3)


def _extent_diagonal(inspection: CadInspection) -> float:
    if inspection.min_easting is None:
        return 0.0
    return math.hypot(inspection.max_easting - inspection.min_easting,
                      inspection.max_northing - inspection.min_northing)


def _summarise_layers(entities: Sequence[FlatEntity], rings: Sequence[CadRing],
                      points: Sequence[CadPoint],
                      labels: Sequence[CadLabel]) -> List[CadLayer]:
    summary: Dict[str, CadLayer] = {}

    def layer_for(name: str) -> CadLayer:
        return summary.setdefault(name or "0", CadLayer(name=name or "0"))

    for entity, _block in entities:
        info = layer_for(entity.dxf.layer)
        info.entity_count += 1
        kind = entity.dxftype()
        if kind not in info.entity_types:
            info.entity_types.append(kind)

    for ring in rings:
        layer_for(ring.layer).ring_count += 1
    for point in points:
        layer_for(point.layer).point_count += 1
    for label in labels:
        layer_for(label.layer).label_count += 1

    for info in summary.values():
        info.entity_types.sort()
    return sorted(summary.values(), key=lambda info: (-info.ring_count, info.name))


# ----------------------------------------------------------------------
# Interpretation: ring -> coordinate register
# ----------------------------------------------------------------------
def _natural_key(text: str):
    """Sort key that orders PB2 before PB10."""
    parts = re.split(r"(\d+)", text or "")
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _signed_area(vertices: Sequence[CadVertex]) -> float:
    total = 0.0
    for index in range(len(vertices)):
        a = vertices[index]
        b = vertices[(index + 1) % len(vertices)]
        total += a.easting * b.northing - b.easting * a.northing
    return total / 2.0


def _rotate(items: List, start: int) -> List:
    return items[start:] + items[:start]


def _ascending_score(labels: Sequence[Optional[str]]) -> int:
    """How well a label sequence runs in order -- PB1, PB2, PB3 ..."""
    keys = [_natural_key(label) for label in labels if label]
    return sum(1 for a, b in zip(keys, keys[1:]) if a < b)


def normalise_ring(vertices: List[CadVertex],
                   labels: List[Optional[str]]) -> Tuple[List[CadVertex], List[Optional[str]]]:
    """Put a ring into a deterministic order.

    Chaining loose segments starts the ring wherever the search happened to
    begin, so the same drawing could otherwise import with its corners rotated
    -- and the first corner is not cosmetic: the plan anchors its north arrow
    and quoted coordinates to it.

    When the drawing carries station ids, the ring is ordered to follow *the
    surveyor's own numbering* -- start at the lowest station and run in
    whichever direction keeps the numbers ascending. Restoring an old drawing
    should reproduce the traverse as it was recorded, so the original order
    wins over any winding convention. Only an unlabelled ring falls back to a
    convention: clockwise from the south-west corner, which at least makes
    repeated imports identical.
    """
    if len(vertices) < 3:
        return vertices, labels

    labelled = [(index, label) for index, label in enumerate(labels) if label]

    if len(labelled) >= 2:
        first_label = min(labelled, key=lambda item: _natural_key(item[1]))[1]

        forward_start = labels.index(first_label)
        forward = (_rotate(vertices, forward_start), _rotate(labels, forward_start))

        reversed_vertices = list(reversed(vertices))
        reversed_labels = list(reversed(labels))
        reverse_start = reversed_labels.index(first_label)
        backward = (_rotate(reversed_vertices, reverse_start),
                    _rotate(reversed_labels, reverse_start))

        if _ascending_score(backward[1]) > _ascending_score(forward[1]):
            return backward
        return forward

    if _signed_area(vertices) > 0:  # counter-clockwise -> make it clockwise
        vertices = list(reversed(vertices))
        labels = list(reversed(labels))

    start = min(range(len(vertices)),
                key=lambda i: (vertices[i].northing, vertices[i].easting))
    return _rotate(vertices, start), _rotate(labels, start)


def ring_to_coordinates(ring: CadRing, points: Sequence[CadPoint],
                        prefix: str = "PB",
                        snap_tolerance: float = 0.05) -> List[CadStation]:
    """Turn a chosen ring into the plan's coordinate register.

    Each vertex takes the station id of the beacon symbol sitting on it when
    there is one -- that is the surveyor's own numbering, recovered from the
    drawing -- and a generated ``PB1..PBn`` otherwise. Ids are made unique so
    the register can never carry a duplicate.

    Only ordered coordinates are produced: bearings, distances and area are
    recomputed downstream from this geometry, never read from the sheet.
    """
    labelled = [p for p in points if p.label]

    # Match each vertex to the beacon symbol standing on it.
    vertex_labels: List[Optional[str]] = []
    for vertex in ring.vertices:
        nearest = None
        for point in labelled:
            distance = math.dist((vertex.easting, vertex.northing),
                                 (point.easting, point.northing))
            if distance <= snap_tolerance and (nearest is None or distance < nearest[0]):
                nearest = (distance, point.label)
        vertex_labels.append(_sanitise_id(nearest[1]) if nearest else None)

    vertices, vertex_labels = normalise_ring(list(ring.vertices), vertex_labels)

    register: List[CadStation] = []
    used: set = set()
    for index, (vertex, label) in enumerate(zip(vertices, vertex_labels)):
        recovered = bool(label) and label not in used
        identifier = label if recovered else f"{prefix}{index + 1}"
        suffix = 1
        while identifier in used:
            suffix += 1
            identifier = f"{prefix}{index + 1}-{suffix}"

        used.add(identifier)
        register.append(CadStation(
            id=identifier,
            easting=round(vertex.easting, 3),
            northing=round(vertex.northing, 3),
            elevation=round(vertex.elevation, 3),
            generated=not recovered,
        ))

    return register


def _sanitise_id(text: str) -> str:
    """Trim a CAD label down to something usable as a station id."""
    cleaned = " ".join((text or "").split())
    return cleaned[:24]
