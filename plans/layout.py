"""Layout (subdivision/estate scheme) plan generator.

Supports the two ways layout jobs arrive in practice:

1. **Draw mode** — the scheme is already designed: the payload carries the
   plot corner coordinate register (``coordinates``), the ``plots`` (corner
   ids per plot), and optionally ``roads``. The plan is simply drawn.

2. **Generate mode** — only the perimeter (``layout_boundary``) and design
   parameters (``layout_parameters``) are given. The subdivision is designed
   automatically using the standard Nigerian pattern: a major spine road
   along the site's long axis, cross streets limiting block length, and
   double-loaded blocks of frontage x depth plots (default 15 m x 30 m),
   with open space and facility reservations. Generation fills in the same
   ``coordinates``/``plots``/``roads`` structures that draw mode consumes.

The payload's ``layout_mode`` field ("manual"/"auto") selects the mode
explicitly — both datasets may coexist without one shadowing the other.
Payloads without it fall back to the legacy rule: draw mode when ``plots``
are present, generate mode otherwise.

Either way, the exported bundle includes a setting-out CSV with the
coordinates of every plot corner beacon for field staking. Perimeter
bearings/distances are computed upstream by the AutoPlan API and arrive in
the payload as ``layout_boundary.legs``; when absent the perimeter is drawn
without leg labels.
"""

import logging
import math
import string
from typing import ClassVar, Dict, List, Optional, Tuple

from ezdxf.enums import TextEntityAlignment
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union

from dxf_manager import SurveyDXFManager
from models.plan import (
    CoordinateProps,
    LayoutMode,
    LayoutPlotProps,
    LayoutRoadProps,
    PlanType,
)
from plans.base import BasePlan
from utils import polygon_orientation, readable_angle

logger = logging.getLogger(__name__)

Point2 = Tuple[float, float]

# Uses that are drawn hatched as green/open areas
OPEN_USES = {"open_space", "green", "park"}


def _block_label(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA ..."""
    letters = string.ascii_uppercase
    label = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        label = letters[rem] + label
    return label


class LayoutPlan(BasePlan):
    expected_type: ClassVar[PlanType] = PlanType.LAYOUT

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.layout_boundary or len(self.layout_boundary.coordinates) < 3:
            raise ValueError("Layout plans require a layout boundary with at least 3 coordinates.")

        boundary_points = [(p.easting, p.northing) for p in self.layout_boundary.coordinates]
        self._boundary_polygon = Polygon(boundary_points)
        if not self._boundary_polygon.is_valid:
            self._boundary_polygon = self._boundary_polygon.buffer(0)
        if self._boundary_polygon.is_empty:
            raise ValueError("Layout boundary is not a valid polygon.")

        # Coordinate register: id -> (easting, northing). Boundary beacons
        # are part of the register so roads/plots may reference them too.
        self._register: Dict[str, Point2] = {}
        for coord in self.layout_boundary.coordinates:
            self._register.setdefault(coord.id, (coord.easting, coord.northing))
        for coord in self.coordinates or []:
            self._register.setdefault(coord.id, (coord.easting, coord.northing))

    def _setup_layers(self, drawer: SurveyDXFManager):
        drawer.setup_layout_layers()
        drawer.setup_beacon_style(self.beacon_type, self.beacon_size)

    def _area_text(self) -> str:
        if self.layout_boundary.area is not None:
            return self._format_area(self.layout_boundary.area)
        return ""

    def _north_arrow_reference(self) -> Optional[CoordinateProps]:
        if not self.layout_boundary.coordinates:
            return None
        return self.layout_boundary.coordinates[0]

    # ------------------------------------------------------------------
    # Boundary computations
    # ------------------------------------------------------------------
    def _ensure_boundary_computations(self):
        """Fill in the boundary area when missing.

        Perimeter legs (bearings/distances) are the AutoPlan API's job — it
        back-computes them and sends them in the payload. When absent, the
        perimeter is drawn without bearing/distance labels.
        """
        if self.layout_boundary.area is None:
            self.layout_boundary.area = round(self._boundary_polygon.area, 3)

        if not self.layout_boundary.legs:
            logger.warning("Layout boundary has no legs; the perimeter will be drawn "
                           "without bearing/distance labels.")

    # ------------------------------------------------------------------
    # Generate mode: double-loaded grid subdivision
    # ------------------------------------------------------------------
    def _orientation_angle(self) -> float:
        """Angle (degrees) of the block axis; blocks run along this axis."""
        orientation = self.layout_parameters.blocks.orientation
        if orientation == "ew":
            return 0.0
        if orientation == "ns":
            return 90.0

        # auto: longest edge of the minimum rotated rectangle
        rect = self._boundary_polygon.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        best_len, best_angle = 0.0, 0.0
        for i in range(len(coords) - 1):
            (x1, y1), (x2, y2) = coords[i], coords[i + 1]
            length = math.hypot(x2 - x1, y2 - y1)
            if length > best_len:
                best_len = length
                best_angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        return best_angle

    def _row_widths(self, row_width: float) -> List[float]:
        """Split a row of ``row_width`` into plot frontages per the strategy."""
        plot = self.layout_parameters.plot
        n = int(row_width // plot.frontage)
        if n == 0:
            return [row_width]

        remainder = row_width - n * plot.frontage
        strategy = plot.remainder_strategy
        separable = remainder * plot.depth >= plot.min_area

        if remainder < 1e-6:
            return [plot.frontage] * n
        if strategy == "distribute":
            return [row_width / n] * n
        if strategy == "separate" and separable:
            return [plot.frontage] * n + [remainder]

        # add_to_last (default): a remainder over half a frontage becomes its
        # own smaller plot when viable — widening the last plot that much
        # would create an oversized parcel no designer would draw.
        if remainder >= plot.frontage * 0.5 and separable:
            return [plot.frontage] * n + [remainder]
        widths = [plot.frontage] * n
        widths[-1] += remainder
        return widths

    def _generate_layout(self):
        """Design the subdivision and fill self.plots / self.roads / self.coordinates."""
        params = self.layout_parameters
        plot_p, road_p, block_p, reserve_p = params.plot, params.roads, params.blocks, params.reserves

        angle = self._orientation_angle()
        origin = self._boundary_polygon.centroid
        work = affinity.rotate(self._boundary_polygon, -angle, origin=origin)
        min_x, min_y, max_x, max_y = work.bounds

        strip_height = 2 * plot_p.depth if block_p.double_loaded else plot_p.depth
        min_band = plot_p.depth * 0.4  # ignore leftover bands shallower than this

        # -- Horizontal structure: spine road through the middle, block
        #    strips separated by access roads above and below it.
        spine_y = (min_y + max_y) / 2
        strips: List[dict] = []  # {y0, y1, fronts_spine: 'bottom'|'top'|None}
        road_ys: List[Tuple[float, float]] = [(spine_y, road_p.major_width)]  # (centerline y, width)

        y = spine_y + road_p.major_width / 2
        first = True
        while y < max_y - min_band:
            y1 = min(y + strip_height, max_y)
            strips.append({"y0": y, "y1": y1, "fronts_spine": "bottom" if first else None})
            first = False
            y = y1 + road_p.access_width
            if y < max_y - min_band:
                road_ys.append((y1 + road_p.access_width / 2, road_p.access_width))

        y = spine_y - road_p.major_width / 2
        first = True
        while y > min_y + min_band:
            y0 = max(y - strip_height, min_y)
            strips.append({"y0": y0, "y1": y, "fronts_spine": "top" if first else None})
            first = False
            y = y0 - road_p.access_width
            if y > min_y + min_band:
                road_ys.append((y0 - road_p.access_width / 2, road_p.access_width))

        # -- Vertical structure: cross streets limit block length.
        width_x = max_x - min_x
        n_cells = max(1, math.ceil((width_x + road_p.collector_width) /
                                   (block_p.max_length + road_p.collector_width)))
        cell_len = (width_x - (n_cells - 1) * road_p.collector_width) / n_cells

        cells: List[Tuple[float, float]] = []
        road_xs: List[Tuple[float, float]] = []
        x = min_x
        for i in range(n_cells):
            cells.append((x, x + cell_len))
            x += cell_len
            if i < n_cells - 1:
                road_xs.append((x + road_p.collector_width / 2, road_p.collector_width))
                x += road_p.collector_width

        # -- Cut plots block by block (top-left block first for numbering).
        strips.sort(key=lambda s: -s["y1"])
        raw_plots: List[dict] = []  # {geometry, block, row, order, use}
        block_index = 0

        for strip in strips:
            height = strip["y1"] - strip["y0"]
            for cell_x0, cell_x1 in cells:
                block_region = Polygon([
                    (cell_x0, strip["y0"]), (cell_x1, strip["y0"]),
                    (cell_x1, strip["y1"]), (cell_x0, strip["y1"]),
                ]).intersection(work)
                if block_region.is_empty or block_region.area < plot_p.min_area:
                    continue

                block = _block_label(block_index)
                block_index += 1

                # Double-loaded: split at the back-of-plot line in the middle
                if block_p.double_loaded and height >= 1.2 * plot_p.depth:
                    mid = (strip["y0"] + strip["y1"]) / 2
                    rows = [(strip["y0"], mid, "bottom"), (mid, strip["y1"], "top")]
                else:
                    rows = [(strip["y0"], strip["y1"], "bottom")]

                order = 0
                for row_y0, row_y1, side in rows:
                    fronts_spine = strip["fronts_spine"] == side
                    row_x = cell_x0
                    for width in self._row_widths(cell_x1 - cell_x0):
                        rect = Polygon([
                            (row_x, row_y0), (row_x + width, row_y0),
                            (row_x + width, row_y1), (row_x, row_y1),
                        ])
                        row_x += width

                        clipped = rect.intersection(work)
                        pieces = list(clipped.geoms) if isinstance(clipped, MultiPolygon) else [clipped]
                        for piece in pieces:
                            if piece.is_empty or piece.geom_type != "Polygon":
                                continue
                            if piece.area < plot_p.min_area:
                                continue
                            use = "commercial" if (reserve_p.commercial_along_major and fronts_spine) \
                                else "residential"
                            raw_plots.append({
                                "geometry": piece,
                                "block": block,
                                "row": side,
                                "order": order,
                                "use": use,
                            })
                            order += 1

        if not raw_plots:
            raise ValueError("Layout generation produced no plots — check the boundary and parameters.")

        # -- Reserves: facilities take whole rows near the centre; open space
        #    converts the plots nearest the centre until the target is met.
        centroid = work.centroid

        for facility in reserve_p.facilities:
            groups: Dict[Tuple[str, str], List[dict]] = {}
            for p in raw_plots:
                if p["use"] == "residential":
                    groups.setdefault((p["block"], p["row"]), []).append(p)
            if not groups:
                break
            key = min(groups, key=lambda k: unary_union(
                [p["geometry"] for p in groups[k]]).centroid.distance(centroid))
            members = groups[key]
            merged = unary_union([p["geometry"] for p in members])
            if isinstance(merged, MultiPolygon):
                merged = max(merged.geoms, key=lambda g: g.area)
            keeper = min(members, key=lambda p: p["order"])
            keeper["geometry"] = merged
            keeper["use"] = facility
            for p in members:
                if p is not keeper:
                    raw_plots.remove(p)

        if reserve_p.open_space_percent > 0:
            target = self._boundary_polygon.area * reserve_p.open_space_percent / 100.0
            reserved = 0.0
            candidates = sorted(
                (p for p in raw_plots if p["use"] == "residential"),
                key=lambda p: p["geometry"].centroid.distance(centroid),
            )
            for p in candidates:
                if reserved >= target:
                    break
                p["use"] = "open_space"
                reserved += p["geometry"].area

        # -- Roads: clip each centerline to the boundary.
        road_records: List[dict] = []  # {name, width, line}
        road_number = 1
        spine_name = road_p.major_road_name or f"Road {road_number}"

        def clip_road(line: LineString) -> List[LineString]:
            clipped = line.intersection(work)
            if clipped.is_empty:
                return []
            if clipped.geom_type == "LineString":
                return [clipped]
            return [g for g in clipped.geoms if g.geom_type == "LineString" and g.length > 1.0]

        for cy, width in road_ys:
            for segment in clip_road(LineString([(min_x - 10, cy), (max_x + 10, cy)])):
                name = spine_name if (cy == spine_y) else f"Road {road_number + 1}"
                if cy != spine_y:
                    road_number += 1
                road_records.append({"name": name, "width": width, "line": segment})
        road_number += 1
        for cx, width in road_xs:
            for segment in clip_road(LineString([(cx, min_y - 10), (cx, max_y + 10)])):
                road_records.append({"name": f"Road {road_number}", "width": width, "line": segment})
                road_number += 1

        # -- Rotate everything back to real coordinates.
        for p in raw_plots:
            p["geometry"] = affinity.rotate(p["geometry"], angle, origin=origin)
        for r in road_records:
            r["line"] = affinity.rotate(r["line"], angle, origin=origin)

        # -- Number plots and build the coordinate register.
        plot_start = params.numbering.plot_start
        raw_plots.sort(key=lambda p: (p["block"], 0 if p["row"] == "bottom" else 1, p["order"]))

        new_coordinates: List[CoordinateProps] = []
        corner_ids: Dict[Point2, str] = {}
        plots: List[LayoutPlotProps] = []
        counters: Dict[str, int] = {}

        def register(x: float, y: float, prefix: str, count: List[int]) -> str:
            key = (round(x, 3), round(y, 3))
            if key not in corner_ids:
                count[0] += 1
                corner_id = f"{prefix} {count[0]}"
                corner_ids[key] = corner_id
                self._register[corner_id] = key
                new_coordinates.append(CoordinateProps(id=corner_id, easting=key[0], northing=key[1]))
            return corner_ids[key]

        lp_count = [0]
        for p in raw_plots:
            block = p["block"]
            counters[block] = counters.get(block, plot_start - 1) + 1
            exterior = list(p["geometry"].exterior.coords)[:-1]
            ids = [register(x, y, "LP", lp_count) for x, y in exterior]
            plots.append(LayoutPlotProps(
                block=block,
                number=counters[block],
                ids=ids,
                area=round(p["geometry"].area, 2),
                use=p["use"],
            ))

        rc_count = [0]
        roads: List[LayoutRoadProps] = []
        for r in road_records:
            ids = [register(x, y, "RC", rc_count) for x, y in r["line"].coords]
            roads.append(LayoutRoadProps(name=r["name"], width=r["width"], centerline_ids=ids))

        self.plots = plots
        self.roads = roads
        self.coordinates = new_coordinates

    # ------------------------------------------------------------------
    # Drawing (shared by both modes)
    # ------------------------------------------------------------------
    def _plot_points(self, plot: LayoutPlotProps) -> List[Point2]:
        points = []
        for pid in plot.ids:
            if pid not in self._register:
                raise ValueError(f"Plot '{plot.label()}' references unknown coordinate id '{pid}'")
            points.append(self._register[pid])
        return points

    def draw_boundary(self):
        boundary_points = [(c.easting, c.northing) for c in self.layout_boundary.coordinates]
        self._drawer.add_boundary(boundary_points)

        orientation = polygon_orientation(boundary_points)
        for leg in self.layout_boundary.legs or []:
            self.add_leg_labels(leg, orientation)

    def draw_beacons(self):
        seen = set()
        for coord in self.layout_boundary.coordinates:
            if coord.id in seen:
                continue
            seen.add(coord.id)
            self._drawer.draw_beacon(coord.easting, coord.northing, 0,
                                     self.label_size, self._get_drawing_extent(), coord.id)

    def draw_roads(self):
        for road in self.roads or []:
            points = [self._register[pid] for pid in road.centerline_ids if pid in self._register]
            if len(points) < 2:
                continue

            self._drawer.add_road_cl(points)

            centerline = LineString(points)
            for side in ("left", "right"):
                edge = centerline.parallel_offset(road.width / 2, side)
                edge = edge.intersection(self._boundary_polygon)
                segments = [edge] if edge.geom_type == "LineString" else \
                    [g for g in getattr(edge, "geoms", []) if g.geom_type == "LineString"]
                for segment in segments:
                    if not segment.is_empty:
                        self._drawer.add_road(list(segment.coords))

            if road.name:
                mid = centerline.interpolate(0.5, normalized=True)
                (x1, y1), (x2, y2) = points[0], points[-1]
                text_angle = readable_angle(math.degrees(math.atan2(y2 - y1, x2 - x1)))
                height = min(self.label_size, road.width * 0.4)
                self._drawer.add_label(road.name, mid.x, mid.y,
                                       angle=text_angle, height=height)

    def draw_plots(self):
        for plot in self.plots or []:
            points = self._plot_points(plot)
            polygon = Polygon(points)
            cx, cy = polygon.centroid.x, polygon.centroid.y

            # Labels must fit inside their own plot regardless of site size
            size = math.sqrt(max(polygon.area, 1.0))
            number_height = min(self.label_size, size * 0.15)
            use_height = min(self.label_size, size * 0.09)

            if plot.use in OPEN_USES:
                self._drawer.add_greenspace(points)
                self._drawer.add_text("OPEN SPACE", cx, cy, use_height,
                                      alignment=TextEntityAlignment.MIDDLE_CENTER)
                continue

            self._drawer.add_parcel(points)
            if plot.use not in ("residential", "commercial"):
                self._drawer.add_text(str(plot.use).upper(), cx, cy, use_height,
                                      alignment=TextEntityAlignment.MIDDLE_CENTER)
            else:
                self._drawer.add_text(str(plot.number), cx, cy, number_height,
                                      alignment=TextEntityAlignment.MIDDLE_CENTER)

    def draw_block_labels(self):
        if not self.plots:
            return

        blocks: Dict[str, List[Polygon]] = {}
        for plot in self.plots:
            if plot.block:
                blocks.setdefault(plot.block, []).append(Polygon(self._plot_points(plot)))

        for block, polygons in blocks.items():
            union = unary_union(polygons)
            height = min(self.label_size * 1.6, math.sqrt(max(union.area, 1.0)) * 0.07)
            self._drawer.add_text(f"BLOCK {block}", union.centroid.x, union.centroid.y,
                                  height, alignment=TextEntityAlignment.MIDDLE_CENTER)

    # ------------------------------------------------------------------
    # Area schedule
    # ------------------------------------------------------------------
    def _area_schedule(self) -> List[List[str]]:
        total_area = self._boundary_polygon.area
        by_use: Dict[str, Tuple[int, float]] = {}
        plots_area = 0.0

        for plot in self.plots or []:
            area = plot.area if plot.area is not None else Polygon(self._plot_points(plot)).area
            count, use_area = by_use.get(plot.use, (0, 0.0))
            by_use[plot.use] = (count + 1, use_area + area)
            plots_area += area

        rows = [["LAND USE", "PLOTS", "AREA (SQ.M)", "%"]]
        for use in sorted(by_use, key=lambda u: -by_use[u][1]):
            count, area = by_use[use]
            rows.append([
                use.replace("_", " ").upper(),
                str(count),
                f"{area:,.0f}",
                f"{area / total_area * 100:.1f}",
            ])

        circulation = max(total_area - plots_area, 0.0)
        rows.append(["ROADS / CIRCULATION", "-", f"{circulation:,.0f}",
                     f"{circulation / total_area * 100:.1f}"])
        rows.append(["TOTAL", str(len(self.plots or [])), f"{total_area:,.0f}", "100.0"])
        return rows

    def draw_schedule(self):
        if not self.plots:
            return

        rows = self._area_schedule()
        frame_left, frame_bottom, frame_right, frame_top = self._frame_coords
        min_x, min_y, max_x, max_y = self._bounding_box

        text_height = self.label_size
        row_height = text_height * 2.2
        # Generous per-character estimate so text never spills over its cell
        char_w = text_height * 0.95
        col_widths = [
            max(len(str(r[0])) for r in rows) * char_w + 2 * char_w,
            max(len(str(r[1])) for r in rows) * char_w + 2 * char_w,
            max(len(str(r[2])) for r in rows) * char_w + 2 * char_w,
            max(len(str(r[3])) for r in rows) * char_w + 2 * char_w,
        ]

        # Place the table below the drawing, left-aligned with the site —
        # the frame's bottom margin is always deep enough, so the table can
        # never overlap the plots regardless of the computed text sizes.
        x = min_x
        y = min_y - row_height
        self._drawer.draw_table(x, y, rows, col_widths, row_height, text_height)

    # ------------------------------------------------------------------
    # Setting-out CSV
    # ------------------------------------------------------------------
    def build_setting_out_csv(self) -> str:
        """CSV of every beacon a surveyor must set out: boundary beacons,
        plot corners, and road centerline points."""
        descriptions: Dict[str, str] = {}

        for coord in self.layout_boundary.coordinates:
            descriptions.setdefault(coord.id, "Boundary beacon")

        for plot in self.plots or []:
            for pid in plot.ids:
                if pid in descriptions:
                    if "Boundary" not in descriptions[pid] and plot.label() not in descriptions[pid]:
                        descriptions[pid] += f" / {plot.label()}"
                else:
                    descriptions[pid] = plot.label()

        for road in self.roads or []:
            for pid in road.centerline_ids:
                descriptions.setdefault(pid, f"{road.name} centerline".strip())

        lines = ["ID,NORTHING,EASTING,DESCRIPTION"]
        for pid, description in descriptions.items():
            if pid not in self._register:
                continue
            easting, northing = self._register[pid]
            safe = description.replace('"', "'")
            lines.append(f'{pid},{northing:.3f},{easting:.3f},"{safe}"')

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    def _effective_layout_mode(self) -> LayoutMode:
        """``layout_mode`` decides which design is drawn; legacy payloads
        without it keep the old behaviour (manual data wins when present)."""
        if self.layout_mode is not None:
            return self.layout_mode
        return LayoutMode.MANUAL if self.plots else LayoutMode.AUTO

    def draw(self):
        self._ensure_boundary_computations()

        if self._effective_layout_mode() == LayoutMode.AUTO:
            # Replaces plots/roads/coordinates in memory only — the caller's
            # stored manual layout data is never modified.
            self._generate_layout()
        elif not self.plots:
            raise ValueError("Layout is in manual mode but has no plots; "
                             "add plots or switch to automated layout.")

        self.draw_boundary()
        self.draw_roads()
        self.draw_plots()
        self.draw_block_labels()
        self.draw_schedule()
        self.draw_frames()
        self.draw_title_block()
        self.draw_footer_boxes()
        self.draw_north_arrow()
        self.draw_beacons()

    def save(self) -> str:
        return self._drawer.save(
            paper_size=self.page_size,
            orientation=self.page_orientation,
            extra_files={"setting_out_coordinates.csv": self.build_setting_out_csv()},
        )
