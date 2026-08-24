"""Route survey plan generator: a plan-and-profile sheet.

The industry-standard route survey drawing combines two views on one sheet:

- **Plan view (horizontal alignment)** — the route centerline drawn from the
  station coordinates, rotated so the route runs left to right, with chainage
  ticks/labels, right-of-way edges, and a north arrow rotated to match.
- **Longitudinal profile** — existing ground level against chainage on an
  exaggerated vertical scale, over a station/elevation grid, with a
  station/ground-level table beneath it.

Long routes make this sheet extremely wide relative to its height, so the
geometry here is deliberate about readability:

- station text is sized against the station spacing and labelled every
  *n*-th station (a stride) when stations are too dense to label all;
- the table rows are sized from the text they hold, not the profile height;
- the frame hugs the content with route-specific margins instead of the
  generic square-ish plan margins.

The plan view is drawn when the payload carries station coordinates
(``coordinates`` whose ids match the ``elevations`` ids) and
``route_parameters.show_plan_view`` is enabled; otherwise the sheet contains
the profile only (backward compatible with older payloads).
"""

import logging
import math
from typing import ClassVar, List, Optional, Tuple

from ezdxf.enums import TextEntityAlignment
from shapely.geometry import LineString

from dxf_manager import SurveyDXFManager
from models.plan import PlanType
from plans.base import BasePlan
from utils import readable_angle

logger = logging.getLogger(__name__)

Point2 = Tuple[float, float]

# Average character width as a fraction of text height (Times-like fonts,
# measured generously so sized boxes never clip their text)
CHAR_W = 0.85


class RoutePlan(BasePlan):
    expected_type: ClassVar[PlanType] = PlanType.ROUTE

    #: A route sheet is a profile, not a map: chainage and elevation are drawn
    #: at independent horizontal and vertical scales, so there is no single
    #: map scale to plot at. The sheet stays fitted to its content.
    true_scale: ClassVar[bool] = False

    # ------------------------------------------------------------------
    # Sheet geometry
    # ------------------------------------------------------------------
    def _compute_bounding_box(self):
        box = self.get_route_plan_bounding_box()
        if box is None:
            raise ValueError("Route plans require elevations and longitudinal profile parameters.")

        params = self.longitudinal_profile_parameters
        elev_start, elev_end, interval, _ = self._elevation_grid_range()

        min_x = box[0]
        hscale = params.horizontal_scale or 1.0
        spacing = params.station_interval * hscale

        # The plan view is true to coordinate; the profile adjusts to it:
        # each station's profile column sits at its true (chord-projected)
        # plan position. Without a usable alignment, columns are uniform.
        self._alignment = self._compute_alignment()
        if self._alignment is not None:
            station_xs = [min_x + along * hscale for along in self._alignment["along"]]
        else:
            station_xs = [min_x + i * spacing for i in range(len(self.elevations))]

        self._station_xs = station_xs
        max_x = station_xs[-1]
        width = max(max_x - min_x, 1e-6)
        grid_bottom = self._elevation_to_y(elev_start)
        grid_top = self._elevation_to_y(elev_end)

        min_spacing = min(
            (b - a for a, b in zip(station_xs, station_xs[1:])), default=spacing
        )

        # Station text: readable relative to the sheet, clamped by the user's
        # label size; labels use a stride when stations are packed tighter
        # than the text needs.
        station_text = min(self.label_size, width * 0.0055)
        station_text = max(station_text, width * 0.003)
        stride = max(1, math.ceil((station_text * 1.9) / max(min_spacing, 1e-6)))

        chain_chars = max((len(e.chainage or "") for e in self.elevations), default=5)
        value_chars = max((len(f"{e.elevation:g}") for e in self.elevations), default=5)

        # Table rows sized from the rotated text they hold
        chain_row = chain_chars * CHAR_W * station_text + station_text * 1.4
        value_row = value_chars * CHAR_W * station_text + station_text * 1.4
        header_chars = len("GROUND ELEV")
        table_width = max(width * 0.08, header_chars * CHAR_W * station_text + 2 * station_text)

        self._grid = {
            "min_x": min_x,
            "max_x": max_x,
            "grid_top": grid_top,
            "grid_bottom": grid_bottom,
            "elev_start": elev_start,
            "elev_end": elev_end,
            "interval": interval,
            "spacing": spacing,
            "station_text": station_text,
            "stride": stride,
            "chain_row": chain_row,
            "value_row": value_row,
            "table_width": table_width,
            "table_bottom": grid_bottom - chain_row - value_row,
            "width": width,
        }

        content = (min_x - table_width, self._grid["table_bottom"], max_x, grid_top)

        # Plan view band above the grid
        self._plan_points: List[Point2] = []
        self._plan_rotation_deg = 0.0
        self._plan_band: Optional[Tuple[float, float, float, float]] = None
        self._prepare_plan_view()

        if self._plan_band is None:
            return content

        bmin_x, bmin_y, bmax_x, bmax_y = self._plan_band
        return (
            min(content[0], bmin_x),
            min(content[1], bmin_y),
            max(content[2], bmax_x),
            max(content[3], bmax_y),
        )

    def _setup_frame_coords(self):
        """Route sheets are far wider than tall; hug the content instead of
        using the generic square-ish plan margins. The bottom margin is
        solved so the footer band (18% of frame height) clears the table."""
        min_x, min_y, max_x, max_y = self._bounding_box
        content_w = max_x - min_x
        content_h = max_y - min_y

        margin_x = content_w * 0.08
        margin_top = content_w * 0.26
        # bottom >= 0.18 * frame_height + clearance, frame_height depends on bottom
        margin_bottom = (0.18 * (content_h + margin_top) + content_w * 0.03) / (1 - 0.18)

        return self._fit_frame_to_page(
            (min_x - margin_x, min_y - margin_bottom, max_x + margin_x, max_y + margin_top)
        )

    def _setup_layers(self, drawer: SurveyDXFManager):
        drawer.setup_route_layers()

    # ------------------------------------------------------------------
    # Profile geometry helpers
    # ------------------------------------------------------------------
    def get_elevation_interval(self) -> float:
        """Pick a 'nice' grid interval that yields roughly 8 elevation lines."""
        elevations = [e.elevation for e in self.elevations]
        elev_range = max(elevations) - min(elevations)
        if elev_range <= 0:
            return 1.0

        raw_interval = elev_range / 8
        exp = math.floor(math.log10(raw_interval))
        frac = raw_interval / 10 ** exp
        if frac < 1.5:
            nice = 1
        elif frac < 3:
            nice = 2
        elif frac < 7:
            nice = 5
        else:
            nice = 10
        return nice * 10 ** exp

    def _elevation_grid_range(self):
        """Grid elevation range: the data range padded by half of itself on
        both sides, snapped outwards to whole intervals."""
        params = self.longitudinal_profile_parameters
        interval = params.elevation_interval or self.get_elevation_interval()

        elevations = [e.elevation for e in self.elevations]
        min_elev, max_elev = min(elevations), max(elevations)
        pad = ((max_elev - min_elev) or interval) * 0.5

        elev_start = math.floor((min_elev - pad) / interval) * interval
        elev_end = math.ceil((max_elev + pad) / interval) * interval
        return elev_start, elev_end, interval, min_elev

    def _elevation_to_y(self, elevation: float) -> float:
        """Y drawing coordinate of an elevation; the minimum ground elevation
        maps to the drawing origin."""
        params = self.longitudinal_profile_parameters
        min_elev = min(e.elevation for e in self.elevations)
        return (elevation - min_elev) * params.vertical_scale

    # ------------------------------------------------------------------
    # Plan view (horizontal alignment)
    # ------------------------------------------------------------------
    def _compute_alignment(self):
        """Chord-decompose the station coordinates: distances along the
        first-to-last chord (which become the profile column positions) and
        lateral offsets across it (the plan view's true shape, rotated).

        Returns None when there is no usable alignment: plan view disabled,
        stations missing coordinates, or the route doubling back on its
        chord (a warped profile would fold onto itself).
        """
        if not self.route_parameters.show_plan_view or not self.coordinates:
            return None

        coord_map = {c.id: c for c in self.coordinates}
        stations = [coord_map.get(e.id) for e in self.elevations]
        if any(c is None for c in stations) or len(stations) < 2:
            if any(c is not None for c in stations):
                logger.warning("Plan view skipped: every station needs a coordinate "
                               "matching its elevation id.")
            return None

        first, last = stations[0], stations[-1]
        chord_x = last.easting - first.easting
        chord_y = last.northing - first.northing
        chord = math.hypot(chord_x, chord_y)
        if chord < 1e-6:
            logger.warning("Plan view skipped: route start and end coincide.")
            return None
        ux, uy = chord_x / chord, chord_y / chord

        along = [(c.easting - first.easting) * ux + (c.northing - first.northing) * uy
                 for c in stations]
        across = [-(c.easting - first.easting) * uy + (c.northing - first.northing) * ux
                  for c in stations]

        if any(b <= a for a, b in zip(along, along[1:])):
            logger.warning("Plan view skipped: the route doubles back relative to its "
                           "chord, so the profile cannot follow its plan positions.")
            return None

        return {
            "along": along,
            "across": across,
            "angle_deg": math.degrees(math.atan2(chord_y, chord_x)),
        }

    def _prepare_plan_view(self):
        """Place the true-shape plan view in a band above the profile.

        The alignment is drawn true to coordinate (rotated so its chord runs
        left to right); the profile columns already sit at each station's
        plan position, so the two views align station-for-station.
        """
        if self._alignment is None:
            return

        params = self.longitudinal_profile_parameters
        hscale = params.horizontal_scale or 1.0
        grid = self._grid

        across = [a * hscale for a in self._alignment["across"]]

        row_half = (self.route_parameters.right_of_way_width / 2) * hscale
        chain_chars = max((len(e.chainage or "") for e in self.elevations), default=5)
        tick_half = max(row_half * 0.25, grid["station_text"] * 0.6)
        # Chainage labels anchor clear of the right-of-way edge (not just the
        # tick, which can end inside the corridor); reserve their full reach.
        label_offset = max(tick_half * 1.5, row_half + grid["station_text"])
        label_reach = label_offset + chain_chars * CHAR_W * grid["station_text"]
        pad = max(row_half, label_reach) + grid["station_text"]

        grid_top = grid["grid_top"]
        grid_height = grid_top - grid["grid_bottom"]
        header_h = min(grid["width"] * 0.012, self.label_size * 1.5)
        # The gap hosts both view headers (PLAN under the band, LONGITUDINAL
        # SECTION above the grid)
        gap = max(grid_height * 0.35, grid["width"] * 0.05, header_h * 6)

        min_ly = min(across)
        ty = grid_top + gap + pad - min_ly

        self._plan_points = [(x, a + ty) for x, a in zip(self._station_xs, across)]
        self._plan_rotation_deg = self._alignment["angle_deg"]
        self._plan_tick_half = tick_half
        self._plan_label_offset = label_offset

        xs = [p[0] for p in self._plan_points]
        ys = [p[1] for p in self._plan_points]

        band_top = max(ys) + pad
        band_bottom = min(ys) - pad

        # PLAN header sits below the band, inside the gap — always clear of
        # the title block above.
        self._plan_header = ((min(xs) + max(xs)) / 2, band_bottom - header_h * 0.8, header_h)

        # Reserve room for the rotated north arrow to the right of the band
        arrow_h = grid["width"] * 0.035
        self._plan_arrow = (max(xs) + grid["width"] * 0.02, (band_top + band_bottom) / 2 - arrow_h / 2, arrow_h)
        self._plan_band = (
            min(xs),
            band_bottom,
            max(xs) + grid["width"] * 0.02 + arrow_h * 1.2,
            band_top,
        )

    def draw_plan_view(self):
        if not self._plan_points:
            return

        grid = self._grid
        params = self.longitudinal_profile_parameters
        hscale = params.horizontal_scale or 1.0
        row_half = (self.route_parameters.right_of_way_width / 2) * hscale
        band = self._plan_band

        # Centerline
        self._drawer.add_polyline(self._plan_points, "ALIGNMENT")

        # Right-of-way edges
        if row_half > 0:
            centerline = LineString(self._plan_points)
            for side in ("left", "right"):
                edge = centerline.parallel_offset(row_half, side)
                segments = [edge] if edge.geom_type == "LineString" else \
                    [g for g in getattr(edge, "geoms", []) if g.geom_type == "LineString"]
                for segment in segments:
                    if not segment.is_empty:
                        self._drawer.add_polyline(list(segment.coords), "ROW")

        # Station ticks (every station) and chainage labels (strided)
        text_h = grid["station_text"]
        stride = grid["stride"]
        tick_half = self._plan_tick_half
        n = len(self._plan_points)
        for i, (x, y) in enumerate(self._plan_points):
            before = self._plan_points[max(i - 1, 0)]
            after = self._plan_points[min(i + 1, n - 1)]
            seg_angle = math.atan2(after[1] - before[1], after[0] - before[0])
            nx, ny = -math.sin(seg_angle), math.cos(seg_angle)  # unit normal

            self._drawer.add_polyline(
                [(x - nx * tick_half, y - ny * tick_half), (x + nx * tick_half, y + ny * tick_half)],
                "STATIONS",
            )

            if self.route_parameters.show_chainage_labels and i % stride == 0:
                # Label along the outward normal, anchored past the tick end.
                # When the readable angle is the reciprocal of the normal,
                # anchor by the other end so the text still extends outward.
                raw_angle = math.degrees(math.atan2(ny, nx))
                label_angle = readable_angle(raw_angle)
                flipped = abs((label_angle - raw_angle) % 360.0) > 90.0
                alignment = (TextEntityAlignment.MIDDLE_RIGHT if flipped
                             else TextEntityAlignment.MIDDLE_LEFT)
                self._drawer.add_text(
                    self.elevations[i].chainage if i < len(self.elevations) else "",
                    x + nx * self._plan_label_offset,
                    y + ny * self._plan_label_offset,
                    text_h,
                    rotation=label_angle,
                    alignment=alignment,
                )

        # View header (below the band) and rotated north arrow
        header_x, header_y, header_h = self._plan_header
        self._drawer.add_text("PLAN", header_x, header_y, header_h,
                              alignment=TextEntityAlignment.TOP_CENTER)

        arrow_x, arrow_y, arrow_h = self._plan_arrow
        self._drawer.draw_north_arrow(arrow_x, arrow_y, arrow_h,
                                      rotation=-self._plan_rotation_deg)

    # ------------------------------------------------------------------
    # Longitudinal profile
    # ------------------------------------------------------------------
    def draw_grid(self):
        grid = self._grid
        min_x, max_x = grid["min_x"], grid["max_x"]
        grid_top, grid_bottom = grid["grid_top"], grid["grid_bottom"]
        text_h = grid["station_text"]
        stride = grid["stride"]

        # Graph box
        self._drawer.add_grid_line(min_x, grid_bottom, max_x, grid_bottom)
        self._drawer.add_grid_line(min_x, grid_bottom, min_x, grid_top)
        self._drawer.add_grid_line(max_x, grid_bottom, max_x, grid_top)
        self._drawer.add_grid_line(min_x, grid_top, max_x, grid_top)

        # Table below the graph: chainage row (bottom) and ground level row
        table_left = min_x - grid["table_width"]
        value_row_top = grid_bottom
        value_row_bottom = grid_bottom - grid["value_row"]
        chain_row_bottom = grid["table_bottom"]

        self._drawer.add_grid_line(table_left, value_row_top, max_x, value_row_top)
        self._drawer.add_grid_line(table_left, value_row_bottom, max_x, value_row_bottom)
        self._drawer.add_grid_line(table_left, chain_row_bottom, max_x, chain_row_bottom)
        self._drawer.add_grid_line(table_left, value_row_top, table_left, chain_row_bottom)
        self._drawer.add_grid_line(max_x, value_row_top, max_x, chain_row_bottom)
        self._drawer.add_grid_line(min_x, value_row_top, min_x, chain_row_bottom)

        # Header text shrinks to fit its box, centered so any font-width
        # variance spreads to both sides instead of crossing the graph edge.
        header_text = min(text_h, (grid["table_width"] * 0.9) / (len("GROUND ELEV") * CHAR_W))
        header_x = (table_left + min_x) / 2
        self._drawer.add_text("GROUND ELEV", header_x,
                              (value_row_top + value_row_bottom) / 2, header_text,
                              alignment=TextEntityAlignment.MIDDLE_CENTER)
        self._drawer.add_text("STATION", header_x,
                              (value_row_bottom + chain_row_bottom) / 2, header_text,
                              alignment=TextEntityAlignment.MIDDLE_CENTER)

        # Station verticals every station; values/chainages every stride.
        # Columns sit at each station's true plan position.
        params = self.longitudinal_profile_parameters
        for i, elevation in enumerate(self.elevations):
            x = self._station_xs[i]
            self._drawer.add_grid_line(x, chain_row_bottom, x, grid_top)
            if i % stride == 0:
                # BOTTOM_CENTER + 90° rotation centers the string's length on
                # the anchor, so anchor at the row middle to keep it inside.
                self._drawer.add_text(
                    f"{elevation.elevation:g}", x,
                    value_row_bottom + grid["value_row"] / 2, text_h,
                    alignment=TextEntityAlignment.BOTTOM_CENTER, rotation=90)
                self._drawer.add_text(
                    elevation.chainage, x,
                    chain_row_bottom + grid["chain_row"] / 2, text_h,
                    alignment=TextEntityAlignment.BOTTOM_CENTER, rotation=90)

        # Horizontal elevation lines with labels on the left (strided when
        # the interval spacing is tighter than the text)
        interval = grid["interval"]
        vscale = params.vertical_scale or 1.0
        elev_text = min(text_h, interval * vscale * 0.6)
        elev_stride = max(1, math.ceil((elev_text * 2.4) / max(interval * vscale, 1e-6)))

        steps = round((grid["elev_end"] - grid["elev_start"]) / interval)
        for i in range(steps + 1):
            elevation = grid["elev_start"] + i * interval
            y = self._elevation_to_y(elevation)
            self._drawer.add_f_grid_line(min_x, y, max_x, y)
            if i % elev_stride == 0:
                self._drawer.add_text(f"{elevation:g}", min_x - elev_text * 0.6, y,
                                      elev_text, alignment=TextEntityAlignment.MIDDLE_RIGHT)

        # View header (only needed when the sheet also has a plan view)
        if self._plan_points:
            header_h = min(grid["width"] * 0.012, self.label_size * 1.5)
            self._drawer.add_text("LONGITUDINAL SECTION", (min_x + max_x) / 2,
                                  grid_top + (grid_top - grid_bottom) * 0.12,
                                  header_h, alignment=TextEntityAlignment.MIDDLE_CENTER)

    def draw_profile_line(self):
        points = [
            (self._station_xs[i], self._elevation_to_y(e.elevation))
            for i, e in enumerate(self.elevations)
        ]
        self._drawer.add_profile(points)

    def draw_content(self):
        self.draw_grid()
        self.draw_profile_line()
        self.draw_plan_view()
        self.draw_frames()
        self.draw_title_block()
        self.draw_footer_boxes()
