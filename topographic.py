import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ezdxf.enums import TextEntityAlignment
from pydantic import PrivateAttr
from dxf import SurveyDXFManager
from models.plan import PlanProps, PlanType
from utils import polygon_orientation, line_normals, line_direction, html_to_mtext
from scipy.interpolate import griddata, RBFInterpolator
from scipy.ndimage import gaussian_filter
from scipy.spatial.distance import cdist
from matplotlib.tri import Triangulation

import math
import numpy as np


def apply_minimum_distance_filter(coordinates, min_distance):
    """
    Filter contour points to maintain minimum distance between points
    """
    if len(coordinates) < 3 or min_distance <= 0:
        return coordinates

    filtered_coords = [coordinates[0]]  # Always keep first point

    for i in range(1, len(coordinates)):
        current_point = coordinates[i]
        last_kept_point = filtered_coords[-1]

        # Calculate distance to last kept point
        distance = np.sqrt((current_point[0] - last_kept_point[0]) ** 2 +
                           (current_point[1] - last_kept_point[1]) ** 2)

        if distance >= min_distance:
            filtered_coords.append(current_point)

    # Always keep the last point if it's not already kept
    if len(filtered_coords) > 1 and not np.array_equal(filtered_coords[-1], coordinates[-1]):
        filtered_coords.append(coordinates[-1])

    return filtered_coords

def calculate_average_point_spacing(x, y):
    """Calculate average distance between survey points"""
    if len(x) < 2:
        return 0

    points = np.column_stack((x, y))
    distances = cdist(points, points)

    # Get non-zero distances (exclude self-distances)
    non_zero_distances = distances[distances > 0]

    return np.mean(non_zero_distances) if len(non_zero_distances) > 0 else 0


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
        if not self._frame_coords:
            raise ValueError("Cannot determine frame coordinates without valid coordinates.")
        self._drawer = self._setup_drawer()

    def _setup_drawer(self) -> SurveyDXFManager:
        drawer = SurveyDXFManager(plan_name=self.name, scale=self.get_drawing_scale())
        drawer.setup_font(self.font)
        drawer.setup_beacon_style(self.beacon_type, self.beacon_size)
        drawer.setup_topo_point_style(type_="cross", size=0.1 * self.topographic_setting.point_label_scale)
        drawer.setup_graphical_scale_style(length=(self._frame_coords[2] - self._frame_coords[0]) * 0.4)
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

    def draw_beacons(self):
        if not self.topographic_boundary:
            return

        check = []
        for coord in self.topographic_boundary.coordinates:
            if coord.id in check:
                continue
            self._drawer.draw_beacon(coord.easting, coord.northing, 0, self.label_scale, coord.id)
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

        self._drawer.add_boundary(boundary_points)
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
        first_x = leg.from_.easting + 0.2 * (leg.to.easting - leg.from_.easting)
        first_y = leg.from_.northing + 0.2 * (leg.to.northing - leg.from_.northing)
        last_x = leg.from_.easting + 0.8 * (leg.to.easting - leg.from_.easting)
        last_y = leg.from_.northing + 0.8 * (leg.to.northing - leg.from_.northing)
        mid_x = (leg.from_.easting + leg.to.easting) / 2
        mid_y = (leg.from_.northing + leg.to.northing) / 2

        # Offset text above/below the line
        normals = line_normals((leg.from_.easting, leg.from_.northing), (leg.to.easting, leg.to.northing), orientation)
        offset_distance = 1 * self.get_drawing_scale()
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
                              angle=text_angle, height=self.label_scale)
        ld = line_direction(angle_deg)
        if ld == "left → right":
            self._drawer.add_text(f"{leg.bearing.degrees}°", first_x, first_y,
                                  angle=text_angle, height=self.label_scale)
            self._drawer.add_text(f"{leg.bearing.minutes}'", last_x, last_y,
                                  angle=text_angle, height=self.label_scale)
        else:
            self._drawer.add_text(f"{leg.bearing.degrees}°", last_x, last_y,
                                  angle=text_angle, height=self.label_scale)
            self._drawer.add_text(f"{leg.bearing.minutes}'", first_x, first_y,
                                  angle=text_angle, height=self.label_scale)

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
            self._drawer.draw_footer_box(html_to_mtext(footer), x1, y1, x2, y2, self.footer_scale)

    def draw_contours(self):
        if not self.coordinates:
            return

        no_of_coordinates = len(self.coordinates)

        if no_of_coordinates < 3:
            return

        x, y, z = np.array([]), np.array([]), np.array([])

        for coord in self.coordinates:
            x = np.append(x, coord.easting)
            y = np.append(y, coord.northing)
            z = np.append(z, coord.elevation)

        from scipy.spatial import Delaunay
        points = np.column_stack((x, y))
        tri = Delaunay(points)

        centroids = []
        centroid_elevations = []

        for simplex in tri.simplices:
            triangle_points = points[simplex]
            triangle_z = z[simplex]

            centroid = np.mean(triangle_points, axis=0)
            centroid_z = np.mean(triangle_z)

            centroids.append(centroid)
            centroid_elevations.append(centroid_z)

        Xi = np.concatenate([x, [c[0] for c in centroids]])
        Yi = np.concatenate([y, [c[1] for c in centroids]])
        Zi = np.concatenate([z, centroid_elevations])

        """Generate smooth contours using grid interpolation"""
        x_min, x_max = x.min(), x.max()
        y_min, y_max = y.min(), y.max()
        z_min, z_max = z.min(), z.max()

        # Add padding to avoid edge effects
        # padding = 0.1
        # x_range = x_max - x_min
        # y_range = y_max - y_min
        #
        # # calculate area
        # area = (x_max - x_min) * (y_max - y_min)

        # calculate grid resolution
        # grid_resolution = 100  # default
        # if area > 0:
        #     # Base resolution on survey area and point density
        #     point_density = no_of_coordinates / area  # points per square unit
        #     grid_resolution = max(50, min(200, int(np.sqrt(area) * point_density * 0.5)))
        #
        # xi = np.linspace(x_min - padding * x_range, x_max + padding * x_range, grid_resolution)
        # yi = np.linspace(y_min - padding * y_range, y_max + padding * y_range, grid_resolution)
        # Xi, Yi = np.meshgrid(xi, yi)

        # Interpolate elevations
        # Zi = griddata((x, y), z, (Xi, Yi), method='cubic', fill_value=np.nan)

        # Apply smoothing
        # smoothing = 1.2
        # if smoothing > 0:
        #     Zi = gaussian_filter(Zi, sigma=smoothing)
        #     print(f"Applied smoothing (factor: {smoothing})")

        # Start from the first multiple of contour_interval above z_min
        start_elevation = np.ceil(z_min / self.topographic_setting.contour_interval) * self.topographic_setting.contour_interval
        end_elevation = np.floor(z_max / self.topographic_setting.contour_interval) * self.topographic_setting.contour_interval

        # generate levels
        num_levels = int((end_elevation - start_elevation) / self.topographic_setting.contour_interval) + 1
        levels = np.linspace(start_elevation, end_elevation, num_levels)

        # Identify major contour levels
        major_levels = []
        for level in levels:
            if abs(level % self.topographic_setting.major_contour) < 0.001:  # Account for floating point precision
                major_levels.append(level)

        # Draw contours
        triangulation = Triangulation(Xi, Yi)
        fig, ax = plt.subplots(figsize=(1, 1))
        contours = ax.tricontour(triangulation, Zi, levels=levels)

        contour_data = []
        points_filtered = 0

        if hasattr(contours, 'allsegs') and len(contours.allsegs) > 0:
            for level_idx, level_segments in enumerate(contours.allsegs):
                elevation = levels[level_idx]
                is_major = elevation in major_levels
                layer_name = 'CONTOURS_MAJOR' if is_major else 'CONTOURS_MINOR'

                for segment in level_segments:
                    if len(segment) < 2:
                        continue

                    # Apply minimum distance filter
                    original_points = len(segment)
                    filtered_segment = apply_minimum_distance_filter(segment, self.topographic_setting.minimum_distance)
                    points_filtered += original_points - len(filtered_segment)

                    if len(filtered_segment) < 2:
                        continue

                    points = [(float(x), float(y), float(elevation)) for x, y in filtered_segment]

                    # Add polyline to DXF
                    polyline = self._drawer.msp.add_polyline3d(
                        points,
                        dxfattribs={'layer': layer_name}
                    )

                    # Add elevation labels for major contours
                    if is_major and len(points) >= 3:
                        mid_idx = len(points) // 2
                        label_x, label_y, _ = points[mid_idx]

                        text_height = self.topographic_setting.contour_label_scale

                        self._drawer.msp.add_text(
                            f"{elevation:.1f}",
                            dxfattribs={
                                'layer': 'CONTOUR_LABELS',
                                'height': text_height,
                                'style': 'Standard'
                            }
                        ).set_placement((label_x, label_y), align=TextEntityAlignment.MIDDLE_CENTER)

        # plt.close(fig)


    def draw(self):
        # Draw elements
        self.draw_topo_points()
        # self.draw_beacons()
        # self.draw_boundary()
        self.draw_contours()
        self.draw_frames()
        self.draw_title_block()
        self.draw_footer_boxes()

    def save_dxf(self, file_path: str):
        self._drawer.save_dxf(file_path)

    def save(self) -> str:
        return self._drawer.save(paper_size=self.page_size, orientation=self.page_orientation)