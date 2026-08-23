"""Topographic survey plan generator.

Draws spot heights, the site boundary, and elevation contours generated
from either a TIN (Delaunay triangulation) or a regular interpolation grid.
Contour extraction uses contourpy directly, which keeps the service free of
matplotlib's global figure state (important for a long-running server).
"""

import logging
import math
from typing import ClassVar, List, Optional, Tuple

import numpy as np
import shapely
from contourpy import LineType, contour_generator
from scipy.interpolate import LinearNDInterpolator, griddata
from scipy.ndimage import gaussian_filter
from scipy.spatial import Delaunay
from shapely.geometry import MultiPoint, Polygon

from dxf_manager import SurveyDXFManager
from models.plan import (
    CONTOUR_GRID_CELL_MM,
    CONTOUR_GRID_MAX,
    CONTOUR_GRID_MIN,
    SPOT_HEIGHT_SPACING_MM,
    TOPO_POINT_SPACING_MM,
    TOPO_POINT_SYMBOL_MM,
    CoordinateProps,
    PlanType,
)
from point_stream import thin_for_display

logger = logging.getLogger(__name__)
from plans.base import BasePlan, TableSpec
from utils import polygon_orientation


class TopographicPlan(BasePlan):
    expected_type: ClassVar[PlanType] = PlanType.TOPOGRAPHIC
    draws_north_arrow: ClassVar[bool] = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        points = [(c.easting, c.northing, c.elevation) for c in self.coordinates or []]
        if not points:
            raise ValueError("Cannot determine topographic points without valid coordinates.")

        self._points = np.array(points)
        self._x = self._points[:, 0]
        self._y = self._points[:, 1]
        self._z = self._points[:, 2]

    def _setup_layers(self, drawer: SurveyDXFManager):
        drawer.setup_topographic_layers()
        drawer.setup_beacon_style(self.beacon_type, self.beacon_symbol_size)
        drawer.setup_topo_point_style(size=self._topo_point_symbol_size())

    def _topo_point_symbol_size(self) -> float:
        """Arm length of the spot-height cross, in model units."""
        if self.auto_scale_sizes and self.true_scale:
            return TOPO_POINT_SYMBOL_MM * self.mm_to_model
        return 0.5 * self.topographic_setting.point_label_scale

    def _area_text(self) -> str:
        if self.topographic_boundary and self.topographic_boundary.area is not None:
            return self._format_area(self.topographic_boundary.area)
        return ""

    def _north_arrow_reference(self) -> Optional[CoordinateProps]:
        if not self.topographic_boundary or not self.topographic_boundary.coordinates:
            return None
        return self.topographic_boundary.coordinates[0]

    def _title_block_notes(self) -> list:
        """Annotate the sheet with the contour interval actually used.

        Only shown when contours are drawn, so the label never advertises an
        interval for a plan that has no contours.
        """
        settings = self.topographic_setting
        draws_contours = settings.show_contours and (settings.tin or settings.grid)
        if draws_contours and settings.contour_interval > 0:
            interval = f"{settings.contour_interval:g}"
            notes = [f"CONTOUR INTERVAL :- {interval} M"]
        else:
            notes = []

        drawn = len(self.visible_spot_heights()) if settings.show_spot_heights else 0
        total = self.total_survey_points()
        if settings.show_spot_heights and total > drawn:
            notes.append(f"SPOT HEIGHTS SHOWN :- {drawn:,} OF {total:,}")
        return notes

    # ------------------------------------------------------------------
    # Points & boundary
    # ------------------------------------------------------------------
    def _bearing_distance_table(self):
        """Legs of the perimeter (boundary) survey."""
        boundary = self.topographic_boundary
        legs = boundary.legs if boundary else []
        return TableSpec("BOUNDARY BEARING & DISTANCE",
                         ["LINE", "BEARING", "DIST. (M)"], self._leg_rows(legs))

    def _coordinate_table(self):
        """The boundary beacon register."""
        boundary = self.topographic_boundary
        coords = boundary.coordinates if boundary else []
        return TableSpec("BOUNDARY COORDINATES", ["STN", "NORTHING", "EASTING"],
                         self._coordinate_rows(coords))

    def draw_beacons(self):
        if not self.topographic_boundary:
            return

        seen = set()
        for coord in self.topographic_boundary.coordinates:
            if coord.id in seen:
                continue
            seen.add(coord.id)
            self._drawer.draw_beacon(
                coord.easting, coord.northing, 0,
                self.height("beacon_label", self.label_size), coord.id,
            )

    def visible_spot_heights(self) -> list:
        """Survey shots the sheet can carry as markers.

        The marker is a 1 mm cross, so this is a much denser set than the one
        that can carry elevations beside it: what limits it is the symbol, not
        the text. The full survey is unaffected either way -- it is still what
        the contours are interpolated from, and what the export carries.
        """
        points = self.coordinates or []
        spacing = TOPO_POINT_SPACING_MM * self.mm_to_model
        return thin_for_display(
            points, spacing, lambda c: (c.easting, c.northing),
        )

    def labelled_spot_heights(self, visible: list) -> list:
        """Which of the drawn markers also get their elevation written.

        Thinned from the markers rather than from the survey, so every label
        sits on a marker that is actually there.
        """
        spacing = SPOT_HEIGHT_SPACING_MM * self.mm_to_model
        return thin_for_display(
            visible, spacing, lambda c: (c.easting, c.northing),
        )

    def draw_topo_points(self):
        visible = self.visible_spot_heights()
        labelled = {id(c) for c in self.labelled_spot_heights(visible)}
        text_height = self.height("spot_height", self.topographic_setting.point_label_scale)

        for coord in visible:
            # Every shot the sheet can hold gets its cross; only those far
            # enough apart to stay readable get the number as well. Thinning
            # them together let the width of a label decide how much of the
            # survey appeared at all.
            self._drawer.draw_topo_point(
                coord.easting, coord.northing, coord.elevation,
                f"{coord.elevation}" if id(coord) in labelled else None,
                text_height,
            )

        drawn, total = len(visible), self.total_survey_points()
        if total > drawn:
            logger.info("drawing %s of %s survey points (%s labelled) at 1:%s",
                        f"{drawn:,}", f"{total:,}", f"{len(labelled):,}", int(self.scale))

    def total_survey_points(self) -> int:
        """Points in the survey, including any thinned away before arrival."""
        return max(int(self.point_totals.get("coordinates", 0)),
                   len(self.coordinates or []))

    def draw_boundary(self):
        if not self.topographic_boundary:
            return

        boundary_points = [(c.easting, c.northing) for c in self.topographic_boundary.coordinates]
        if not boundary_points:
            return

        self._drawer.add_boundary(boundary_points)
        orientation = polygon_orientation(boundary_points)

        for leg in self.topographic_boundary.legs or []:
            self.add_leg_labels(leg, orientation)

    # ------------------------------------------------------------------
    # Contour generation
    # ------------------------------------------------------------------
    def generate_tin_contours(self, smoothing: float = 1.0):
        """Generate contours from a Delaunay triangulation of the points."""
        tri = Delaunay(np.column_stack([self._x, self._y]))
        interpolator = LinearNDInterpolator(tri, self._z)
        grid_x, grid_y, grid_z = self._create_interpolation_grid(interpolator)

        if smoothing > 0:
            grid_z = gaussian_filter(grid_z, sigma=smoothing)

        self._generate_contours(grid_x, grid_y, grid_z)

    def contour_grid_size(self) -> int:
        """Interpolation grid resolution for this sheet.

        Sized from what the paper resolves rather than fixed at 100: a cell of
        `CONTOUR_GRID_CELL_MM` printed means a small site is not over-sampled
        and a large one is not under-sampled, and the cost tracks the sheet
        instead of the survey extent.
        """
        min_x, min_y, max_x, max_y = self._bounding_box
        span = max(max_x - min_x, max_y - min_y, 1e-6)
        cell = CONTOUR_GRID_CELL_MM * self.mm_to_model
        return int(min(max(span / cell, CONTOUR_GRID_MIN), CONTOUR_GRID_MAX))

    def generate_grid_contours(self, grid_size: Optional[int] = None, smoothing: float = 1.0):
        """Generate contours from cubic interpolation over a regular grid."""
        grid_size = grid_size or self.contour_grid_size()
        xi = np.linspace(self._x.min(), self._x.max(), int(grid_size))
        yi = np.linspace(self._y.min(), self._y.max(), int(grid_size))
        grid_x, grid_y = np.meshgrid(xi, yi)

        grid_z = griddata(
            np.column_stack([self._x, self._y]),
            self._z,
            (grid_x, grid_y),
            method="cubic",
        )

        if smoothing > 0:
            grid_z = gaussian_filter(grid_z, sigma=smoothing)

        self._generate_contours(grid_x, grid_y, grid_z)

    # ------------------------------------------------------------------
    # Mesh overlays (drawn on demand, independent of the contour method)
    # ------------------------------------------------------------------
    def draw_tin_mesh(self):
        """Draw the Delaunay triangulation of the survey points."""
        tri = Delaunay(np.column_stack([self._x, self._y]))
        for simplex in tri.simplices:
            triangle = [tuple(self._points[idx]) for idx in simplex]
            triangle.append(triangle[0])  # close the triangle
            self._drawer.add_tin_mesh(triangle)

    def _reference_grid_drawn(self) -> bool:
        settings = self.topographic_setting
        if settings is None:
            return False
        return bool(settings.show_grid or (settings.show_mesh and settings.grid))

    def _labelled_ids(self):
        # Only the boundary beacons carry ids; the survey points themselves
        # are drawn as spot heights, so their ids never reach the margin.
        boundary = self.topographic_boundary
        if boundary is None:
            return []
        return [str(c.id) for c in (boundary.coordinates or []) if c.id not in (None, "")]

    def _label_reach(self):
        # The reference grid prints "(easting, northing)" at each corner,
        # running right from the corner it belongs to -- wider than any
        # boundary beacon id, and the furthest right anything on the sheet is
        # drawn.
        reach_x, reach_y = super()._label_reach()
        if not self._reference_grid_drawn():
            return reach_x, reach_y

        min_x, min_y, max_x, max_y = self._bounding_box
        if max_x is None:
            return reach_x, reach_y
        label_h = self.height("grid_label", 2)
        corner = self._drawer.text_width(f"({max_x:.1f}, {max_y:.1f})", label_h)
        return max(reach_x, corner), max(reach_y, label_h)

    def _origin_value_ceiling(self, default: float) -> float:
        # The reference grid hangs its own "E: ..." labels just under the
        # data, in the same strip the origin easting climbs through, so the
        # origin value stops below them.
        if not self._reference_grid_drawn():
            return default
        label_h = self.height("grid_label", 2)
        return default - label_h - self._drawer.text_width(
            f"E: {default:.2f}", label_h)

    def draw_reference_grid(self, grid_size: int = 100, step: int = 5):
        """Draw a rectangular coordinate grid with easting/northing labels,
        spanning the extent of the survey points at their mean elevation."""
        xi = np.linspace(self._x.min(), self._x.max(), grid_size)
        yi = np.linspace(self._y.min(), self._y.max(), grid_size)
        grid_x, grid_y = np.meshgrid(xi, yi)
        z_grid = float(np.mean(self._z))

        x_min, x_max = grid_x.min(), grid_x.max()
        y_min, y_max = grid_y.min(), grid_y.max()

        # Label height and its clearance from the grid edge are printed sizes,
        # so the annotation reads the same on paper at any scale.
        label_h = self.height("grid_label", 2)
        lead = label_h  # gap between the grid edge and the label

        # Horizontal lines (constant northing) with labels at both edges
        for i in range(0, grid_x.shape[0], step):
            northing = grid_y[i, 0]
            self._drawer.add_grid_mesh([(x_min, northing, z_grid), (x_max, northing, z_grid)])
            self._drawer.add_grid_mesh_label(x_min - lead, northing, z_grid,
                                             f"N: {northing:.2f}", label_h, rotation=0)
            self._drawer.add_grid_mesh_label(x_max + lead / 2, northing, z_grid,
                                             f"{northing:.2f}", label_h, rotation=0)

        # Vertical lines (constant easting) with labels at both edges
        for j in range(0, grid_x.shape[1], step):
            easting = grid_x[0, j]
            self._drawer.add_grid_mesh([(easting, y_min, z_grid), (easting, y_max, z_grid)])
            self._drawer.add_grid_mesh_label(easting, y_min - lead, z_grid,
                                             f"E: {easting:.2f}", label_h, rotation=90)
            self._drawer.add_grid_mesh_label(easting, y_max + lead / 2, z_grid,
                                             f"{easting:.2f}", label_h, rotation=90)

        # Border and corner coordinates
        self._drawer.add_grid_mesh_border([
            (x_min, y_min, z_grid),
            (x_max, y_min, z_grid),
            (x_max, y_max, z_grid),
            (x_min, y_max, z_grid),
            (x_min, y_min, z_grid),
        ])

        for x, y in ((x_min, y_min), (x_max, y_min), (x_max, y_max), (x_min, y_max)):
            self._drawer.add_grid_mesh_label(x, y, z_grid, f"({x:.1f}, {y:.1f})",
                                             label_h, rotation=0)

    def _create_interpolation_grid(self, interpolator, grid_size: Optional[int] = None):
        grid_size = grid_size or self.contour_grid_size()
        xi = np.linspace(self._x.min(), self._x.max(), grid_size)
        yi = np.linspace(self._y.min(), self._y.max(), grid_size)
        grid_x, grid_y = np.meshgrid(xi, yi)

        points = np.column_stack([grid_x.ravel(), grid_y.ravel()])
        grid_z = interpolator(points).reshape(grid_x.shape)

        # Fill gaps outside the triangulation with nearest-neighbour values
        nan_mask = np.isnan(grid_z)
        if np.any(nan_mask):
            grid_z_nearest = griddata(
                np.column_stack([self._x, self._y]),
                self._z,
                (grid_x, grid_y),
                method="nearest",
            )
            grid_z[nan_mask] = grid_z_nearest[nan_mask]

        return grid_x, grid_y, grid_z

    def _clip_polygon(self) -> Optional[Polygon]:
        """Region the contours are confined to: the survey boundary polygon
        when one is supplied, otherwise the convex hull of the spot heights
        (the limit of survey). Returns ``None`` only when neither can form a
        polygon (fewer than three points)."""
        if self.topographic_boundary and self.topographic_boundary.coordinates:
            pts = [(c.easting, c.northing) for c in self.topographic_boundary.coordinates]
            if len(pts) >= 3:
                poly = Polygon(pts)
                # Repair self-intersections/duplicate closing points.
                return poly if poly.is_valid else poly.buffer(0)

        hull = MultiPoint(list(zip(self._x, self._y))).convex_hull
        return hull if isinstance(hull, Polygon) else None

    def _generate_contours(self, grid_x, grid_y, grid_z):
        """Extract contour polylines from gridded data and add them to the DXF."""
        interval = self.topographic_setting.contour_interval
        major = self.topographic_setting.major_contour

        # Confine contours to the survey extent (boundary, else point hull) so
        # they stop at the surveyed outline instead of the data's rectangular
        # bounding box. Grid cells whose centre falls outside are masked; for
        # the TIN path this also discards the nearest-neighbour corner fill.
        z = np.ma.masked_invalid(grid_z)
        clip = self._clip_polygon()
        if clip is not None and not clip.is_empty:
            inside = shapely.contains_xy(clip, grid_x, grid_y)
            z = np.ma.masked_where(~inside, z)

        z_min, z_max = np.nanmin(grid_z), np.nanmax(grid_z)
        levels = np.arange(
            np.floor(z_min / interval) * interval,
            np.ceil(z_max / interval) * interval + interval,
            interval,
        )

        generator = contour_generator(
            x=grid_x, y=grid_y, z=z,
            line_type=LineType.Separate,
        )

        for level in levels:
            level = float(level)
            is_major = abs(level - round(level / major) * major) < 1e-6
            layer = "CONTOUR_MAJOR" if is_major else "CONTOUR_MINOR"

            for path in generator.lines(level):
                if len(path) <= 2:
                    continue

                points_3d = [(float(p[0]), float(p[1]), level) for p in path]
                self._add_smooth_3d_polyline(points_3d, layer)

                if is_major:
                    mid = path[len(path) // 2]
                    self._add_contour_label(float(mid[0]), float(mid[1]), level)

    def _add_smooth_3d_polyline(self, points: List[Tuple[float, float, float]], layer: str):
        if len(points) < 4:
            self._drawer.add_3d_contour(points, layer)
            return
        try:
            self._drawer.add_spline(points, layer)
        except Exception:
            # Fall back to a plain polyline when spline fitting fails
            self._drawer.add_3d_contour(points, layer)

    def _add_contour_label(self, x: float, y: float, elevation: float):
        self._drawer.add_contour_label(
            x, y, elevation, f"{elevation:.2f}",
            self.height("contour_label", self.topographic_setting.contour_label_scale),
        )

    def draw_topo_map(self):
        settings = self.topographic_setting

        if settings.tin:
            self.generate_tin_contours(1.5)
        if settings.grid:
            self.generate_grid_contours(smoothing=1.5)

        # TIN mesh and coordinate grid are optional sheet overlays, switchable
        # independently of the contour method. `show_mesh` is the legacy single
        # toggle (tied to the active method) and is honoured for old payloads.
        show_tin_mesh = settings.show_tin_mesh or (settings.show_mesh and settings.tin)
        show_grid = settings.show_grid or (settings.show_mesh and settings.grid)

        if show_tin_mesh:
            self.draw_tin_mesh()
        if show_grid:
            self.draw_reference_grid()

        self._drawer.toggle_layer("SPOT_HEIGHTS", settings.show_spot_heights)
        self._drawer.toggle_layer("CONTOUR_MAJOR", settings.show_contours)
        self._drawer.toggle_layer("CONTOUR_MINOR", settings.show_contours)
        self._drawer.toggle_layer("CONTOUR_LABELS", settings.show_contours_labels)
        self._drawer.toggle_layer("BOUNDARY", settings.show_boundary)
        self._drawer.toggle_layer("TIN_MESH", bool(show_tin_mesh))
        self._drawer.toggle_layer("GRID_MESH", bool(show_grid))

    def draw(self):
        self.draw_beacons()
        self.draw_topo_points()
        self.draw_boundary()
        self.draw_frames()
        self.draw_title_block()
        self.draw_footer_boxes()
        self.draw_topo_map()
        self.draw_tables()
        self.draw_north_arrow()
