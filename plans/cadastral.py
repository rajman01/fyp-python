"""Cadastral survey plan generator.

Draws property beacons, parcel boundaries with bearing/distance labels,
and the standard plan furniture (frame, title block, footers, north arrow).
"""

from typing import ClassVar, Optional

from dxf_manager import SurveyDXFManager
from models.plan import CoordinateProps, PlanType
from plans.base import BasePlan, TableSpec
from utils import polygon_orientation


class CadastralPlan(BasePlan):
    expected_type: ClassVar[PlanType] = PlanType.CADASTRAL
    draws_north_arrow: ClassVar[bool] = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._coord_dict = {coord.id: coord for coord in self.coordinates or []}

    def _setup_layers(self, drawer: SurveyDXFManager):
        drawer.setup_cadastral_layers()
        drawer.setup_beacon_style(self.beacon_type, self.beacon_symbol_size)

    def _area_text(self) -> str:
        if self.parcels and self.parcels[0].area is not None:
            return self._format_area(self.parcels[0].area)
        return ""

    def _north_arrow_reference(self) -> Optional[CoordinateProps]:
        if not self.parcels or not self.parcels[0].ids:
            return None
        return self._coord_dict.get(self.parcels[0].ids[0])

    def _bearing_distance_table(self):
        """Legs of every parcel on the plan."""
        rows = []
        for parcel in self.parcels or []:
            rows.extend(self._leg_rows(parcel.legs))
        return TableSpec("BEARING & DISTANCE", ["LINE", "BEARING", "DIST. (M)"], rows)

    def _coordinate_table(self):
        """The beacon register."""
        return TableSpec(
            "COORDINATES", ["STN", "NORTHING", "EASTING"],
            self._coordinate_rows(self.coordinates),
        )

    def draw_beacons(self):
        height = self.height("beacon_label", self.label_size)
        for coord in self.coordinates or []:
            self._drawer.draw_beacon(
                coord.easting, coord.northing, 0, height, coord.id,
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
        self.draw_tables()
        self.draw_north_arrow()
