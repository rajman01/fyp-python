"""Shared behaviour for all survey plan generators.

``BasePlan`` owns everything that is common to every plan type: computing
the drawing frame from the data bounding box, and drawing the frame, title
block, footer boxes, north arrow, and bearing/distance leg labels.
"""

import logging
import math
from typing import ClassVar, List, NamedTuple, Optional, Tuple

from ezdxf.enums import TextEntityAlignment

from dxf_manager import PAPER_SIZES, SurveyDXFManager
from models.plan import (
    BEACON_SYMBOL_MAX_MM,
    TEXT_HEIGHTS_MM,
    BEACON_SYMBOL_MM,
    FONT_SIZE_MIN_MM,
    LEGACY_BEACON_MM,
    STANDARD_SCALES,
    TABLE_CELL_PADDING,
    TABLE_COORDINATE_DECIMALS,
    TABLE_DISTANCE_DECIMALS,
    TABLE_GAP_MM,
    TABLE_ROW_SPACING,
    CoordinateProps,
    PlanProps,
    PlanType,
    TraverseLegProps,
    origin_display_name,
)
from utils import format_number, html_to_mtext, line_normals, readable_angle

logger = logging.getLogger(__name__)

# Most of the sheet width the schedule band may ever take.
TABLE_BAND_LIMIT = 0.4

# MText hard line break.
MTEXT_NEW_LINE = "\\P"

# Margins around the data bounding box, as a fraction of its larger side.
# Only used by plans that fit the sheet to their content (route profiles);
# true-scale map plans take their frame from the paper size instead.
FRAME_X_PERCENT = 0.9
FRAME_Y_PERCENT = 1.5

# Fraction of the frame height reserved for footer boxes.
FOOTER_HEIGHT_PERCENT = 0.18

# Clearance kept around the survey, in printed millimetres.
#
# The drawing area used to be sized from the coordinate bounding box alone,
# but nothing a plan draws stops there: beacon ids sit beside their symbols,
# leg labels sit outside the boundary, and the quoted-coordinate ticks run to
# the frame. Fitting the bare extent pushed all of that hard against the frame
# and into the footer boxes.
ANNOTATION_MARGIN_MM = 6.0

# Gap between the frame's top edge and the first line of the title, and the
# clearance left between the bottom of the title stack and the drawing, both
# as printed millimetres.
TITLE_TOP_GAP_MM = 6.0
TITLE_CLEARANCE_MM = 4.0

# Ceiling on the measured title band, as a fraction of the frame height, so a
# pathologically long title cannot squeeze the drawing off its own sheet.
TITLE_HEIGHT_LIMIT = 0.45

# Width of the title block, as a fraction of the frame. A narrow column wraps
# a long title into many lines, and every wrapped line is height taken from the
# drawing -- at 0.6 a two-sentence title could take 40% of the sheet.
TITLE_WIDTH_PERCENT = 0.85

# Size of the north-arrow locator cross, as a multiple of the beacon symbol.
NORTH_CROSS_BEACON_RATIO = 5.0
# North arrow height, as a fraction of the frame height.
NORTH_ARROW_HEIGHT_PERCENT = 0.07
# How far the arrow head and its U/N labels reach either side of the easting
# line the arrow stands on, as a fraction of the arrow's height.
NORTH_ARROW_HALF_WIDTH_RATIO = 0.4
# Default length of an origin grid tick, as a fraction of the frame width.
GRID_TICK_PERCENT = 0.1
# Room a grid tick keeps beyond its value, as a printed size: enough for the
# inset that holds the value off the frame border, plus a tail past the text.
# Printed rather than proportional so the space a value has to fit into does
# not shrink just because a larger one was asked for.
GRID_LABEL_PAD_MM = 3.0

# Square metres in one hectare. Parcels at or above this are also quoted in
# hectares in the title block.
SQUARE_METRES_PER_HECTARE = 10_000

# Decimal places used for the hectare figure (0.001 ha = 10 sq.metres). The
# app quotes hectares to the same precision.
HECTARE_DECIMALS = 3


class TableSpec(NamedTuple):
    """A schedule to be drawn on the sheet.

    ``header`` is the column-title row and is repeated at the top of every
    column the table flows into; ``rows`` are the data rows.
    """
    title: str
    header: List[str]
    rows: List[List[str]]


class TableColumn(NamedTuple):
    """One measured column of the band: its schedule blocks and cell widths."""
    blocks: List[List[List[str]]]
    col_widths: List[float]
    width: float


class ColumnRows(NamedTuple):
    """The schedule blocks destined for one column of the band.

    Each block is drawn as its own bordered table, so two schedules stacked in
    the same column read as two schedules rather than one table with an odd
    empty row. Row 0 of every block is its title and spans the full width.
    """
    blocks: List[List[List[str]]]


class BasePlan(PlanProps):
    #: Concrete subclasses set this so payloads with the wrong ``type`` fail fast.
    expected_type: ClassVar[Optional[PlanType]] = None

    #: Whether the sheet is plotted at the plan's declared scale. Map plans
    #: (cadastral, topographic, layout) are drawn in true ground coordinates
    #: and plotted at 1:``scale``. Route sheets are not maps -- the
    #: longitudinal profile has independent horizontal and vertical scales --
    #: so they keep the content-fitted sheet.
    true_scale: ClassVar[bool] = True

    #: Whether the sheet carries the origin north arrow, whose tip sits on the
    #: frame's top edge. Sheets that do reserve room for it above the title.
    draws_north_arrow: ClassVar[bool] = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.expected_type is not None and self.type != self.expected_type:
            raise ValueError(f"{type(self).__name__} must have type '{self.expected_type.value}'")

        self._frame_x_percent = FRAME_X_PERCENT
        self._frame_y_percent = FRAME_Y_PERCENT
        # Set when ``fit_scale_to_sheet`` had to zoom the plan out; read it
        # via ``scale_adjusted_from`` to surface "drawn at 1:2500, not the
        # 1:1000 requested". Underscored so pydantic treats it as private
        # state rather than a payload field.
        self._scale_adjusted_from = None
        self._bounding_box = self._compute_bounding_box()
        # The drawer is built before the frame: sizing the sheet needs to
        # measure the wrapped title text, which needs a document to measure in.
        self._drawer = self._setup_drawer()
        self._fit_text_to_title_band()
        if self.true_scale and self._resolve_scale_for_sheet():
            # Zoomed out to fit: rebuild so the drawer's printed-size
            # conversions use the scale the sheet is actually drawn at.
            self._drawer = self._setup_drawer()
        self._frame_coords = self._setup_frame_coords()
        if not self._frame_coords:
            raise ValueError("Cannot determine frame coordinates without valid coordinates.")

    # ------------------------------------------------------------------
    # Setup hooks
    # ------------------------------------------------------------------
    def _compute_bounding_box(self):
        return self.get_bounding_box()

    def _setup_drawer(self) -> SurveyDXFManager:
        drawer = SurveyDXFManager(
            plan_name=self.name,
            mm_to_model=self.mm_to_model,
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

        if not self.true_scale:
            width = max_x - min_x
            height = max_y - min_y

            margin_x = max(width, height) * self._frame_x_percent
            margin_y = max(width, height) * self._frame_y_percent

            return self._fit_frame_to_page(
                (min_x - margin_x, min_y - margin_y, max_x + margin_x, max_y + margin_y)
            )

        return self._sheet_frame_coords()

    def group_scale(self, group: str) -> float:
        """Group multiplier actually used, after any shrink-to-fit.

        Only the title group shrinks: the band being fitted is the title
        block, and pulling the map annotation down with it would undo the
        legible sizes the surveyor specified for a reason that has nothing to
        do with them.
        """
        scale = PlanProps.group_scale(self, group)
        if group == "title":
            scale *= getattr(self, "_text_scale_fit", 1.0)
        return scale

    def _fit_text_to_title_band(self) -> None:
        """Shrink the text until the title block fits the room it is allowed.

        The band is capped so a long title cannot squeeze the drawing off its
        own sheet -- but capping the *reservation* alone just means the title
        overflows the cap and prints on top of the drawing. Reducing the text
        instead keeps the sheet intact and gives the user the largest size that
        actually fits, which is what asking for an oversized font should do.
        """
        self._text_scale_fit = 1.0
        if not (self.true_scale and self.auto_scale_sizes):
            return

        printable_w_mm, printable_h_mm = self.printable_area()
        frame_w = printable_w_mm * self.mm_to_model
        frame_h = printable_h_mm * self.mm_to_model
        allowed = frame_h * TITLE_HEIGHT_LIMIT

        for _ in range(12):
            if self._title_band_height(frame_w, frame_h, clamp=False) <= allowed:
                break
            self._text_scale_fit *= 0.9

        if self._text_scale_fit < 1.0:
            logger.info(
                "%s: title text reduced to %.0f%% so the title block fits the sheet",
                self.name, self._text_scale_fit * 100,
            )

    def _resolve_scale_for_sheet(self) -> bool:
        """Zoom the plan out to the next standard scale that fits the sheet.

        Runs before anything reads the scale, so the frame, the text heights,
        the plotted PDF and the ``SCALE :- 1 : n`` line in the title block all
        agree on one number. Without this a survey too large for its sheet
        used to be silently squeezed onto the paper at whatever scale happened
        to fit while the title block still claimed the requested one.

        Returns ``True`` when the scale was changed.
        """
        min_x, min_y, max_x, max_y = self._bounding_box
        if min_x is None or max_x is None:
            return False

        data_w, data_h = max_x - min_x, max_y - min_y

        # Worked entirely in printed millimetres. The label margin is a fixed
        # printed size, so it comes off the usable sheet -- adding it to the
        # survey instead measured it at the scale being replaced, which left
        # the resolver and the fit check disagreeing about whether a plan fit.
        margin_mm = self._annotation_margin_mm()
        printable_w_mm, printable_h_mm = self.printable_area()
        usable_w_mm = max(printable_w_mm - self._table_band_mm() - 2 * margin_mm, 1.0)
        usable_h_mm = max(self._usable_height_mm() - 2 * margin_mm, 1.0)

        needed = max(data_w / usable_w_mm, data_h / usable_h_mm) * 1000
        if needed <= (self.scale or 1000):
            return False

        fitted = None
        if self.fit_scale_to_sheet:
            fitted = next((s for s in STANDARD_SCALES if s >= needed), None)

        if fitted is None:
            frame_w = printable_w_mm * self.mm_to_model
            frame_h = printable_h_mm * self.mm_to_model
            self._check_fits_sheet(data_w, data_h, frame_w, frame_h)
            return False

        self._scale_adjusted_from = self.scale
        logger.warning(
            "%s: survey is %.1f m x %.1f m and does not fit %s %s at 1:%d; "
            "drawing at 1:%d instead",
            self.name, data_w, data_h,
            getattr(self.page_size, "value", self.page_size),
            getattr(self.page_orientation, "value", self.page_orientation),
            int(self.scale), int(fitted),
        )
        self.scale = fitted
        return True

    def _sheet_frame_coords(self):
        """Frame for a plan plotted at its declared scale.

        The frame *is* the sheet: the printable area of the chosen paper,
        converted to model units at the plan scale. A 1:1000 plan on A4
        portrait therefore frames 170 m x 257 m of ground, whatever the parcel
        measures, which is what makes the printed sheet a true 1:1000 plot.

        The survey is centred horizontally, and vertically within the band
        left between the title stack and the footer boxes so it cannot run
        into either.
        """
        min_x, min_y, max_x, max_y = self._bounding_box

        printable_w_mm, printable_h_mm = self.printable_area()
        frame_w = printable_w_mm * self.mm_to_model
        frame_h = printable_h_mm * self.mm_to_model

        # Everything below works from the survey *plus* the room its labels
        # need, so the drawing is never fitted flush against the frame.
        margin = self.annotation_margin()
        data_w = (max_x - min_x) + 2 * margin
        data_height = (max_y - min_y) + 2 * margin

        self._check_fits_sheet(data_w, data_height, frame_w, frame_h)

        # Schedules take a band down the right of the sheet; the drawing is
        # centred in what is left, so a table can never sit on the drawing.
        draw_w = frame_w - self._table_band_mm() * self.mm_to_model
        # Centre the *ink*, not the coordinates. Beacon ids are drawn up and
        # to the right of their symbols, so the drawing reaches further that
        # way than its bounding box does; centring the bare box therefore left
        # a wide margin on the left and a thin one on the right, which reads
        # as the frame crowding the plan even when the sheet has room.
        reach_x, reach_y = self._label_reach()
        center_x = (min_x + max_x + reach_x) / 2
        left = center_x - draw_w / 2

        # Vertical: centre the data in the usable band, then derive the frame.
        band_bottom_gap = (FOOTER_HEIGHT_PERCENT * frame_h
                           + self._bottom_band_mm() * self.mm_to_model)
        band_top_gap = self._title_band_height(frame_w, frame_h)
        band_height = frame_h - band_bottom_gap - band_top_gap
        centre_y = (min_y + max_y + reach_y) / 2
        bottom = centre_y - band_bottom_gap - band_height / 2

        return left, bottom, left + frame_w, bottom + frame_h

    def _labelled_ids(self) -> List[str]:
        """Point ids that are drawn as labels beside their symbols."""
        return [str(c.id) for c in (self.coordinates or []) if c.id not in (None, "")]

    def _label_reach(self) -> Tuple[float, float]:
        """How much further the drawing's ink runs than its coordinates, in
        model units, as (right, up).

        Beacon labels are set up and to the right of the point they belong to,
        so this is one-sided: the left and bottom edges of the ink sit on the
        coordinates themselves.
        """
        ids = self._labelled_ids()
        if not ids:
            return 0.0, 0.0

        scale = self.group_scale("annotation")
        height_mm = TEXT_HEIGHTS_MM["beacon_label"] * scale
        width_mm = self._drawer.text_width(max(ids, key=len), height_mm)
        return (width_mm * self.mm_to_model, height_mm * self.mm_to_model)

    def _annotation_margin_mm(self) -> float:
        """Clearance the drawing needs beyond the survey extent, in printed mm.

        The survey extent is where the *points* stop, not where the drawing
        stops. Every beacon carries its id beside it, so a plan whose stations
        are named "SBD 1204" reaches some 17 mm further than its own bounding
        box -- and fitting the bare extent left those labels a millimetre off
        the frame. Measuring the longest id means the sheet is chosen to hold
        the drawing *and* its annotation.
        """
        scale = self.group_scale("annotation")
        margin = ANNOTATION_MARGIN_MM * scale

        ids = self._labelled_ids()
        if not ids:
            return margin

        # text_width is proportional, so a height in millimetres gives a width
        # in millimetres. Only the longest id is measured -- a survey can hold
        # a million points and they are all set in the same style.
        height_mm = TEXT_HEIGHTS_MM["beacon_label"] * scale
        return margin + self._drawer.text_width(max(ids, key=len), height_mm)

    def annotation_margin(self) -> float:
        """Room the drawing's labels need beyond the survey extent, in model
        units. Scales with the text, so a larger font also gets more room."""
        return self._annotation_margin_mm() * self.mm_to_model

    def _title_top_gap(self, frame_h: float) -> float:
        """Gap between the frame's top edge and the first line of the title.

        The north arrow stands on the origin easting line with its tip on the
        frame's top edge, and that line falls wherever the anchor beacon
        happens to be -- often behind the centred title. Reserving the arrow's
        height here starts the title stack below it rather than through it.

        This is deliberately independent of the payload: the band is measured
        while the plan is still being constructed, before the coordinates are
        indexed, so it cannot ask where the arrow will actually land.
        """
        gap = TITLE_TOP_GAP_MM * self.mm_to_model
        if not self.draws_north_arrow:
            return gap
        arrow_h = frame_h * NORTH_ARROW_HEIGHT_PERCENT
        return max(gap, arrow_h + TITLE_CLEARANCE_MM * self.mm_to_model)

    def _title_band_height(self, frame_w: float, frame_h: float,
                           clamp: bool = True) -> float:
        """Height the title stack actually needs, in model units.

        Measures the wrapped title, the graphical scale bar and the
        area/origin/notes block rather than reserving a fixed fraction: a
        six-line title on a small sheet needs more than twice the room of a
        one-line title, and guessing one number for both used to drop the
        notes on top of the drawing.
        """
        title_h = self._drawer.measure_mtext(
            html_to_mtext(self.build_title(), font=self.font),
            self.height("title", self.font_size),
            frame_w * TITLE_WIDTH_PERCENT,
        )[1]

        bar_length = self._graphical_scale_length(frame_w, frame_h)
        bar_height = bar_length * 0.05
        scale_text = self.height("scale_bar", self.font_size * 0.5)
        bar_stack = bar_height * 1.5 + scale_text * 1.6 + bar_height

        notes = [t for t in [self._area_text(), self._origin_text(),
                             *self._title_block_notes()] if t]
        notes_h = 0.0
        if notes:
            notes_h = self._drawer.measure_mtext(
                MTEXT_NEW_LINE.join(notes),
                self.height("title_note", self.font_size),
                frame_w * TITLE_WIDTH_PERCENT,
            )[1]

        total = (self._title_top_gap(frame_h) + title_h + bar_stack
                 + notes_h + TITLE_CLEARANCE_MM * self.mm_to_model)
        return min(total, frame_h * TITLE_HEIGHT_LIMIT) if clamp else total

    # ------------------------------------------------------------------
    # On-sheet schedules (Task 10)
    # ------------------------------------------------------------------
    def _leg_rows(self, legs) -> List[List[str]]:
        """Bearing/distance rows for a run of traverse legs."""
        rows = []
        for leg in legs or []:
            if leg.bearing is None and leg.distance is None:
                continue
            line = f"{leg.from_.id}-{leg.to.id}"
            if leg.bearing is None:
                bearing = ""
            else:
                degrees = format_number(leg.bearing.degrees, "hundredth")
                minutes = format_number(leg.bearing.minutes, "tenth")
                bearing = f"{degrees}\u00b0 {minutes}'"
            distance = "" if leg.distance is None else f"{leg.distance:.{TABLE_DISTANCE_DECIMALS}f}"
            rows.append([line, bearing, distance])
        return rows

    def _coordinate_rows(self, coordinates) -> List[List[str]]:
        """Coordinate rows, northing before easting as surveyors quote them.

        Values are plain decimals: no thousands separator, so a figure read off
        the sheet matches the register and survives a paste into CAD.
        """
        rows = []
        seen = set()
        for coord in coordinates or []:
            if coord.id in seen:
                continue
            seen.add(coord.id)
            rows.append([
                coord.id,
                f"{coord.northing:.{TABLE_COORDINATE_DECIMALS}f}",
                f"{coord.easting:.{TABLE_COORDINATE_DECIMALS}f}",
            ])
        return rows

    def _bearing_distance_table(self) -> Optional[TableSpec]:
        """The plan type's bearing/distance schedule, or ``None`` if it has no
        legs to list."""
        return None

    def _coordinate_table(self) -> Optional[TableSpec]:
        """The plan type's coordinate schedule, or ``None``."""
        return None

    def _table_specs(self) -> List[TableSpec]:
        """Schedules the user asked to have drawn on the sheet.

        Memoised on first use. The band is reserved while the sheet is being
        sized, but a layout plan generates its plots (and its coordinate
        register) later, during ``draw()`` -- caching here keeps the width the
        sheet reserved and the table actually drawn in agreement instead of
        letting a regenerated register overflow its band.
        """
        cached = getattr(self, "_table_specs_cache", None)
        if cached is not None:
            return cached

        specs = []
        if self.show_bearing_distance_table:
            spec = self._bearing_distance_table()
            if spec is not None and spec.rows:
                specs.append(spec)
        if self.show_coordinate_table:
            spec = self._coordinate_table()
            if spec is not None and spec.rows:
                specs.append(spec)

        self._table_specs_cache = specs
        return specs

    def _table_text_height(self) -> float:
        return self.height("table", self.label_size)

    def _flow_tables(self, specs: List[TableSpec], band_height: float) -> List[ColumnRows]:
        """Lay the schedules out as columns of rows down the band.

        Tables stack vertically in a column while they fit and spill into a
        new column beside it when they do not, the way a schedule continues on
        a drawing sheet. A table split across columns repeats its title --
        marked ``(CONT.)`` -- and its headings, so each column reads on its
        own.
        """
        if not specs:
            return []

        row_height = self._table_text_height() * TABLE_ROW_SPACING
        # Every block costs a title row and a heading row, so a column has to
        # hold at least three rows to carry any data at all.
        capacity = max(3, int(band_height // row_height))
        width = max(len(spec.header) for spec in specs)

        def pad(row):
            return list(row) + [""] * (width - len(row))

        # Flow the rows continuously: a schedule fills whatever is left of the
        # current column before starting a new one, so a four-leg bearing
        # schedule does not cost a whole column of the band.
        columns: List[ColumnRows] = []
        current: List[List[List[str]]] = []
        used = 0

        for spec in specs:
            header = pad(spec.header)
            remaining = spec.rows
            first = True
            while remaining:
                # A block costs a title row and a heading row, plus a blank
                # separator when it follows another schedule in the column.
                overhead = 2 + (1 if current else 0)
                room = capacity - used - overhead
                if room < 1:
                    columns.append(ColumnRows(current))
                    current, used = [], 0
                    room = capacity - 2

                chunk, remaining = remaining[:room], remaining[room:]
                title = spec.title if first else f"{spec.title} (CONT.)"
                block = [pad([title]), header] + [pad(row) for row in chunk]
                if current:
                    used += 1
                current.append(block)
                used += len(block)
                first = False

                if remaining:
                    columns.append(ColumnRows(current))
                    current, used = [], 0

        if current:
            columns.append(ColumnRows(current))

        return columns

    def _measure_column(self, column: ColumnRows) -> TableColumn:
        """Cell widths for one column of the band, from the real font metrics.

        Measured across every block in the column so stacked schedules line up,
        and ignoring the title rows -- a title spans the table, so it widens
        the whole table rather than stretching the first column to its length.
        """
        text_height = self._table_text_height()
        padding = text_height * TABLE_CELL_PADDING

        def width_of(text):
            return self._drawer.text_width(text, text_height) + 2 * padding

        count = max(len(block[0]) for block in column.blocks)
        cells = [row for block in column.blocks for row in block[1:]]
        col_widths = [
            max((width_of(row[i]) for row in cells), default=text_height * 4)
            for i in range(count)
        ]

        # Widen the table, not one column, if a title needs more room.
        needed = max(width_of(block[0][0]) for block in column.blocks)
        total = sum(col_widths)
        if needed > total:
            extra = (needed - total) / count
            col_widths = [w + extra for w in col_widths]
            total = needed

        return TableColumn(column.blocks, col_widths, total)

    def _rows_per_column(self, band_height: float) -> int:
        row_height = self._table_text_height() * TABLE_ROW_SPACING
        return max(3, int(band_height // row_height))

    def _table_columns(self, band_height: float) -> List[TableColumn]:
        """Drawn columns, capped so the schedules can never take more than
        :data:`TABLE_BAND_LIMIT` of the sheet width.

        When a register is long enough to overrun that, every requested
        schedule still appears -- each is given a fair share of the columns
        that do fit and the ones that had to be cut short say so in their
        title. Dropping a schedule the user switched on would be the one
        outcome that leaves the sheet quietly wrong.
        """
        specs = self._table_specs()
        if not specs:
            return []

        columns = [self._measure_column(c)
                   for c in self._flow_tables(specs, band_height)]
        gap = TABLE_GAP_MM * self.mm_to_model
        printable_w_mm, _ = self.printable_area()
        limit = printable_w_mm * TABLE_BAND_LIMIT * self.mm_to_model

        def total(cols):
            return sum(c.width for c in cols) + gap * (len(cols) + 1)

        if total(columns) <= limit or len(columns) == 1:
            return columns

        # How many of those columns fit? Always keep at least one per table.
        allowed = 0
        used = gap
        for column in columns:
            if used + column.width + gap > limit:
                break
            used += column.width + gap
            allowed += 1
        allowed = max(1, allowed)

        # Share the rows those columns can hold between the schedules, rather
        # than giving each its own column: two narrow schedules stacked in one
        # column cost half the sheet width of two side by side. Flowing can
        # still spill into an extra column, so step the allowance down until
        # the band really fits.
        rows_per_column = self._rows_per_column(band_height)
        for count in range(allowed, 0, -1):
            furniture = 2 * len(specs) + (len(specs) - 1)
            capacity = max(len(specs), count * rows_per_column - furniture)
            budgets = self._share_rows([len(spec.rows) for spec in specs], capacity)

            trimmed = []
            for spec, budget in zip(specs, budgets):
                if len(spec.rows) <= budget:
                    trimmed.append(spec)
                    continue
                title = f"{spec.title} (FIRST {budget} OF {len(spec.rows)})"
                trimmed.append(TableSpec(title, spec.header, spec.rows[:budget]))

            columns = [self._measure_column(c)
                       for c in self._flow_tables(trimmed, band_height)]
            if total(columns) <= limit or count == 1:
                return columns

        return columns

    @staticmethod
    def _share_rows(wanted: List[int], capacity: int) -> List[int]:
        """Split ``capacity`` rows between schedules wanting ``wanted`` rows.

        Equal shares, with whatever a short schedule does not need passed on
        to the longer ones -- so a four-leg bearing schedule never costs the
        coordinate register half the space.
        """
        budgets = [0] * len(wanted)
        active = [i for i, n in enumerate(wanted) if n > 0]
        remaining = capacity

        while remaining > 0 and active:
            share = max(1, remaining // len(active))
            progressed = False
            for i in list(active):
                take = min(share, wanted[i] - budgets[i], remaining)
                if take <= 0:
                    active.remove(i)
                    continue
                budgets[i] += take
                remaining -= take
                progressed = True
                if budgets[i] >= wanted[i]:
                    active.remove(i)
            if not progressed:
                break

        return budgets

    def _table_band_mm(self) -> float:
        """Printed width the schedules need down the right of the sheet.

        Zero when no schedule is switched on, so a plan that does not use them
        keeps the whole sheet for its drawing.
        """
        if not self.true_scale:
            return 0.0

        specs = self._table_specs()
        if not specs:
            return 0.0

        _, printable_h_mm = self.printable_area()
        band_height_mm = (printable_h_mm - self._title_band_mm()
                          - FOOTER_HEIGHT_PERCENT * printable_h_mm)
        columns = self._table_columns(band_height_mm * self.mm_to_model)
        if not columns:
            return 0.0

        width = sum(c.width for c in columns) / self.mm_to_model
        return width + TABLE_GAP_MM * (len(columns) + 1)

    def _bottom_band_mm(self) -> float:
        """Extra printed height reserved above the footer boxes for a sheet
        element the plan type always draws there (layout plans put their
        land-use schedule here). Zero for plan types that draw nothing."""
        return 0.0

    def _title_band_mm(self) -> float:
        """Title band as printed millimetres.

        Every ingredient (text heights, the scale bar, the sheet itself) is a
        printed size, so the band is the same number of millimetres at every
        scale -- which is what lets the scale be chosen from it before the
        frame exists.
        """
        printable_w_mm, printable_h_mm = self.printable_area()
        frame_w = printable_w_mm * self.mm_to_model
        frame_h = printable_h_mm * self.mm_to_model
        return self._title_band_height(frame_w, frame_h) / self.mm_to_model

    def _usable_height_mm(self) -> float:
        """Printable height left for the drawing once the title stack and the
        footer band have taken theirs."""
        _, printable_h_mm = self.printable_area()
        return (printable_h_mm - self._title_band_mm() - self._bottom_band_mm()
                - FOOTER_HEIGHT_PERCENT * printable_h_mm)

    def _graphical_scale_length(self, frame_w: float, frame_h: float) -> float:
        """Nominal scale-bar length: sized from the smaller frame side so the
        title stack does not grow into the drawing on landscape sheets."""
        return min(frame_w, frame_h) * 0.4

    def _usable_sheet_size(self, frame_w: float, frame_h: float) -> tuple:
        """Drawing area inside the frame, once the title stack, footer boxes
        and any schedule band have taken their share."""
        title_band = self._title_band_height(frame_w, frame_h)
        return (frame_w - self._table_band_mm() * self.mm_to_model,
                frame_h - title_band - FOOTER_HEIGHT_PERCENT * frame_h
                - self._bottom_band_mm() * self.mm_to_model)

    def _check_fits_sheet(self, data_w: float, data_h: float,
                          frame_w: float, frame_h: float) -> None:
        """Fail with an actionable message when the survey cannot be drawn at
        the requested scale on the requested sheet."""
        usable_w, usable_h = self._usable_sheet_size(frame_w, frame_h)
        if data_w <= usable_w and data_h <= usable_h:
            return

        margin = self.annotation_margin()

        printable_w_mm, printable_h_mm = self.printable_area()
        usable_w_mm = printable_w_mm
        usable_h_mm = self._usable_height_mm()

        # Smallest standard scale that would hold this survey on this sheet.
        annotation_margin = self._annotation_margin_mm()
        usable_w_mm = max(usable_w_mm - 2 * annotation_margin, 1.0)
        usable_h_mm = max(usable_h_mm - 2 * annotation_margin, 1.0)
        needed = max((data_w - 2 * margin) / usable_w_mm,
                     (data_h - 2 * margin) / usable_h_mm) * 1000
        suggestion = next((s for s in STANDARD_SCALES if s >= needed), None)

        page = getattr(self.page_size, "value", self.page_size)
        orientation = getattr(self.page_orientation, "value", self.page_orientation)
        message = (
            f"The survey is {data_w:,.1f} m x {data_h:,.1f} m, which does not fit "
            f"on {page} {orientation} at 1:{int(self.scale)} "
            f"(the sheet holds {usable_w:,.1f} m x {usable_h:,.1f} m of ground)."
        )
        if suggestion is not None:
            message += f" Use a scale of 1:{suggestion} or smaller, or a larger sheet."
        else:
            message += " Use a larger sheet."
        raise ValueError(message)

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

    @property
    def scale_adjusted_from(self) -> Optional[float]:
        """The originally requested scale when the plan had to be zoomed out
        to fit the sheet, otherwise ``None``."""
        return self._scale_adjusted_from

    # ------------------------------------------------------------------
    # Element sizing (Task 8)
    # ------------------------------------------------------------------
    def height(self, element: str, legacy: float) -> float:
        """Height for a text element class, in model units.

        With ``auto_scale_sizes`` on (the default) the height comes from the
        printed-millimetre table resolved at the plan's scale, so choosing a
        scale is all a user has to do to get a legible plot. With it off the
        caller's ``legacy`` value -- the ``font_size`` / ``label_size`` /
        ``footer_size`` fields the API computes from the drawing extent -- is
        used instead, preserving the pre-Task-8 behaviour.

        Sheets that are *not* plotted at a true scale (route profiles) always
        take the legacy value: a printed millimetre size only converts to
        model units if the sheet has a scale to convert at, and a fitted sheet
        does not -- its printed size depends on the content extent, which is
        exactly what the extent-derived values already account for.
        """
        if self.auto_scale_sizes and self.true_scale:
            return self.text_height(element)
        return legacy

    @property
    def beacon_symbol_size(self) -> float:
        """Beacon symbol width in model units, from the Beacon Size control."""
        if not (self.auto_scale_sizes and self.true_scale):
            return self.beacon_size

        mm = BEACON_SYMBOL_MM
        if self.beacon_size and float(self.beacon_size) >= LEGACY_BEACON_MM:
            mm = min(float(self.beacon_size), BEACON_SYMBOL_MAX_MM)
        return mm * self.mm_to_model

    def _drawing_area(self) -> tuple:
        """(left, bottom, right, top) of the region the drawing may occupy.

        The frame less the schedule band, so sheet furniture anchored to the
        drawing (the north-arrow grid ticks) stops at the tables instead of
        running underneath them.
        """
        left, bottom, right, top = self._frame_coords
        return left, bottom, right - self._table_band_mm() * self.mm_to_model, top

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

    def _title_block_notes(self) -> list:
        """Extra note lines drawn below the origin/area in the title block.

        Empty by default; plan types override this to annotate the sheet with
        settings that matter for interpretation (e.g. the topographic contour
        interval).
        """
        return []

    # ------------------------------------------------------------------
    # Shared drawing routines
    # ------------------------------------------------------------------
    def draw_frames(self):
        frame_left, frame_bottom, frame_right, frame_top = self._frame_coords
        self._drawer.draw_frame(frame_left, frame_bottom, frame_right, frame_top)

    def draw_title_block(self):
        frame_left, frame_bottom, frame_right, frame_top = self._frame_coords
        min_x, min_y, max_x, max_y = self._bounding_box

        frame_width = frame_right - frame_left
        frame_height = frame_top - frame_bottom
        frame_center_x = frame_left + (frame_width / 2)

        if self.true_scale:
            # Fixed printed gap below the frame's top edge, so the stack fills
            # exactly the band the sheet reserved for it -- widened to clear
            # the north arrow when the origin easting runs behind the title.
            title_y = frame_top - self._title_top_gap(frame_height)
        else:
            # Content-fitted sheets have no reserved band; keep the stack
            # proportional to the gap above the data.
            title_y = frame_top - ((frame_top - max_y) * 0.2)

        self._drawer.draw_title_block(
            html_to_mtext(self.build_title(), font=self.font),
            frame_center_x,
            title_y,
            frame_width * TITLE_WIDTH_PERCENT,
            self.height("title", self.font_size),
            graphical_scale_length=self._graphical_scale_length(frame_width, frame_height),
            area=self._area_text(),
            origin=self._origin_text(),
            notes=self._title_block_notes(),
            note_height=self.height("title_note", self.font_size),
            scale_text_height=self.height("scale_bar", self.font_size * 0.5),
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
                plan_no_height = self.height("plan_number", self.label_size * 1.3)
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
                x1, y_min, x1 + box_width, y_min + box_height,
                self.height("surveyor_name", self.footer_size),
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

        leg_height = self.height("bearing_distance", self.label_size)

        if leg.distance is not None:
            self._drawer.add_label(f"{leg.distance:.2f}m", mid_x, mid_y,
                                   angle=text_angle, height=leg_height)

        if leg.bearing is None:
            return

        # Degrees and minutes as a single MText entity (professional
        # convention), centered on the leg with the two parts spread apart
        # so the label spans a fixed fraction of the leg.
        degrees_label = f"{format_number(leg.bearing.degrees, 'hundredth')}°"
        minutes_label = f"{format_number(leg.bearing.minutes, 'tenth')}'"
        self._drawer.add_split_mtext_label(
            degrees_label, minutes_label, bearing_x, bearing_y,
            angle=text_angle, height=leg_height,
            span=math.hypot(dx, dy) * 0.6,
        )

    def draw_tables(self):
        """Draw the enabled schedules in the band reserved for them.

        The band runs down the right of the sheet between the title stack and
        the footer boxes, and the drawing area was already narrowed by exactly
        this width in :meth:`_sheet_frame_coords`, so the tables cannot
        overlap the drawing, the title block or the footers.
        """
        band_mm = self._table_band_mm()
        if not band_mm:
            return

        frame_left, frame_bottom, frame_right, frame_top = self._frame_coords
        frame_h = frame_top - frame_bottom

        band_top = frame_top - self._title_band_height(frame_right - frame_left, frame_h)
        band_bottom = frame_bottom + FOOTER_HEIGHT_PERCENT * frame_h
        columns = self._table_columns(band_top - band_bottom)
        if not columns:
            return

        text_height = self._table_text_height()
        row_height = text_height * TABLE_ROW_SPACING
        gap = TABLE_GAP_MM * self.mm_to_model

        x = frame_right - band_mm * self.mm_to_model + gap
        for column in columns:
            y = band_top
            for block in column.blocks:
                self._drawer.draw_table(
                    x, y, block, column.col_widths, row_height, text_height,
                    layer="TABLES", span_rows={0},
                )
                y -= len(block) * row_height + row_height
            x += column.width + gap

    # ------------------------------------------------------------------
    # North arrow
    # ------------------------------------------------------------------
    def _north_arrow_reference(self) -> Optional[CoordinateProps]:
        """Coordinate the north arrow and grid lines are anchored to."""
        return None

    def _origin_value_ceiling(self, default: float) -> float:
        """How far up the sheet the vertical origin value may run.

        Defaults to the underside of the drawing. Plans that print their own
        annotation in that strip lower it, so the origin value stops short of
        theirs instead of overprinting it.
        """
        return default

    def _easting_tick_start(self, frame_top: float, frame_bottom: float) -> float:
        """Bottom of the vertical origin tick, held clear of the footer boxes."""
        y = frame_bottom
        if self._effective_footers():
            y += (frame_top - frame_bottom) * FOOTER_HEIGHT_PERCENT
        return y

    def _fit_grid_value(self, text: str, height: float, run: float) -> float:
        """Shrink an origin grid value until it fits the run available to it.

        The nominal height is the surveyor's own figure for a quoted
        coordinate, so it is only ever reduced, never raised. A value that
        runs off its tick and into the drawing reads as a mistake on the
        sheet; a slightly smaller one does not.
        """
        width = self._drawer.text_width(text, height)
        if width <= 0 or run <= 0 or width <= run:
            return height
        return max(FONT_SIZE_MIN_MM * self.mm_to_model, height * run / width)

    def draw_north_arrow(self):
        coord = self._north_arrow_reference()
        if coord is None:
            return

        frame_left, frame_bottom, frame_right, frame_top = self._drawing_area()
        height = (frame_top - frame_bottom) * NORTH_ARROW_HEIGHT_PERCENT

        # The block draws upward from its insertion point, so this puts the
        # base one arrow-height down and the tip on the frame top edge.
        self._drawer.draw_north_arrow(coord.easting, frame_top - height, height)

        # Grid ticks at the frame edges. Each carries the coordinate that is
        # constant along it -- the horizontal tick is a line of equal
        # northing, the vertical one a line of equal easting -- so a surveyor
        # can read the origin off the sheet the way a grid is read.
        quoted_height = self.height("quoted_coordinate", self.label_size)
        pad = GRID_LABEL_PAD_MM * self.mm_to_model
        default_tick = (frame_right - frame_left) * GRID_TICK_PERCENT

        northing_text = f"{coord.northing}mN"
        easting_text = f"{coord.easting}mE"

        # The northing value goes in the margin with the most clear space
        # between the frame and the drawing itself, since that margin is what
        # it has to fit into. Ties go left, which is where a surveyor looks
        # for it first.
        min_x, min_y, max_x, _ = self._bounding_box
        if min_x is None or max_x is None:
            min_x = max_x = coord.easting
        if min_y is None:
            min_y = coord.northing
        # Labels hang to the right of their points, so the drawing reaches
        # further that way than its bounding box; the right margin is smaller
        # than it looks.
        reach_x, _reach_y = self._label_reach()
        max_x += reach_x
        from_left = (min_x - frame_left) >= (frame_right - max_x)

        # Each value has to live in the gap between the frame and the drawing,
        # and clear of the other value standing on the tick that crosses it.
        if from_left:
            northing_run = min(min_x, coord.easting - quoted_height) - frame_left - pad
        else:
            northing_run = frame_right - max(max_x, coord.easting + quoted_height) - pad
        easting_y = self._easting_tick_start(frame_top, frame_bottom)
        easting_run = (self._origin_value_ceiling(min(min_y, coord.northing))
                       - easting_y - pad)

        # One size for both. They are read as a pair, and two coordinate
        # values set at visibly different heights read as an error rather
        # than as a fit, so whichever margin is tighter governs both.
        grid_height = min(
            self._fit_grid_value(northing_text, quoted_height, northing_run),
            self._fit_grid_value(easting_text, quoted_height, easting_run),
        )

        # A tick shorter than its own value looked like the number had come
        # adrift from the line, so each one is at least as long as the label
        # it carries.
        northing_tick = max(default_tick,
                            self._drawer.text_width(northing_text, grid_height) + pad)
        easting_tick = max(default_tick,
                           self._drawer.text_width(easting_text, grid_height) + pad)
        near = (frame_left, coord.northing) if from_left else (frame_right, coord.northing)
        far = ((frame_left + northing_tick, coord.northing) if from_left
               else (frame_right - northing_tick, coord.northing))
        self._drawer.add_north_arrow_label(near, far, northing_text, grid_height)

        # The opposite tick is drawn unlabelled: two marks read as a grid line
        # crossing the sheet, one reads as a stray dash.
        opposite = ((frame_right, coord.northing) if from_left
                    else (frame_left, coord.northing))
        opposite_end = ((frame_right - default_tick, coord.northing) if from_left
                        else (frame_left + default_tick, coord.northing))
        self._drawer.add_north_arrow_label(opposite, opposite_end, "", grid_height)

        # The easting runs up from the bottom edge, clear of the footer boxes.
        self._drawer.add_north_arrow_label(
            (coord.easting, easting_y), (coord.easting, easting_y + easting_tick),
            easting_text, grid_height,
        )
        # The locator cross is not a beacon symbol; the multiplier keeps it at
        # its previous absolute size now that the beacon symbol is smaller.
        self._drawer.draw_north_arrow_cross(
            coord.easting, coord.northing,
            self.beacon_symbol_size * NORTH_CROSS_BEACON_RATIO,
        )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def draw(self):
        raise NotImplementedError

    def save_dxf(self, file_path: str):
        self._drawer.save_dxf(file_path)

    def save(self) -> str:
        return self._drawer.save(
            paper_size=self.page_size,
            orientation=self.page_orientation,
            scale=self.plot_scale_mm_per_unit if self.true_scale else None,
        )
