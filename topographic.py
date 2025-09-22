import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from pydantic import PrivateAttr
from dxf import SurveyDXFManager
from models.plan import PlanProps, PlanType
from utils import polygon_orientation, line_normals, line_direction, html_to_mtext
from scipy.interpolate import griddata, LinearNDInterpolator
from scipy.ndimage import gaussian_filter
from scipy.spatial import Delaunay
from typing import List, Tuple, Optional

import math
import numpy as np

class TopographicPlan(PlanProps):
    _drawer: SurveyDXFManager = PrivateAttr()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.type != PlanType.TOPOGRAPHIC:
            raise ValueError("TopographicPlan must have type PlanType.TOPOGRAPHIC")

        self._frame_x_percent = 0.35
        self._frame_y_percent = 0.8
        self._bounding_box = self.get_bounding_box()
        self._frame_coords = self._setup_frame_coords()
        self._boundary_dict = {coord.id: coord for coord in self.topographic_boundary.coordinates}
        if not self._frame_coords:
            raise ValueError("Cannot determine frame coordinates without valid coordinates.")

        # Extract coordinates
        self._points = self._setup_topo_points()
        if self._points is None:
            raise ValueError("Cannot determine topographic points without valid coordinates.")
        self._x = self._points[:, 0]
        self._y = self._points[:, 1]
        self._z = self._points[:, 2]

        self._drawer = self._setup_drawer()

    def _setup_drawer(self) -> SurveyDXFManager:
        drawer = SurveyDXFManager(plan_name=self.name, scale=self.get_drawing_scale())
        drawer.setup_font(self.font)
        drawer.setup_beacon_style(self.beacon_type, self.beacon_size)
        drawer.setup_topo_point_style(type_="cross", size=0.5 * self.topographic_setting.point_label_scale)
        return drawer

    def _setup_frame_coords(self):
        min_x, min_y, max_x, max_y = self._bounding_box
        if min_x is None or min_y is None or max_x is None or max_y is None:
            return None

        width = max_x - min_x
        height = max_y - min_y

        margin_x = max(width, height) * self._frame_x_percent
        margin_y = max(height, width) * self._frame_y_percent

        frame_left = min_x - margin_x
        frame_bottom = min_y - margin_y
        frame_right = max_x + margin_x
        frame_top = max_y + margin_y

        return frame_left, frame_bottom, frame_right, frame_top

    def _setup_topo_points(self):
        if not self.coordinates:
            return None

        # list of tuples
        pts = [(coord.easting, coord.northing, coord.elevation) for coord in self.coordinates]
        return np.array(pts)

    def _get_drawing_extent(self) -> float:
        # get bounding box
        min_x, min_y, max_x, max_y = self._bounding_box
        if min_x is None or min_y is None or max_x is None or max_y is None:
            return 0.0

        width = max_x - min_x
        height = max_y - min_y
        extent = math.sqrt(width ** 2 + height ** 2)
        return extent

    def draw_beacons(self):
        if not self.topographic_boundary:
            return

        check = []
        for coord in self.topographic_boundary.coordinates:
            if coord.id in check:
                continue
            self._drawer.draw_beacon(coord.easting, coord.northing, 0, self.label_size, self._get_drawing_extent(), coord.id)
            check.append(coord.id)

    def draw_topo_points(self):
        if not self.coordinates:
            return

        for coord in self.coordinates:
            self._drawer.draw_topo_point(coord.easting, coord.northing, coord.elevation, f"{coord.elevation}", self.topographic_setting.point_label_scale)

    def draw_boundary(self):
        if not self.topographic_boundary:
            return

        boundary_points = [(coord.easting, coord.northing) for coord in self.topographic_boundary.coordinates]
        if not boundary_points:
            return

        (self._drawer.
         add_boundary(boundary_points))
        orientation = polygon_orientation(boundary_points)

        for leg in self.topographic_boundary.legs:
            self.add_leg_labels(leg, orientation)

    def add_leg_labels(self, leg, orientation: str):
        """Add distance and bearing labels to a leg."""
        # Angle and positions
        angle_rad = math.atan2(leg.to.northing - leg.from_.northing,
                               leg.to.easting - leg.from_.easting)
        angle_deg = math.degrees(angle_rad)

        # Fractional positions
        first_x = leg.from_.easting + (0.2 * (leg.to.easting - leg.from_.easting))
        first_y = leg.from_.northing + (0.2 * (leg.to.northing - leg.from_.northing))
        last_x = leg.from_.easting + (0.8 * (leg.to.easting - leg.from_.easting))
        last_y = leg.from_.northing + (0.8 * (leg.to.northing - leg.from_.northing))
        mid_x = (leg.from_.easting + leg.to.easting) / 2
        mid_y = (leg.from_.northing + leg.to.northing) / 2

        # Offset text above/below the line
        normals = line_normals((leg.from_.easting, leg.from_.northing), (leg.to.easting, leg.to.northing), orientation)
        offset_distance = self._get_drawing_extent() * 0.02
        offset_inside_x = (normals[0][0] / math.hypot(*normals[0])) * offset_distance
        offset_inside_y = (normals[0][1] / math.hypot(*normals[0])) * offset_distance
        offset_outside_x = (normals[1][0] / math.hypot(*normals[1])) * offset_distance
        offset_outside_y = (normals[1][1] / math.hypot(*normals[1])) * offset_distance

        first_x += offset_outside_x
        first_y += offset_outside_y
        last_x += offset_outside_x
        last_y += offset_outside_y
        mid_x += offset_inside_x
        mid_y += offset_inside_y

        # Text angle adjustment
        text_angle = angle_deg
        if text_angle > 90 or text_angle < -90:
            text_angle += 180

        # Add labels
        self._drawer.add_text(f"{leg.distance:.2f} m", mid_x, mid_y,
                              angle=text_angle, height=self.label_size)
        ld = line_direction(angle_deg)
        if ld == "left → right":
            self._drawer.add_text(f"{leg.bearing.degrees}°", first_x, first_y,
                                  angle=text_angle, height=self.label_size)
            self._drawer.add_text(f"{leg.bearing.minutes}'", last_x, last_y,
                                  angle=text_angle, height=self.label_size)
        else:
            self._drawer.add_text(f"{leg.bearing.degrees}°", last_x, last_y,
                                  angle=text_angle, height=self.label_size)
            self._drawer.add_text(f"{leg.bearing.minutes}'", first_x, first_y,
                                  angle=text_angle, height=self.label_size)

    def draw_frames(self):
        """Draw outer and offset frames."""
        min_x, min_y, max_x, max_y = self._bounding_box
        width, height = max_x - min_x, max_y - min_y

        margin_x, margin_y = max(width, height) * self._frame_x_percent, max(height, width) * self._frame_y_percent
        frame_left, frame_bottom = min_x - margin_x, min_y - margin_y
        frame_right, frame_top = max_x + margin_x, max_y + margin_y
        self._drawer.draw_frame(frame_left, frame_bottom, frame_right, frame_top)

        offset_x, offset_y = max(width, height) * (self._frame_x_percent + 0.03), max(height, width) * (
                    self._frame_y_percent + 0.03)
        self._drawer.draw_frame(min_x - offset_x, min_y - offset_y,
                                max_x + offset_x, max_y + offset_y)

    def draw_title_block(self):
        """Add title block to the frame."""
        min_x, min_y, max_x, max_y = self._bounding_box
        width, height = max_x - min_x, max_y - min_y

        margin_x, margin_y = max(width, height) * self._frame_x_percent, max(height, width) * self._frame_y_percent
        frame_left, frame_bottom = min_x - margin_x, min_y - margin_y
        frame_right, frame_top = max_x + margin_x, max_y + margin_y

        frame_width = frame_right - frame_left
        frame_center_x = frame_left + (frame_width / 2)

        title_y = frame_top - (margin_y * 0.2)
        self._drawer.draw_title_block(html_to_mtext(self.build_title()),
                                      frame_center_x,
                                      title_y,
                                      frame_width * 0.6,
                                      self.font_size,
                                      graphical_scale_length=(self._frame_coords[2] - self._frame_coords[0]) * 0.4,
                                      area=f"AREA :- {self.topographic_boundary.area} SQ.METRES",
                                      origin=f"ORIGIN :- {self.origin.upper()}")

    def draw_footer_boxes(self):
        if len(self.footers) == 0:
            return

        x_min = self._frame_coords[0]
        y_min = self._frame_coords[1]
        x_max = self._frame_coords[2]
        y_max = self._frame_coords[3]

        box_width = (x_max - x_min) / len(self.footers)
        box_height = (y_max - y_min) * 0.25

        for i, footer in enumerate(self.footers):
            x1 = x_min + i * box_width
            x2 = x1 + box_width
            y1 = y_min
            y2 = y1 + box_height
            self._drawer.draw_footer_box(html_to_mtext(footer), x1, y1, x2, y2, self.footer_size)

    def generate_tin_contours(self, smoothing: float = 1.0):
        """
                Generate contours using the Triangulated Irregular Network (TIN) method

                Args:
                    smoothing: Smoothing factor for contours (0-3)
                    show_mesh: Whether to add TIN mesh to drawing

                Returns:
                    Dictionary with contour data and statistics
        """

        # Create Delaunay triangulation
        tri = Delaunay(np.column_stack([self._x, self._y]))

        # Add TIN mesh
        self._add_tin_mesh(tri)

        # Create interpolator
        interpolator = LinearNDInterpolator(tri, self._z)

        # Generate grid for contouring
        grid_x, grid_y, grid_z = self._create_interpolation_grid(interpolator)

        # Apply smoothing if requested
        if smoothing > 0:
            grid_z = gaussian_filter(grid_z, sigma=smoothing)

        # Generate contours
        self._generate_contours(grid_x, grid_y, grid_z)

    def generate_grid_contours(self, grid_size: float = 100.0, smoothing: float  = 1.0):
        """
              Generate contours using the grid interpolation method

              Args:
                  method: Interpolation method ('linear', 'cubic', 'nearest')
                  grid_size: Number of grid points in each direction
                  smoothing: Smoothing factor for contours (0-3)
                  show_mesh: Whether to add grid mesh to drawing

              Returns:
                  Dictionary with contour data and statistics
              """

        # Create regular grid
        xi = np.linspace(self._x.min(), self._x.max(), grid_size)
        yi = np.linspace(self._y.min(), self._y.max(), grid_size)
        grid_x, grid_y = np.meshgrid(xi, yi)

        # Interpolate to grid
        grid_z = griddata(
            np.column_stack([self._x, self._y]),
            self._z,
            (grid_x, grid_y),
            method="cubic"
        )

        # Apply smoothing
        if smoothing > 0:
            grid_z = gaussian_filter(grid_z, sigma=smoothing)

        # Add grid mesh
        self._add_grid_mesh(grid_x, grid_y, grid_z)

        # Generate contours
        self._generate_contours(grid_x, grid_y, grid_z)

    def _add_tin_mesh(self, tri: Delaunay):
        """Add TIN mesh to the drawing"""
        for simplex in tri.simplices:
            # Get triangle vertices
            triangle_points = []
            for vertex_idx in simplex:
                point = self._points[vertex_idx]
                triangle_points.append(point)

            # Close the triangle
            triangle_points.append(triangle_points[0])

            # Add as 3D polyline
            self._drawer.add_tin_mesh(triangle_points)

    # def _add_grid_mesh(self, grid_x, grid_y, grid_z, step: int = 5):
    #     """Add grid mesh to the drawing"""
    #     # Add horizontal grid lines
    #     for i in range(0, grid_x.shape[0], step):
    #         points = []
    #         for j in range(grid_x.shape[1]):
    #             if not np.isnan(grid_z[i, j]):
    #                 points.append((grid_x[i, j], grid_y[i, j], grid_z[i, j]))
    #
    #         if len(points) > 1:
    #             self._drawer.add_grid_mesh(points)
    #
    #     # Add vertical grid lines
    #     for j in range(0, grid_x.shape[1], step):
    #         points = []
    #         for i in range(grid_x.shape[0]):
    #             if not np.isnan(grid_z[i, j]):
    #                 points.append((grid_x[i, j], grid_y[i, j], grid_z[i, j]))
    #
    #         if len(points) > 1:
    #             self._drawer.add_grid_mesh(points)

    def _add_grid_mesh(self, grid_x, grid_y, grid_z, step: int = 5, elevation: Optional[float] = None):
        """Add rectangular grid mesh with easting and northing labels

              Args:
                  grid_x: X coordinates grid
                  grid_y: Y coordinates grid
                  grid_z: Z coordinates grid
                  step: Step size for grid lines
                  elevation: Fixed elevation for grid (None = use average elevation)
              """
        # Get grid bounds
        x_min, x_max = grid_x.min(), grid_x.max()
        y_min, y_max = grid_y.min(), grid_y.max()

        # Determine grid elevation
        if elevation is None:
            z_grid = np.nanmean(grid_z)  # Use average elevation
        else:
            z_grid = elevation

        # Calculate grid line positions
        x_lines = np.arange(0, grid_x.shape[1], step)
        y_lines = np.arange(0, grid_x.shape[0], step)

        # Add horizontal grid lines (constant northing)
        for i in y_lines:
            northing = grid_y[i, 0]

            # Create a full horizontal line across the grid
            points = [
                (x_min, northing, z_grid),
                (x_max, northing, z_grid)
            ]

            self._drawer.add_grid_mesh(points)

            # Add northing label at the left edge
            self._drawer.add_grid_mesh_label(x_min - 2, northing, z_grid, f'N: {northing:.2f}', 2, rotation=0)

            # Add northing label at the right edge (optional)
            self._drawer.add_grid_mesh_label(x_max + 1, northing, z_grid, f'{northing:.2f}', 2, rotation=0)

        # Add vertical grid lines (constant easting)
        for j in x_lines:
            easting = grid_x[0, j]

            # Create a full vertical line across the grid
            points = [
                (easting, y_min, z_grid),
                (easting, y_max, z_grid)
            ]

            self._drawer.add_grid_mesh(points)

            # Add an easting label at the bottom edge
            self._drawer.add_grid_mesh_label(easting, y_min - 2, z_grid, f'E: {easting:.2f}', 2, rotation=90)

            # Add an easting label at the top edge (optional)
            self._drawer.add_grid_mesh_label(easting, y_max + 1, z_grid,  f'{easting:.2f}', 2, rotation=90)

        # Add border rectangle
        border_points = [
            (x_min, y_min, z_grid),
            (x_max, y_min, z_grid),
            (x_max, y_max, z_grid),
            (x_min, y_max, z_grid),
            (x_min, y_min, z_grid)  # Close the rectangle
        ]

        self._drawer.add_grid_mesh_border(border_points)

        # Add corner coordinate labels
        corners = [
            (x_min, y_min, f"({x_min:.1f}, {y_min:.1f})"),
            (x_max, y_min, f"({x_max:.1f}, {y_min:.1f})"),
            (x_max, y_max, f"({x_max:.1f}, {y_max:.1f})"),
            (x_min, y_max, f"({x_min:.1f}, {y_max:.1f})")
        ]

        for x, y, label in corners:
            self._drawer.add_grid_mesh_corner_coords(x, y, z_grid, label, 2, 0)

    def _create_interpolation_grid(self, interpolator, grid_size: int = 100):
        """Create interpolation grid for TIN method"""
        xi = np.linspace(self._x.min(), self._x.max(), grid_size)
        yi = np.linspace(self._y.min(), self._y.max(), grid_size)
        grid_x, grid_y = np.meshgrid(xi, yi)

        # Flatten for interpolation
        points = np.column_stack([grid_x.ravel(), grid_y.ravel()])
        grid_z = interpolator(points).reshape(grid_x.shape)

        # Handle NaN values
        if np.any(np.isnan(grid_z)):
            grid_z_nearest = griddata(
                np.column_stack([self._x, self._y]),
                self._z,
                (grid_x, grid_y),
                method='nearest'
            )
            grid_z[np.isnan(grid_z)] = grid_z_nearest[np.isnan(grid_z)]

        return grid_x, grid_y, grid_z

    def _generate_contours(self, grid_x, grid_y, grid_z):
        """Generate contour lines from gridded data"""
        # Calculate contour levels
        z_min, z_max = np.nanmin(grid_z), np.nanmax(grid_z)
        levels = np.arange(
            np.floor(z_min / self.topographic_setting.contour_interval) * self.topographic_setting.contour_interval,
            np.ceil(z_max / self.topographic_setting.contour_interval) * self.topographic_setting.contour_interval + self.topographic_setting.contour_interval,
            self.topographic_setting.contour_interval
        )

        # Generate contours using matplotlib
        fig, ax = plt.subplots(figsize=(10, 10))
        cs = ax.contour(grid_x, grid_y, grid_z, levels=levels)
        plt.close(fig)

        # Extract contour paths and add to DXF as smooth 3D polylines
        for level_idx, level in enumerate(cs.levels):
            is_major = abs(level % self.topographic_setting.major_contour) < 0.001
            layer = 'CONTOUR_MAJOR' if is_major else 'CONTOUR_MINOR'

            # Get all paths for this level
            paths = cs.allsegs[level_idx]

            for path in paths:
                if len(path) > 2:
                    # Convert to 3D points (add Z coordinate)
                    points_3d = [(p[0], p[1], level) for p in path]

                    # Create smooth 3D polyline using spline
                    self._add_smooth_3d_polyline(points_3d, layer)

                    # Add elevation label for major contours
                    # if is_major and len(path) > 10:
                    if is_major:
                        mid_idx = len(path) // 2
                        self._add_contour_label(float(path[mid_idx][0]), float(path[mid_idx][1]), level)

    def _add_smooth_3d_polyline(self, points: List[Tuple[float, float, float]],
                                layer: str):
        """Add a smooth 3D polyline using B-spline"""
        if len(points) < 4:
            # For short segments, use a simple polyline
            self._drawer.add_3d_contour(points, layer)
        else:
            # Create B-spline for smooth curves
            try:
                self._drawer.add_spline(points, layer)
            except:
                # Fallback to polyline if spline fails
                self._drawer.add_3d_contour(points, layer)

    def _add_contour_label(self, x: float, y: float, elevation: float):
        """Add elevation label to contour"""
        self._drawer.add_contour_label(
            x,
            y,
            elevation,
            f"{elevation:.2f}",
            self.topographic_setting.contour_label_scale
        )

    def draw_topo_map(self):
        if self.topographic_setting.tin:
            self.generate_tin_contours(1.5)

        if self.topographic_setting.grid:
            self.generate_grid_contours(100, 1.5)

        self._drawer.toggle_layer("SPOT_HEIGHTS", self.topographic_setting.show_spot_heights)
        self._drawer.toggle_layer("CONTOUR_MAJOR", self.topographic_setting.show_contours)
        self._drawer.toggle_layer("CONTOUR_MINOR", self.topographic_setting.show_contours)
        self._drawer.toggle_layer("CONTOUR_LABELS", self.topographic_setting.show_contours_labels)
        self._drawer.toggle_layer("BOUNDARY", self.topographic_setting.show_boundary)

        if self.topographic_setting.tin:
            self._drawer.toggle_layer("TIN_MESH", self.topographic_setting.show_mesh)

        if self.topographic_setting.grid:
            self._drawer.toggle_layer("GRID_MESH", self.topographic_setting.show_mesh)

    def draw_north_arrow(self):
        if len(self.topographic_boundary.coordinates) == 0:
            return

        coord = self._boundary_dict[self.topographic_boundary.coordinates[0].id]
        height = (self._frame_coords[3] - self._frame_coords[1]) * 0.07
        self._drawer.draw_north_arrow(coord.easting, self._frame_coords[3] - height, height)

    def draw(self):
        # Draw elements
        self.draw_beacons()
        self.draw_topo_points()
        self.draw_boundary()
        self.draw_frames()
        self.draw_title_block()
        self.draw_footer_boxes()
        self.draw_topo_map()
        self.draw_north_arrow()

    def save_dxf(self, file_path: str):
        self._drawer.save_dxf(file_path)

    def save(self) -> str:
        return self._drawer.save(paper_size=self.page_size, orientation=self.page_orientation)
