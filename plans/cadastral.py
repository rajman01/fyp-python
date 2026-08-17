"""Cadastral survey plan generator.

Draws property beacons, parcel boundaries with bearing/distance labels,
and the standard plan furniture (frame, title block, footers, north arrow).
"""

from typing import ClassVar, Optional

from dxf_manager import SurveyDXFManager
from models.plan import CoordinateProps, PlanType
from plans.base import BasePlan
from utils import polygon_orientation


class CadastralPlan(BasePlan):
    expected_type: ClassVar[PlanType] = PlanType.CADASTRAL

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._coord_dict = {coord.id: coord for coord in self.coordinates or []}

    def _setup_layers(self, drawer: SurveyDXFManager):
        drawer.setup_cadastral_layers()
        drawer.setup_beacon_style(self.beacon_type, self.beacon_size)

    def _area_text(self) -> str:
        if self.parcels and self.parcels[0].area is not None:
            return self._format_area(self.parcels[0].area)
        return ""

    def _north_arrow_reference(self) -> Optional[CoordinateProps]:
        if not self.parcels or not self.parcels[0].ids:
            return None
        return self._coord_dict.get(self.parcels[0].ids[0])

    def draw_beacons(self):
        for coord in self.coordinates or []:
            self._drawer.draw_beacon(
                coord.easting, coord.northing, 0,
                self.label_size, self._get_drawing_extent(), coord.id,
            )

    def draw_parcels(self):
        if not self.parcels:
            return

        for parcel in self.parcels:
            parcel_points = [
                (self._coord_dict[pid].easting, self._coord_dict[pid].northing)
                for pid in parcel.ids if pid in self._coord_dict
            ]
            if not parcel_points:
                continue

            self._drawer.add_parcel(parcel_points)
            orientation = polygon_orientation(parcel_points)

            for leg in parcel.legs:
                self.add_leg_labels(leg, orientation)

    def draw(self):
        self.draw_beacons()
        self.draw_parcels()
        self.draw_frames()
        self.draw_title_block()
        self.draw_footer_boxes()
        self.draw_north_arrow()
