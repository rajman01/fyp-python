from dxf import SurveyDXFManager
from models.plan import PlanProps, PlanType
from utils import polygon_orientation, line_normals, line_direction, html_to_mtext
from pydantic import PrivateAttr

import math

class CadastralPlan(PlanProps):
    _drawer: SurveyDXFManager = PrivateAttr()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.type != PlanType.CADASTRAL:
            raise ValueError("CadastralPlan must have type PlanType.CADASTRAL")

        self._drawer = self._setup_drawer()

    def _setup_drawer(self) -> SurveyDXFManager:
        drawer = SurveyDXFManager(plan_name=self.name, scale=self.get_drawing_scale())
        drawer.setup_font(self.font)
        drawer.setup_beacon_style(self.beacon_type, self.beacon_size)
        return drawer

    def draw_beacons(self):
        if not self.coordinates:
            return

        for coord in self.coordinates:
            self._drawer.draw_beacon(coord.easting, coord.northing, 0, self.label_scale, coord.id)

    def draw_parcels(self):
        if not self.parcels or not self.coordinates:
            return

        # create a dictionary of coordinates for easy lookup
        coord_dict = {coord.id: coord for coord in self.coordinates}

        for parcel in self.parcels:
            parcel_points = [(coord_dict[pid].easting, coord_dict[pid].northing)
                             for pid in parcel.ids if pid in coord_dict]

            if not parcel_points:
                continue

            self._drawer.add_parcel(parcel.name, parcel_points, label_scale=self.label_scale)
            orientation = polygon_orientation(parcel_points)

            for leg in parcel.legs:
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

        first_x += offset_outside_x; first_y += offset_outside_y
        last_x += offset_outside_x; last_y += offset_outside_y
        mid_x += offset_inside_x; mid_y += offset_inside_y

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
        min_x, min_y, max_x, max_y = self.get_bounding_box()
        width, height = max_x - min_x, max_y - min_y

        margin_x, margin_y = max(width, height) * 0.35, max(height, width) * 0.7
        frame_left, frame_bottom = min_x - margin_x, min_y - margin_y
        frame_right, frame_top = max_x + margin_x, max_y + margin_y
        self._drawer.draw_frame(frame_left, frame_bottom, frame_right, frame_top)

        offset_x, offset_y = max(width, height) * 0.38, max(height, width) * 0.73
        self._drawer.draw_frame(min_x - offset_x, min_y - offset_y,
                          max_x + offset_x, max_y + offset_y)

    def draw_title(self):
        """Add title block to the frame."""
        min_x, min_y, max_x, max_y = self.get_bounding_box()
        width, height = max_x - min_x, max_y - min_y

        margin_x, margin_y = max(width, height) * 0.35, max(height, width) * 0.7
        frame_left, frame_bottom = min_x - margin_x, min_y - margin_y
        frame_right, frame_top = max_x + margin_x, max_y + margin_y

        frame_width = frame_right - frame_left
        frame_center_x = frame_left + (frame_width / 2)

        title_y = frame_top - (margin_y * 0.2)
        self._drawer.add_title(html_to_mtext(self.build_title()),
                         frame_center_x,
                         title_y,
                         frame_width * 0.6,
                         self.font_size)

    def draw(self):
        # Draw elements
        self.draw_beacons()
        self.draw_parcels()
        self.draw_frames()
        self.draw_title()

    def save_dxf(self, file_path: str):
        self._drawer.save_dxf(file_path)

    def save(self) -> str:
        return self._drawer.save(paper_size=self.page_size, orientation=self.page_orientation)





