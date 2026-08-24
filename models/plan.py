"""Pydantic models describing survey plan payloads.

These models define the JSON contract between this service and its callers
(e.g. the TypeScript API server that handles users and persistence).
"""

from enum import Enum
from typing import Dict, List, Optional, Union
from datetime import datetime
from pydantic import BaseModel, Field, model_validator
from bs4 import BeautifulSoup

from dxf_manager import PAGE_MARGIN_MM, PAPER_SIZES


# ---------- Enums ----------
class PlanType(str, Enum):
    CADASTRAL = "cadastral"
    LAYOUT = "layout"
    TOPOGRAPHIC = "topographic"
    ROUTE = "route"


class PlanOrigin(str, Enum):
    UTM_ZONE_31 = "utm_zone_31"
    UTM_ZONE_32 = "utm_zone_32"
    UTM_ZONE_33 = "utm_zone_33"

    @property
    def display_name(self) -> str:
        """Human-readable label for this origin (e.g. ``UTM Zone 31``)."""
        return origin_display_name(self)


#: Explicit display labels for each origin. Kept as a map rather than derived
#: from the enum value so origins whose casing does not fall out of a simple
#: ``replace``/``title`` (acronyms, arbitrary datums) still read correctly.
PLAN_ORIGIN_DISPLAY_NAMES = {
    PlanOrigin.UTM_ZONE_31.value: "UTM Zone 31",
    PlanOrigin.UTM_ZONE_32.value: "UTM Zone 32",
    PlanOrigin.UTM_ZONE_33.value: "UTM Zone 33",
}


def origin_display_name(origin) -> str:
    """Return the human-readable label for ``origin``.

    Accepts a :class:`PlanOrigin` or a raw string. Origins without an entry in
    :data:`PLAN_ORIGIN_DISPLAY_NAMES` fall back to the raw value with
    underscores replaced by spaces, so a newly added origin is still readable
    before it gets an explicit label.
    """
    value = getattr(origin, "value", origin)
    if value is None:
        return ""
    value = str(value)
    return PLAN_ORIGIN_DISPLAY_NAMES.get(value, value.replace("_", " ").title())


#: Printed text heights in millimetres, per element class.
#:
#: These are *paper* sizes: the model-unit height is resolved at draw time as
#: ``mm * scale / 1000`` (see :meth:`PlanProps.text_height`), so selecting a
#: scale automatically yields a legible plan without re-editing text in CAD.
#:
#: The values come from the surveyor's reference ranges, quoted as model units
#: at 1:500 (1 m at 1:500 prints at 2 mm):
#:
#:   * bearing/distance 1.0-1.3 m  -> 2.0-2.6 mm  (2.0 mm here)
#:   * quoted coordinates 1.5-2 m  -> 3.0-4.0 mm  (3.0 mm here)
#:
#: Both sit at the bottom of their range rather than the top: the annotation
#: read heavy on a finished sheet. Reduced within the surveyor's own figures
#: rather than below them, so the sheet is still one they specified.
#:
#: The remaining classes are anchored to those two: title text is the largest
#: element on the sheet, the plan number reads at coordinate size, and the
#: dense annotation (spot heights, contour labels) sits at the small end where
#: testers already confirmed the printed size in the Task 6 review.
TEXT_HEIGHTS_MM = {
    # The title and the notes under the graphical scale are one size, by
    # request: the block reads as a single statement rather than a heading with
    # smaller print beneath it. They are equal here rather than equal by
    # coincidence of the Title Size control, so they stay equal when it moves.
    "title": 3.5,           # plan title, address, state, scale line
    "title_note": 3.5,      # area / origin / notes under the graphical scale
    "scale_bar": 1.8,       # graphical scale tick labels
    "bearing_distance": 2.0,  # leg distance and bearing labels
    "quoted_coordinate": 3.0,  # the mE / mN values quoted along the frame
    "beacon_label": 2.0,    # beacon / station identifiers
    "plan_number": 3.5,     # PLAN No:- in the footer band
    "surveyor_name": 2.5,   # footer box text (surveyor's name, credits)
    "spot_height": 1.5,     # topographic spot elevations
    "contour_label": 1.8,   # contour value labels
    "grid_label": 2.0,      # reference-grid coordinate labels
    # Smaller than the annotation on the map, deliberately. A schedule is read
    # up close, a leg label is read in the context of the drawing -- and the
    # schedule's own width is what decides how much sheet the drawing gets. At
    # 2.0 mm two schedules took 70 mm of a 170 mm sheet and forced a survey
    # from 1:1250 to 1:2500; at 1.6 mm the same schedules, with the same number
    # of rows, leave it at 1:2000 and the drawing a quarter larger.
    "table": 1.6,           # schedule / table cell text
    "general": 2.0,         # anything not otherwise classified
}

#: Which of the app's four size controls governs each text class.
#:
#: The controls are labelled Title Size, Label Size and Footer Size (plus
#: Beacon Size for the symbols), so each one moves its own group and nothing
#: else. Within a group the surveyor's designed ratios from
#: :data:`TEXT_HEIGHTS_MM` are preserved -- the control scales the group
#: together rather than flattening it to one height.
TEXT_GROUPS = {
    "title": "title",
    "title_note": "title",
    "scale_bar": "title",
    "bearing_distance": "annotation",
    "quoted_coordinate": "annotation",
    "beacon_label": "annotation",
    "spot_height": "annotation",
    "contour_label": "annotation",
    "grid_label": "annotation",
    "table": "annotation",
    "general": "annotation",
    "plan_number": "footer",
    "surveyor_name": "footer",
}

#: The element each control is read as the printed height of.
GROUP_REFERENCE = {
    "title": "title",
    "annotation": "general",
    "footer": "surveyor_name",
}

#: Below this, a ``*_size`` field is not a printed millimetre size.
#:
#: These fields carried ground metres before Task 8 -- ``label_size`` 1.0,
#: ``footer_size`` 0.5, ``beacon_size`` 0.18 -- and the API also wrote
#: extent-derived fractions into them. All of those are far under any usable
#: printed size, so a value this small is read as "not set" and the designed
#: default is used. Old plans keep rendering correctly without a migration.
LEGACY_SIZE_MM = 2.0

#: The same rule for the beacon symbol, which is legitimately smaller than any
#: text: 1.6 mm is its designed size, while the legacy ground-metre values sat
#: at 0.15-0.3.
LEGACY_BEACON_MM = 0.5

#: Ceiling for the beacon symbol, so a stale value cannot blot out the sheet.
BEACON_SYMBOL_MAX_MM = 8.0

#: ``font_size`` is read as the printed height of the plan title, in
#: millimetres. It defaults to the same height as the area and origin notes,
#: so the title block reads at one size rather than leading with a heading. Before this
#: the scale-driven table ignored ``font_size`` entirely, so the embellishment
#: control in the app did nothing -- 12 and 5.5 produced identical sheets.
#:
#: Clamped, because the field has carried very different meanings over time
#: (model units, an API-computed fraction of the drawing extent) and a stale
#: 0.2 or 100 would otherwise render a plan unreadable rather than merely
#: mis-sized.
FONT_SIZE_MIN_MM = 2.0
FONT_SIZE_MAX_MM = 14.0

#: Printed size of the beacon symbol in millimetres. Testers settled on this
#: in the Task 3 review ("the beacon symbols are too large"): a neat point
#: marker just above the small annotation sizes.
BEACON_SYMBOL_MM = 1.6

#: Printed size (arm length) of the topographic spot-height cross.
# Arm length of the spot-height cross, so the symbol spans twice this. Small:
# a spot height is a position, and at survey density a heavier mark turns the
# sheet into hatching.
TOPO_POINT_SYMBOL_MM = 0.5

#: Schedule tables drawn on the sheet (Task 10): row pitch as a multiple of
#: the cell text height, a generous per-character width estimate so text never
#: spills its cell, and the printed gap between the drawing, the table band,
#: and adjacent table columns.
TABLE_ROW_SPACING = 2.2
#: Cell padding either side of the text, as a multiple of the text height.
#: Matches the padding ``SurveyDXFManager.draw_table`` insets its text by.
TABLE_CELL_PADDING = 0.4
TABLE_GAP_MM = 5.0

#: Decimal places for coordinates and distances in the on-sheet tables. No
#: thousands separator anywhere -- surveyors do not write coordinates that way
#: and a comma breaks a copy/paste into CAD (see Task 4).
TABLE_COORDINATE_DECIMALS = 3
TABLE_DISTANCE_DECIMALS = 2

#: Minimum printed spacing between spot-height *labels*, in millimetres.
#:
#: An elevation reads as about five characters at 1.5 mm, so it occupies
#: roughly 5 mm of width; below that spacing the values run into each other.
#: The previous 9 mm was nearly twice what its own reasoning called for and
#: showed a fraction of the survey it could have.
SPOT_HEIGHT_SPACING_MM = 5.0

#: Minimum printed spacing between spot-height *markers*, in millimetres.
#:
#: Markers and labels used to be thinned together, so the width of a number
#: decided how many survey shots appeared on the sheet -- a 25,000-point
#: survey drew 60 crosses and looked as though the data had been thrown away.
#: The cross is 1 mm across, so it can sit far closer than the text beside it:
#: every point that will not collide is drawn, and only the elevations are
#: thinned to stay readable.
TOPO_POINT_SPACING_MM = 2.5

#: Interpolation grid cell, in printed millimetres. The grid only has to
#: resolve what the paper can show; a fixed cell count wastes work on a small
#: site and under-samples a large one.
CONTOUR_GRID_CELL_MM = 0.75
CONTOUR_GRID_MIN = 40
CONTOUR_GRID_MAX = 600

#: Standard plotting scales, used to suggest a workable scale when the survey
#: does not fit the chosen sheet.
STANDARD_SCALES = (100, 200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 20000, 50000)


class BeaconType(str, Enum):
    DOT = "dot"
    CIRCLE = "circle"
    BOX = "box"
    NONE = "none"


class PageSize(str, Enum):
    A4 = "A4"
    A3 = "A3"
    A2 = "A2"
    A1 = "A1"
    A0 = "A0"


class PageOrientation(str, Enum):
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class LayoutMode(str, Enum):
    """Which layout design a layout plan draws: the auto-generated
    subdivision (from ``layout_parameters``) or the manually entered one
    (``coordinates``/``plots``/``roads``). Both datasets may coexist in the
    payload; this field only selects which one is used."""
    AUTO = "auto"
    MANUAL = "manual"


# ---------- Supporting models ----------
class CoordinateProps(BaseModel):
    id: str = ""
    northing: Optional[float] = 0.0
    easting: Optional[float] = 0.0
    elevation: Optional[float] = 0.0


class BearingProps(BaseModel):
    degrees: Optional[int] = 0
    minutes: Optional[int] = 0
    seconds: Optional[float] = 0.0
    decimal: Optional[float] = 0.0


class TraverseLegProps(BaseModel):
    from_: CoordinateProps = Field(alias="from")
    to: CoordinateProps
    bearing: Optional[BearingProps] = None
    observed_angle: Optional[BearingProps] = None
    distance: Optional[float] = None


class ParcelProps(BaseModel):
    name: str
    ids: List[str]
    area: Optional[float] = None  # in square meters
    legs: List[TraverseLegProps] = []


class ElevationProps(BaseModel):
    id: Optional[str] = None
    elevation: float
    chainage: str


class TopographicSettingProps(BaseModel):
    show_spot_heights: bool = True
    point_label_scale: float = 1.0
    show_contours: bool = True
    contour_interval: float = 1.0
    major_contour: float = 5.0
    minimum_distance: float = 0.1  # 0.1 to 0.5
    show_contours_labels: bool = True
    contour_label_scale: float = 1.0
    show_boundary: bool = True
    boundary_label_scale: float = 1.0
    tin: Optional[bool] = False  # generate contours from a TIN (Delaunay)
    grid: Optional[bool] = False  # generate contours from a regular grid
    show_mesh: Optional[bool] = False  # legacy single mesh toggle (deprecated)
    show_tin_mesh: Optional[bool] = False  # draw the TIN triangulation overlay
    show_grid: Optional[bool] = False  # draw the coordinate reference grid

    @model_validator(mode="after")
    def _validate_contour_settings(self):
        """The contour interval divides the elevation range into levels, so a
        zero or negative value is meaningless (and would break level
        generation). Enforce it whenever contours are generated or shown."""
        if self.show_contours or self.tin or self.grid:
            if self.contour_interval <= 0:
                raise ValueError("contour_interval must be greater than 0")
            if self.major_contour <= 0:
                raise ValueError("major_contour must be greater than 0")
        return self


class TopographicBoundaryProps(BaseModel):
    coordinates: List[CoordinateProps] = []
    area: Optional[float] = None
    legs: Optional[List[TraverseLegProps]] = []


class LayoutBoundaryProps(BaseModel):
    coordinates: List[CoordinateProps] = []
    area: Optional[float] = None
    legs: Optional[List[TraverseLegProps]] = []


class LayoutPlotProps(BaseModel):
    """A single plot in a layout: corner beacon ids referencing the plan's
    coordinate register, in polygon order."""
    block: str = ""
    number: Union[int, str] = ""
    ids: List[str] = []
    area: Optional[float] = None
    use: str = "residential"  # residential | commercial | open_space | <facility>

    def label(self) -> str:
        if self.block:
            return f"Block {self.block} Plot {self.number}"
        return f"Plot {self.number}"


class LayoutRoadProps(BaseModel):
    """A road defined by centerline beacon ids in the coordinate register."""
    name: str = ""
    width: float = 9.0
    centerline_ids: List[str] = []


class LayoutPlotParams(BaseModel):
    """Standard plot module, quoted as frontage x depth (15 x 30 = 450 sqm)."""
    frontage: float = 15.0  # meters along the road
    depth: float = 30.0  # meters
    min_area: float = 400.0  # drop edge remainders smaller than this
    remainder_strategy: str = "add_to_last"  # add_to_last | separate | distribute


class LayoutRoadParams(BaseModel):
    major_width: float = 15.0  # spine road right-of-way
    collector_width: float = 12.0
    access_width: float = 9.0
    corner_radius: float = 6.0
    major_road_name: str = ""


class LayoutBlockParams(BaseModel):
    double_loaded: bool = True  # two plot rows back-to-back per block
    max_length: float = 180.0  # block length before a cross street
    orientation: str = "auto"  # auto | ns | ew


class LayoutReserveParams(BaseModel):
    open_space_percent: float = 10.0
    commercial_along_major: bool = True
    facilities: List[str] = []  # e.g. ["school", "market"]


class LayoutNumberingParams(BaseModel):
    scheme: str = "block_plot"  # Block A Plot 1 ...
    block_labels: str = "alphabetic"
    plot_start: int = 1


class LayoutParameters(BaseModel):
    """Design parameters for auto-generating a subdivision layout."""
    plot: LayoutPlotParams = Field(default_factory=LayoutPlotParams)
    roads: LayoutRoadParams = Field(default_factory=LayoutRoadParams)
    blocks: LayoutBlockParams = Field(default_factory=LayoutBlockParams)
    reserves: LayoutReserveParams = Field(default_factory=LayoutReserveParams)
    numbering: LayoutNumberingParams = Field(default_factory=LayoutNumberingParams)


class LongitudinalProfileParameters(BaseModel):
    horizontal_scale: float = 1.0  # drawing units per metre of chainage
    vertical_scale: float = 1.0  # drawing units per metre of elevation
    station_interval: float = 10.0  # metres
    elevation_interval: float = 1.0


class RouteParameters(BaseModel):
    """Plan-view (horizontal alignment) settings for route surveys.

    The plan view is drawn when the payload carries station coordinates
    (``coordinates`` entries whose ids match the ``elevations`` ids).
    """
    right_of_way_width: float = 30.0  # metres, total corridor width
    show_plan_view: bool = True
    show_chainage_labels: bool = True


# ---------- Main Plan Model ----------
class PlanProps(BaseModel):
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    user: Union[str, dict]
    project: Union[str, dict]
    name: str
    type: PlanType = PlanType.CADASTRAL
    font: str = "Times New Roman"
    #: Printed height of the plan title in millimetres, and the master control
    #: for every other text element (see :attr:`text_scale`). Defaults to the
    #: designed title size, so an unset plan gets exactly the scale-driven
    #: defaults rather than a doubled sheet.
    font_size: float = 3.5
    coordinates: Optional[List[CoordinateProps]] = None
    elevations: Optional[List[ElevationProps]] = None
    parcels: Optional[List[ParcelProps]] = None
    title: str = "Untitled Plan"
    address: str = ""
    local_govt: str = ""
    state: str = ""
    plan_number: str = ""
    origin: PlanOrigin = PlanOrigin.UTM_ZONE_31
    scale: float = 1000
    beacon_type: BeaconType = BeaconType.BOX
    #: Beacon symbol width in metres. Legacy field: used only when
    #: ``auto_scale_sizes`` is off. With auto sizing (the default) the symbol
    #: is resolved from :data:`BEACON_SYMBOL_MM` and the plotting scale.
    beacon_size: float = 0.18
    #: General label height in metres. Legacy field, see ``beacon_size``.
    label_size: float = 1.0
    personel_name: str = ""
    surveyor_name: str = ""
    page_size: PageSize = PageSize.A4
    page_orientation: PageOrientation = PageOrientation.PORTRAIT
    topographic_setting: TopographicSettingProps = Field(default_factory=TopographicSettingProps)
    topographic_boundary: Optional[TopographicBoundaryProps] = None
    layout_boundary: Optional[LayoutBoundaryProps] = None
    layout_parameters: LayoutParameters = Field(default_factory=LayoutParameters)
    # None keeps the legacy behaviour: manual data wins when present.
    layout_mode: Optional[LayoutMode] = None
    plots: Optional[List[LayoutPlotProps]] = None
    roads: Optional[List[LayoutRoadProps]] = None
    longitudinal_profile_parameters: Optional[LongitudinalProfileParameters] = None
    route_parameters: RouteParameters = Field(default_factory=RouteParameters)
    footers: List[str] = []
    footer_size: float = 0.5
    dxf_version: str = "R2000"
    #: How many points each series held before thinning, set by the streaming
    #: reader when a large survey is sent as NDJSON. Used only to tell the
    #: reader of the sheet what fraction of the survey it is looking at.
    point_totals: Dict[str, int] = Field(default_factory=dict)
    #: Draw a bearing/distance schedule on the sheet, so the plan is
    #: self-contained for submission (Task 10). Cadastral plans list the
    #: parcel legs; topographic and layout plans list their boundary legs.
    show_bearing_distance_table: bool = False
    #: Draw a coordinate schedule on the sheet. Cadastral plans list the
    #: beacon register; topographic and layout plans list their boundary
    #: coordinates, and layout additionally lists the plot-corner register it
    #: exports for setting out.
    show_coordinate_table: bool = False
    #: Resolve text and symbol sizes from the plotting scale (Task 8). When
    #: this is on -- the default -- choosing a scale automatically produces
    #: legible, plot-ready text and the legacy ``*_size`` fields are ignored
    #: for map plans. Set it to ``False`` to drive every size manually from
    #: ``font_size`` / ``label_size`` / ``footer_size`` / ``beacon_size``.
    auto_scale_sizes: bool = True
    #: When the survey does not fit the chosen sheet at the requested scale,
    #: fall back to the next standard scale that does (never a larger one --
    #: the plan is only ever zoomed out, never in) and print that scale in the
    #: title block. With this off the plan raises instead, which is the right
    #: behaviour when a submission mandates an exact scale.
    fit_scale_to_sheet: bool = True
    #: Per-element printed height overrides, in millimetres, keyed by the
    #: element classes of :data:`TEXT_HEIGHTS_MM` (e.g.
    #: ``{"bearing_distance": 3.0}``). Overrides win over the table while
    #: leaving every other element on the scale-driven default.
    text_heights: Dict[str, float] = Field(default_factory=dict)

    @property
    def mm_to_model(self) -> float:
        """Model units (metres) that print as one millimetre at this scale.

        Geometry is drawn at true ground coordinates, so a plan plotted at
        1:500 renders 1 m as 2 mm and one printed millimetre is 0.5 m of
        model space.
        """
        scale = self.scale or 1000
        return scale / 1000.0

    @property
    def plot_scale_mm_per_unit(self) -> float:
        """Printed millimetres per model unit -- what the PDF renderer needs
        to plot the sheet at the declared scale."""
        scale = self.scale or 1000
        return 1000.0 / scale

    @property
    def text_scale(self) -> float:
        """Title-group multiplier, from ``font_size``.

        Kept as a name for the title group's scale. It used to multiply every
        text element on the sheet, which meant nudging the title quietly
        resized the bearings and quoted coordinates whose heights the surveyor
        had specified -- see :func:`group_scale`.
        """
        return self.group_scale("title")

    def size_control(self, group: str) -> Optional[float]:
        """The app control that governs a text group."""
        return {
            "title": self.font_size,
            "annotation": self.label_size,
            "footer": self.footer_size,
        }.get(group)

    def group_scale(self, group: str) -> float:
        """Multiplier for one text group, from the control that owns it.

        Each control is read as the printed height it asks for, divided by the
        designed height of its group's reference element -- so Title Size
        moves the title block, Label Size moves the map annotation, and
        Footer Size moves the footer, none of them touching the others.
        """
        if not self.auto_scale_sizes:
            return 1.0

        requested = self.size_control(group)
        if requested is None or float(requested) < LEGACY_SIZE_MM:
            # Unset, or a legacy ground-metre value: use the designed sizes.
            return 1.0

        clamped = min(float(requested), FONT_SIZE_MAX_MM)
        reference = GROUP_REFERENCE.get(group, "general")
        return clamped / TEXT_HEIGHTS_MM[reference]

    def text_height(self, element: str = "general") -> float:
        """Model-unit height for a text element class.

        Resolves the class's printed height in millimetres -- an entry in
        ``text_heights`` if the caller supplied one, otherwise the
        :data:`TEXT_HEIGHTS_MM` default -- and converts it to model units at
        the plan's scale.
        """
        override = self.text_heights.get(element)
        if override is not None and override > 0:
            # An explicit per-element override is an absolute printed size and
            # is not scaled again by font_size.
            return override * self.mm_to_model

        mm = TEXT_HEIGHTS_MM.get(element, TEXT_HEIGHTS_MM["general"])
        group = TEXT_GROUPS.get(element, "annotation")
        return mm * self.group_scale(group) * self.mm_to_model

    def printable_area(self) -> tuple:
        """Printable sheet size in millimetres (paper less the print margins),
        honouring the page orientation."""
        page_size = getattr(self.page_size, "value", self.page_size)
        orientation = getattr(self.page_orientation, "value", self.page_orientation)
        paper_w, paper_h = PAPER_SIZES.get(str(page_size).upper(), PAPER_SIZES["A4"])
        if str(orientation).lower() == "landscape":
            paper_w, paper_h = paper_h, paper_w
        return paper_w - 2 * PAGE_MARGIN_MM, paper_h - 2 * PAGE_MARGIN_MM

    def get_bounding_box(self) -> tuple:
        """Bounding box (min_x, min_y, max_x, max_y) of all plan coordinates.

        Returns a tuple of ``None`` values when the plan has no coordinates.
        """
        xs, ys = [], []

        if self.coordinates:
            xs = [p.easting for p in self.coordinates]
            ys = [p.northing for p in self.coordinates]

        if self.type == PlanType.TOPOGRAPHIC and self.topographic_boundary is not None:
            xs += [p.easting for p in self.topographic_boundary.coordinates]
            ys += [p.northing for p in self.topographic_boundary.coordinates]

        if self.type == PlanType.LAYOUT and self.layout_boundary is not None:
            xs += [p.easting for p in self.layout_boundary.coordinates]
            ys += [p.northing for p in self.layout_boundary.coordinates]

        if not xs or not ys:
            return None, None, None, None

        return min(xs), min(ys), max(xs), max(ys)

    def get_route_plan_bounding_box(self) -> Optional[tuple]:
        """Bounding box of the longitudinal profile, in drawing coordinates."""
        if self.type != PlanType.ROUTE or not self.elevations or self.longitudinal_profile_parameters is None:
            return None

        params = self.longitudinal_profile_parameters
        min_elev = min(e.elevation for e in self.elevations)
        max_elev = max(e.elevation for e in self.elevations)
        chainage_length = params.station_interval * (len(self.elevations) - 1)

        # The profile is anchored at the drawing origin; the frame fits to
        # content, so the absolute position carries no meaning.
        min_x = 0.0
        min_y = 0.0
        max_x = min_x + chainage_length * params.horizontal_scale
        max_y = min_y + (max_elev - min_elev) * params.vertical_scale

        return min_x, min_y, max_x, max_y

    def build_title(self) -> str:
        """Compose the plan title block as an HTML fragment."""
        soup = BeautifulSoup(self.title.upper(), "html.parser")

        for line in (
            self.address.upper() if self.address else None,
            self.local_govt.upper() if self.local_govt else None,
            f"{self.state.upper()} STATE" if self.state else None,
            f"SCALE :- 1 : {int(self.scale)}" if self.scale else None,
        ):
            if line:
                p = soup.new_tag("p")
                # Marked as subordinate so the drawing renders them below the
                # title's own size; they are context, not the heading.
                small = soup.new_tag("small")
                small.string = line
                p.append(small)
                soup.append(p)

        return str(soup)
