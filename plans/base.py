"""Shared behaviour for all survey plan generators.

``BasePlan`` owns everything that is common to every plan type: computing
the drawing frame from the data bounding box, and drawing the frame, title
block, footer boxes, north arrow, and bearing/distance leg labels.
"""

import math
from typing import ClassVar, Optional

from ezdxf.enums import TextEntityAlignment

from dxf_manager import PAPER_SIZES, SurveyDXFManager
from models.plan import (
    CoordinateProps,
    PlanProps,
    PlanType,
    TraverseLegProps,
    origin_display_name,
)
from utils import format_number, html_to_mtext, line_normals, readable_angle

# Margins around the data bounding box, as a fraction of its larger side.
FRAME_X_PERCENT = 0.9
FRAME_Y_PERCENT = 1.5

# Fraction of the frame height reserved for footer boxes.
FOOTER_HEIGHT_PERCENT = 0.18

# Size of the north-arrow locator cross, as a multiple of the beacon symbol.
NORTH_CROSS_BEACON_RATIO = 5.0

# Square metres in one hectare. Parcels at or above this are also quoted in
# hectares in the title block.
SQUARE_METRES_PER_HECTARE = 10_000

# Decimal places used for the hectare figure (0.001 ha = 10 sq.metres). The
# app quotes hectares to the same precision.
HECTARE_DECIMALS = 3


class BasePlan(PlanProps):
    #: Concrete subclasses set this so payloads with the wrong ``type`` fail fast.
    expected_type: ClassVar[Optional[PlanType]] = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.expected_type is not None and self.type != self.expected_type:
            raise ValueError(f"{type(self).__name__} must have type '{self.expected_type.value}'")

        self._frame_x_percent = FRAME_X_PERCENT
        self._frame_y_percent = FRAME_Y_PERCENT
        self._bounding_box = self._compute_bounding_box()
        self._frame_coords = self._setup_frame_coords()
        if not self._frame_coords:
            raise ValueError("Cannot determine frame coordinates without valid coordinates.")
        self._drawer = self._setup_drawer()

    # ------------------------------------------------------------------
    # Setup hooks
    # ------------------------------------------------------------------
    def _compute_bounding_box(self):
        return self.get_bounding_box()

    def _setup_drawer(self) -> SurveyDXFManager:
        drawer = SurveyDXFManager(
            plan_name=self.name,
            scale=self.get_drawing_scale(),
            dxf_version=self.dxf_version,
        )
        drawer.setup_font(self.font)
        self._setup_layers(drawer)
        return drawer

    def _setup_layers(self, drawer: SurveyDXFManager):
        """Add the plan-type specific layers and block styles."""
        raise NotImplementedError

    def _setup_frame_coords(self):
        min_x, min_y, max_x, max_y = self._bounding_box
        if min_x is None or min_y is None or max_x is None or max_y is None:
            return None

        width = max_x - min_x
        height = max_y - min_y

        margin_x = max(width, height) * self._frame_x_percent
        margin_y = max(width, height) * self._frame_y_percent

        return self._fit_frame_to_page(
            (min_x - margin_x, min_y - margin_y, max_x + margin_x, max_y + margin_y)
        )

    def _fit_frame_to_page(self, frame):
        """Stretch the frame to the paper's aspect ratio so a landscape page
        gets a landscape frame (and portrait a portrait one) and the drawing
        fills the sheet when fitted to the page."""
        page_size = getattr(self.page_size, "value", self.page_size)
        orientation = getattr(self.page_orientation, "value", self.page_orientation)
        paper_w, paper_h = PAPER_SIZES.get(str(page_size).upper(), PAPER_SIZES["A4"])
        if str(orientation).lower() == "landscape":
            paper_w, paper_h = paper_h, paper_w
        # the PDF renderer applies 20 mm print margins on every side
        aspect = (paper_w - 40) / (paper_h - 40)

        left, bottom, right, top = frame
        width = right - left
        height = top - bottom

        if width / height < aspect:
            extra = height * aspect - width
            left -= extra / 2
            right += extra / 2
        else:
            extra = width / aspect - height
            bottom -= extra / 2
            top += extra / 2

        return left, bottom, right, top

    def _get_drawing_extent(self) -> float:
        """Diagonal of the data bounding box, used to size labels and offsets."""
        min_x, min_y, max_x, max_y = self._bounding_box
        if min_x is None or min_y is None or max_x is None or max_y is None:
            return 0.0
        return math.hypot(max_x - min_x, max_y - min_y)

    # ------------------------------------------------------------------
    # Title block hooks
    # ------------------------------------------------------------------
    def _area_text(self) -> str:
        """Area line of the title block; empty string hides it."""
        return ""

    def _format_area(self, area: Optional[float]) -> str:
        """Format an area for the title block.

        Square metres stay the primary figure -- that is what a surveyor
        verifies against -- and are printed exactly as supplied. Once the
        parcel reaches a full hectare the equivalent in hectares is appended
        for readability, matching how the app quotes areas.

        e.g. ``AREA :- 12500.0 SQ.METRES (1.250 HA)``
        """
        if area is None:
            return ""

        text = f"AREA :- {area} SQ.METRES"
        if area >= SQUARE_METRES_PER_HECTARE:
            hectares = area / SQUARE_METRES_PER_HECTARE
            text += f" ({hectares:.{HECTARE_DECIMALS}f} HA)"
        return text

    def _origin_text(self) -> str:
        """Origin line of the title block, e.g. ``ORIGIN :- UTM ZONE 31``."""
        return f"ORIGIN :- {origin_display_name(self.origin).upper()}"

    # ------------------------------------------------------------------
    # Shared drawing routines
    # ------------------------------------------------------------------
    def draw_frames(self):
        frame_left, frame_bottom, frame_right, frame_top = self._frame_coords
        self._drawer.draw_frame(frame_left, frame_bottom, frame_right, frame_top)

    def draw_title_block(self):
        frame_left, frame_bottom, frame_right, frame_top = self._frame_coords
        min_x, min_y, max_x, max_y = self._bounding_box

        margin_y = frame_top - max_y
        frame_width = frame_right - frame_left
        frame_height = frame_top - frame_bottom
        frame_center_x = frame_left + (frame_width / 2)
        title_y = frame_top - (margin_y * 0.2)

        self._drawer.draw_title_block(
            html_to_mtext(self.build_title(), font=self.font),
            frame_center_x,
            title_y,
            frame_width * 0.6,
            self.font_size,
            # size the scale bar from the smaller frame side so the title
            # stack does not grow into the drawing on landscape sheets
            graphical_scale_length=min(frame_width, frame_height) * 0.4,
            area=self._area_text(),
            origin=self._origin_text(),
        )

    def _effective_footers(self) -> list:
        """Footer texts to draw; a plan number forces at least two boxes so
        it always has a box to sit in (empty boxes are drawn without text)."""
        footers = list(self.footers or [])
        if self.plan_number and len(footers) < 2:
            footers += [""] * (2 - len(footers))
        return footers

    def draw_footer_boxes(self):
        footers = self._effective_footers()
        if not footers:
            return

        x_min, y_min, x_max, y_max = self._frame_coords
        box_width = (x_max - x_min) / len(footers)
        box_height = (y_max - y_min) * FOOTER_HEIGHT_PERCENT

        for i, footer in enumerate(footers):
            x1 = x_min + i * box_width
            top_inset = 0.0

            # Plan number sits at the top left of the rightmost footer box;
            # that box's own text starts below it.
            if self.plan_number and i == len(footers) - 1:
                plan_no_height = self.label_size * 1.3
                self._drawer.add_label(
                    f"PLAN No:- {self.plan_number.upper()}",
                    x1 + box_width * 0.05,
                    y_min + box_height * 0.9,
                    height=plan_no_height,
                    alignment=TextEntityAlignment.TOP_LEFT,
                )
                top_inset = plan_no_height * 1.8

            self._drawer.draw_footer_box(
                html_to_mtext(footer, font=self.font),
                x1, y_min, x1 + box_width, y_min + box_height, self.footer_size,
                top_inset=top_inset,
            )

    def add_leg_labels(self, leg: TraverseLegProps, orientation: str):
        """Label a traverse leg with its distance (inside) and bearing (outside)."""
        dx = leg.to.easting - leg.from_.easting
        dy = leg.to.northing - leg.from_.northing
        if dx == 0 and dy == 0:
            return

        angle_deg = math.degrees(math.atan2(dy, dx))

        # Both labels sit at the leg midpoint: distance inside the polygon,
        # bearing outside.
        mid_x = (leg.from_.easting + leg.to.easting) / 2
        mid_y = (leg.from_.northing + leg.to.northing) / 2

        # Offset the labels perpendicular to the leg: distance towards the
        # inside of the polygon, bearing towards the outside.
        inside, outside = line_normals(
            (leg.from_.easting, leg.from_.northing),
            (leg.to.easting, leg.to.northing),
            orientation,
        )
        offset_distance = self._get_drawing_extent() * 0.02
        length = math.hypot(*inside)
        inside = (inside[0] / length * offset_distance, inside[1] / length * offset_distance)
        outside = (outside[0] / length * offset_distance, outside[1] / length * offset_distance)

        bearing_x = mid_x + outside[0]
        bearing_y = mid_y + outside[1]
        mid_x += inside[0]
        mid_y += inside[1]

        # Keep text readable: left-to-right for horizontal-ish legs,
        # bottom-to-top for vertical-ish ones (readability bias).
        text_angle = readable_angle(angle_deg)

        if leg.distance is not None:
            self._drawer.add_label(f"{leg.distance:.2f}m", mid_x, mid_y,
                                   angle=text_angle, height=self.label_size)

        if leg.bearing is None:
            return

        # Degrees and minutes as a single MText entity (professional
        # convention), centered on the leg with the two parts spread apart
        # so the label spans a fixed fraction of the leg.
        degrees_label = f"{format_number(leg.bearing.degrees, 'hundredth')}°"
        minutes_label = f"{format_number(leg.bearing.minutes, 'tenth')}'"
        self._drawer.add_split_mtext_label(
            degrees_label, minutes_label, bearing_x, bearing_y,
            angle=text_angle, height=self.label_size,
            span=math.hypot(dx, dy) * 0.6,
        )

    # ------------------------------------------------------------------
    # North arrow
    # ------------------------------------------------------------------
    def _north_arrow_reference(self) -> Optional[CoordinateProps]:
        """Coordinate the north arrow and grid lines are anchored to."""
        return None

    def draw_north_arrow(self):
        coord = self._north_arrow_reference()
        if coord is None:
            return

        frame_left, frame_bottom, frame_right, frame_top = self._frame_coords
        height = (frame_top - frame_bottom) * 0.07
        self._drawer.draw_north_arrow(coord.easting, frame_top - height, height)

        # easting label along the top of the frame
        width = (frame_right - frame_left) * 0.1
        self._drawer.add_north_arrow_label(
            (frame_left, coord.northing), (frame_left + width, coord.northing),
            f"{coord.easting}mE", self.label_size,
        )
        self._drawer.add_north_arrow_label(
            (frame_right, coord.northing), (frame_right - width, coord.northing),
            "", self.label_size,
        )

        # northing label, raised above the footer boxes when present
        northing_label_y = frame_bottom
        if self._effective_footers():
            northing_label_y += (frame_top - frame_bottom) * FOOTER_HEIGHT_PERCENT

        self._drawer.add_north_arrow_label(
            (coord.easting, northing_label_y), (coord.easting, northing_label_y + height),
            f"{coord.northing}mN", self.label_size,
        )
        # The locator cross is not a beacon symbol; the multiplier keeps it at
        # its previous absolute size now that the beacon symbol is smaller.
        self._drawer.draw_north_arrow_cross(
            coord.easting, coord.northing, self.beacon_size * NORTH_CROSS_BEACON_RATIO
        )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def draw(self):
        raise NotImplementedError

    def save_dxf(self, file_path: str):
        self._drawer.save_dxf(file_path)

    def save(self) -> str:
        return self._drawer.save(paper_size=self.page_size, orientation=self.page_orientation)
