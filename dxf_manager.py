"""Low-level DXF drawing manager.

Wraps ezdxf to provide the drawing primitives used by the plan generators
(beacons, parcels, contours, frames, title blocks, ...).

Every coordinate and size passed to this class is in **true model units**
(metres) and is written to the DXF unchanged: the drawing is 1:1 in ground
coordinates, so a beacon at easting 543210 sits at x=543210 in the file and a
surveyor can snap to it and read the real value. Plot scale is applied once,
at render time, by :meth:`SurveyDXFManager.save_pdf`.

Callers size annotation (text heights, symbols) by converting a printed
millimetre size to model units at the plan scale -- see
``PlanProps.text_height`` -- rather than passing fractions of the drawing
extent.
"""

import logging
import math
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime
from typing import List, Optional, Tuple

import ezdxf
from ezdxf import bbox, colors
from ezdxf.addons import odafc
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, pymupdf
from ezdxf.enums import TextEntityAlignment
from ezdxf.fonts import fonts as ezfonts
from ezdxf.tools.text import MTextEditor
from ezdxf.tools.text_size import mtext_size

from upload import upload_file

logger = logging.getLogger(__name__)

# Metric-compatible substitutes used to *measure* text when the style's real
# font is not installed (e.g. in the Docker container). The DXF still names
# the original font; only the width/height estimates use the substitute.
MEASUREMENT_FONT_SUBSTITUTES = {
    "times new roman": "LiberationSerif-Regular.ttf",
    "arial": "LiberationSans-Regular.ttf",
    "courier new": "LiberationMono-Regular.ttf",
}

# Print margin the PDF renderer leaves on every side of the sheet, in mm.
PAGE_MARGIN_MM = 20.0

# Paper sizes in mm (width, height) for portrait orientation.
PAPER_SIZES = {
    "A0": (841, 1189),
    "A1": (594, 841),
    "A2": (420, 594),
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "LETTER": (216, 279),
    "LEGAL": (216, 356),
}


def nice_round(value: float) -> float:
    """Round a positive value to the nearest 'nice' number (1, 2, 2.5, 5 x 10^k)."""
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    frac = value / 10 ** exp
    nice = min((1.0, 2.0, 2.5, 5.0, 10.0), key=lambda n: abs(n - frac))
    return nice * 10 ** exp


class SurveyDXFManager:
    def __init__(self, plan_name: str = "Survey Plan", mm_to_model: float = 1.0,
                 dxf_version: str = "R2000"):
        """``mm_to_model`` is how many model units print as one millimetre at
        the plan's scale. Geometry never uses it -- coordinates go in at true
        ground values -- it exists only for the handful of DXF settings that
        are absolute model sizes with no caller-supplied value (the point
        display size and hatch pattern spacing)."""
        self.plan_name = plan_name
        self.mm_to_model = mm_to_model
        self.dxf_version = dxf_version
        self.doc = ezdxf.new(dxfversion=dxf_version)
        self.msp = self.doc.modelspace()
        self.setup_layers()

        # Document units
        self.doc.header["$INSUNITS"] = 6  # meters
        self.doc.header["$LUNITS"] = 2  # decimal
        self.doc.header["$LUPREC"] = 3  # 3 decimal places
        self.doc.header["$AUNITS"] = 1  # degrees/minutes/seconds
        self.doc.header["$AUPREC"] = 3  # 0d00'00"
        self.doc.header["$ANGBASE"] = 90.0  # 0 degrees points North
        # Absolute point display size; the default (0 = relative to viewport)
        # is unsupported by the PDF renderer and triggers a log warning.
        # Expressed as a printed size so POINT entities stay a consistent dot
        # on paper instead of growing with the plan scale.
        self.doc.header["$PDSIZE"] = 0.6 * self.mm_to_model

    # ------------------------------------------------------------------
    # Layer / style setup
    # ------------------------------------------------------------------
    def setup_layers(self):
        self.doc.layers.add(name="LABELS", color=colors.BLACK)
        self.doc.layers.add(name="TABLES", color=colors.BLACK)
        self.doc.layers.add(name="FRAME", color=colors.BLACK)
        self.doc.layers.add(name="TITLE_BLOCK", color=colors.BLACK)
        self.doc.layers.add(name="FOOTER", color=colors.BLACK)

    def setup_cadastral_layers(self):
        self.doc.layers.add(name="BEACONS", color=colors.BLACK)
        self.doc.layers.add(name="PARCELS", color=colors.RED)

    def setup_topographic_layers(self):
        self.doc.layers.add(name="BEACONS", color=colors.BLACK)
        self.doc.layers.add(name="BOUNDARY", color=colors.RED)
        self.doc.layers.add("CONTOUR_MAJOR", true_color=colors.rgb2int((127, 31, 0)),
                            linetype="Continuous", lineweight=35)
        self.doc.layers.add("CONTOUR_MINOR", true_color=colors.rgb2int((127, 31, 0)),
                            linetype="Continuous", lineweight=18)
        self.doc.layers.add("CONTOUR_LABELS", true_color=colors.rgb2int((127, 31, 0)))
        self.doc.layers.add("TIN_MESH", color=colors.GRAY, linetype="Continuous", lineweight=9)
        self.doc.layers.add("GRID_MESH", color=colors.LIGHT_GRAY, linetype="Dot", lineweight=9)
        self.doc.layers.add("SPOT_HEIGHTS", true_color=colors.rgb2int((205, 105, 40)),
                            linetype="Continuous", lineweight=25)

    def setup_layout_layers(self):
        self.doc.layers.add(name="BEACONS", color=colors.BLACK)
        self.doc.layers.add(name="BOUNDARY", color=colors.RED, linetype="CONTINUOUS", lineweight=50)
        self.doc.layers.add(name="PARCELS", color=colors.GREEN, linetype="CONTINUOUS", lineweight=25)
        self.doc.layers.add(name="ROADS", color=colors.BLACK, linetype="CONTINUOUS", lineweight=35)
        self.doc.layers.add(name="ROADS_CL", color=colors.CYAN, linetype="DASHDOT", lineweight=18)
        self.doc.layers.add(name="SETBACKS", color=colors.MAGENTA, linetype="DASHED", lineweight=18)
        self.doc.layers.add(name="DIMENSIONS", color=colors.YELLOW, linetype="CONTINUOUS", lineweight=18)
        self.doc.layers.add(name="TEXT", color=colors.BLACK, linetype="CONTINUOUS", lineweight=18)
        self.doc.layers.add(name="GREEN_SPACE", color=colors.GREEN, linetype="CONTINUOUS", lineweight=25)
        self.doc.layers.add(name="UTILITIES", color=colors.BLUE, linetype="DASHED", lineweight=18)
        self.doc.layers.add(name="EASEMENTS", true_color=colors.rgb2int((255, 165, 0)),
                            linetype="DASHDOT", lineweight=18)
        self.doc.layers.add(name="BUILDABLE", color=colors.GRAY, linetype="DASHDOT", lineweight=18)

    def setup_route_layers(self):
        self.doc.layers.add(name="GRID", color=colors.BLACK)
        self.doc.layers.add(name="F-GRID", color=colors.YELLOW, linetype="DASHDOT")
        self.doc.layers.add(name="TEXT", color=colors.BLUE)
        self.doc.layers.add(name="PROFILE", color=colors.RED)
        # plan view (horizontal alignment)
        self.doc.layers.add(name="ALIGNMENT", color=colors.RED, linetype="DASHDOT", lineweight=35)
        self.doc.layers.add(name="ROW", color=colors.BLACK, linetype="DASHED", lineweight=18)
        self.doc.layers.add(name="STATIONS", color=colors.BLUE, lineweight=13)

    def setup_font(self, font_name: str = "Times New Roman"):
        self.doc.styles.add("SURVEY_TEXT", font=f"{font_name}.ttf")

    def setup_beacon_style(self, type_: str = "box", size: float = 1.0):
        block = self.doc.blocks.new(name="BEACON_POINT")
        radius = size * 0.2  # inner hatch radius
        half = size / 2

        if type_ == "circle":
            block.add_circle((0, 0), radius=size * 0.5)
        elif type_ == "box":
            block.add_lwpolyline(
                [(-half, -half), (half, -half), (half, half), (-half, half)],
                close=True,
            )
        elif type_ == "none":
            return

        # Hatched inner circle shared by all visible styles
        hatch = block.add_hatch(color=7)
        path = hatch.paths.add_edge_path()
        path.add_arc((0, 0), radius=radius, start_angle=0, end_angle=360)

    def setup_topo_point_style(self, size: float = 1.0):
        block = self.doc.blocks.new(name="TOPO_POINT")

        # cross with a colored point at the center
        block.add_line((-size, -size), (size, size))
        block.add_line((-size, size), (size, -size))
        block.add_point((0, 0), dxfattribs={"true_color": colors.rgb2int((205, 105, 40))})

    # ------------------------------------------------------------------
    # Drawing primitives
    # ------------------------------------------------------------------
    def draw_beacon(self, x: float, y: float, z: float = 0,
                    text_height: float = 1.0, label: Optional[str] = None):
        """Add a beacon point with an optional label offset from the point."""

        self.msp.add_blockref("BEACON_POINT", (x, y, z), dxfattribs={"layer": "BEACONS"})

        if label is not None:
            offset = 0.8 * self.mm_to_model
            self.msp.add_text(
                label,
                dxfattribs={"layer": "LABELS", "height": text_height, "style": "SURVEY_TEXT"},
            ).set_placement((x + offset, y + offset))

    def add_parcel(self, points: List[Tuple[float, float]]):
        points = [(x, y) for x, y, *_ in points]
        self.msp.add_lwpolyline(points, close=True, dxfattribs={"layer": "PARCELS"})

    def add_boundary(self, points: List[Tuple[float, float]]):
        points = [(x, y) for x, y, *_ in points]
        self.msp.add_lwpolyline(points, close=True, dxfattribs={"layer": "BOUNDARY"})

    def add_buildable(self, points: List[Tuple[float, float]]):
        points = [(x, y) for x, y, *_ in points]
        self.msp.add_lwpolyline(points, close=True, dxfattribs={"layer": "BUILDABLE"})

    def add_road_cl(self, points: List[Tuple[float, float]]):
        points = [(x, y) for x, y, *_ in points]
        self.msp.add_lwpolyline(points, dxfattribs={"layer": "ROADS_CL"})

    def add_road(self, points: List[Tuple[float, float]]):
        points = [(x, y) for x, y, *_ in points]
        self.msp.add_lwpolyline(points, dxfattribs={"layer": "ROADS"})

    def add_polyline(self, points: List[Tuple[float, float]], layer: str, close: bool = False):
        """Add a generic 2D polyline on the given layer."""
        points = [(x, y) for x, y, *_ in points]
        self.msp.add_lwpolyline(points, close=close, dxfattribs={"layer": layer})

    def add_greenspace(self, points: List[Tuple[float, float]]):
        points = [(x, y) for x, y, *_ in points]
        self.msp.add_lwpolyline(points, close=True, dxfattribs={"layer": "GREEN_SPACE"})

        hatch = self.msp.add_hatch(dxfattribs={"layer": "GREEN_SPACE"})
        # Pattern spacing is in model units; tie it to the printed size so the
        # hatch reads at the same density on paper at any scale.
        hatch.set_pattern_fill("ANSI31", scale=max(2.0 * self.mm_to_model, 1e-6))
        hatch.paths.add_polyline_path(points, is_closed=True)

    def add_label(self, text: str, x: float, y: float, angle: float = 0.0, height: float = 1.0,
                  alignment=TextEntityAlignment.MIDDLE_CENTER):
        """Add single-line text on the LABELS layer (centered by default)."""

        self.msp.add_text(
            text,
            dxfattribs={
                "layer": "LABELS",
                "height": height,
                "style": "SURVEY_TEXT",
                "rotation": angle,
            },
        ).set_placement((x, y), align=alignment)

    def add_mtext_label(self, text: str, x: float, y: float, angle: float = 0.0,
                        height: float = 1.0, layer: str = "LABELS"):
        """Add a single-line MText label centered at (x, y)."""

        mtext = self.msp.add_mtext(text, dxfattribs={
            "layer": layer,
            "style": "SURVEY_TEXT",
            "char_height": height,
        })
        mtext.set_location(
            (x, y),
            rotation=angle,
            attachment_point=ezdxf.enums.MTextEntityAlignment.MIDDLE_CENTER,
        )
        return mtext

    def add_split_mtext_label(self, left: str, right: str, x: float, y: float,
                              angle: float = 0.0, height: float = 1.0, span: float = 0.0,
                              layer: str = "LABELS"):
        """Single MText label whose ``left`` and ``right`` parts are padded
        apart with spaces so the whole label spans roughly ``span`` model
        units, centered at (x, y)."""
        font_file = "txt"
        if "SURVEY_TEXT" in self.doc.styles:
            font_file = self.doc.styles.get("SURVEY_TEXT").dxf.font or "txt"
        font = self._measurement_font(font_file, height)

        text_width = font.text_width(left + right)
        space_width = max(font.text_width("| |") - font.text_width("||"), 1e-9)
        spaces = max(1, round((span - text_width) / space_width))

        return self.add_mtext_label(f"{left}{' ' * spaces}{right}", x, y,
                                    angle=angle, height=height, layer=layer)

    def add_text(self, text: str, x: float, y: float, height: float = 1.0,
                 rotation: float = 0.0, alignment=TextEntityAlignment.TOP_LEFT):
        """Add single-line text on the TEXT layer."""

        self.msp.add_text(
            text,
            dxfattribs={
                "layer": "TEXT",
                "height": height,
                "style": "SURVEY_TEXT",
                "rotation": rotation,
            },
        ).set_placement((x, y), align=alignment)

    # ------------------------------------------------------------------
    # North arrow
    # ------------------------------------------------------------------
    def draw_north_arrow(self, x: float, y: float, height: float = 100.0, rotation: float = 0.0):
        """Draw the north arrow; ``rotation`` (CCW degrees) supports rotated
        plan views where sheet-up is not true north."""

        if "NORTH_ARROW" not in self.doc.blocks:
            block = self.doc.blocks.new(name="NORTH_ARROW")

            arrow_size = height * 0.4
            bulge = math.tan(math.radians(250) / 4) * -1
            block.add_lwpolyline(
                [(0, 0), (0, height), (-arrow_size / 2, height - arrow_size, bulge),
                 (-arrow_size / 2, height - (arrow_size * 2))],
                format="xyb", dxfattribs={"color": 5},
            )

            block.add_text(
                "U", dxfattribs={"height": height * 0.2, "color": 5, "style": "SURVEY_TEXT"},
            ).set_placement((-height * 0.3, height - (height * 0.2)),
                            align=TextEntityAlignment.MIDDLE_CENTER)

            block.add_text(
                "N", dxfattribs={"height": height * 0.2, "color": 5, "style": "SURVEY_TEXT"},
            ).set_placement((height * 0.2, height - (height * 0.2)),
                            align=TextEntityAlignment.MIDDLE_CENTER)

        self.msp.add_blockref("NORTH_ARROW", (x, y), dxfattribs={"rotation": rotation})

    def add_north_arrow_label(self, start: Tuple[float, float], stop: Tuple[float, float],
                              label: str = "", height: float = 100.0):
        """Draw an origin grid tick with its coordinate value sitting on it.

        The value starts at ``start`` -- the frame edge -- and runs along the
        tick towards ``stop``, resting on the line the way text rests on a
        baseline. Callers size the tick to the label so the number never runs
        off the end of the line it belongs to.

        Text is never drawn upside down: a tick that runs right-to-left keeps
        its value the right way up and hangs it back from the frame edge
        instead.
        """
        x, y = start
        stop_x, stop_y = stop

        self.msp.add_line((x, y), (stop_x, stop_y), dxfattribs={"color": 5})

        if not label:
            return

        angle = math.atan2(stop_y - y, stop_x - x)

        # Keep the text within a readable half-turn. A tick pointing left or
        # down would otherwise render the value mirrored.
        reversed_run = not (-math.pi / 2 < angle <= math.pi / 2)
        if reversed_run:
            # Normalised rather than just rotated by pi, so a right-to-left
            # tick records a rotation of 0 in the DXF instead of 360.
            angle = math.atan2(-(stop_y - y), -(stop_x - x))

        ux, uy = math.cos(angle), math.sin(angle)   # reading direction
        nx, ny = -uy, ux                            # perpendicular, left of it

        # The inset follows the tick itself, not the reading direction: on a
        # right-to-left tick those are opposite, and following the text would
        # push the value out through the frame border.
        length = math.hypot(stop_x - x, stop_y - y) or 1.0
        rx, ry = (stop_x - x) / length, (stop_y - y) / length

        # Just clear of the line so the glyphs do not print through it, and
        # set in from the frame edge so the value does not touch the border.
        clearance = height * 0.2
        inset = height * 0.3
        placement = (x + rx * inset + nx * clearance,
                     y + ry * inset + ny * clearance)
        alignment = (TextEntityAlignment.BOTTOM_RIGHT if reversed_run
                     else TextEntityAlignment.BOTTOM_LEFT)

        self.msp.add_text(
            label,
            dxfattribs={
                "height": height,
                "color": 5,
                "style": "SURVEY_TEXT",
                "rotation": math.degrees(angle),
            },
        ).set_placement(placement, align=alignment)

    def draw_north_arrow_cross(self, x: float, y: float, length: float = 100.0):
        half = length / 2

        self.msp.add_line((x - half, y), (x + half, y), dxfattribs={"color": 5})
        self.msp.add_line((x, y - half), (x, y + half), dxfattribs={"color": 5})

    # ------------------------------------------------------------------
    # Graphical scale & title block
    # ------------------------------------------------------------------
    def draw_graphical_scale(self, x: float, y: float, length: float = 1000.0,
                             text_height: Optional[float] = None):
        """Draw a scale bar around ``length`` model-metres long at (x, y).

        The bar has 5 intervals; the interval is snapped to a 'nice' ground
        distance so the tick labels reflect true distances on the plan.

        ``text_height`` sizes the tick labels in model units. Left unset they
        fall back to a fraction of the bar, which goes unreadable on a short
        bar -- callers should pass a printed size instead.
        """
        # Snap the interval to a nice ground distance and rebuild the length
        interval_m = nice_round(length / 5)
        length = interval_m * 5

        height = length * 0.05  # bar height, 5% of length
        interval = length / 5
        if text_height is None or text_height <= 0:
            text_height = height * 0.5
        # Tick labels sit clear above the ticks, which rise to 1.5x the bar
        # height. They are placed by their *bottom* edge: hanging them from the
        # top put the last 40% of every glyph back down over the tick it
        # labelled.
        label_y = height * 1.5 + text_height * 0.35

        block_name = f"GRAPHICAL_SCALE_{len(self.doc.blocks)}"
        block = self.doc.blocks.new(name=block_name)

        # outer rectangle and middle line
        block.add_lwpolyline(
            [(0, 0), (length, 0), (length, height), (0, height)],
            close=True, dxfattribs={"color": 7},
        )
        block.add_line((0, height / 2), (length, height / 2), dxfattribs={"color": 7})

        def label_for(i: int) -> str:
            # Bar starts one interval before zero (the subdivided cell).
            return f"{(i - 1) * interval_m:g}"

        to_shade = "up"
        for i in range(6):
            tick_x = i * interval
            block.add_line((tick_x, 0), (tick_x, height * 1.5), dxfattribs={"color": 7})

            text = label_for(i)
            alignment = TextEntityAlignment.BOTTOM_CENTER
            if i == 0:
                text = f"Meters {interval_m:g}"
                alignment = TextEntityAlignment.BOTTOM_RIGHT
            if i == 5:
                text = f"{label_for(i)} Meters"
                alignment = TextEntityAlignment.BOTTOM_LEFT

            block.add_text(
                text,
                dxfattribs={"height": text_height, "color": 7, "style": "SURVEY_TEXT"},
            ).set_placement((tick_x, label_y), align=alignment)

            if i == 5:
                continue

            # Alternate shading of upper/lower halves; the first cell is
            # subdivided into two half-interval cells.
            sub_intervals = 2 if i == 0 else 1
            sub_width = interval / sub_intervals
            for j in range(sub_intervals):
                sub_x = tick_x + j * sub_width
                if to_shade == "up":
                    corners = [(sub_x, height / 2), (sub_x + sub_width, height / 2),
                               (sub_x + sub_width, height), (sub_x, height)]
                    to_shade = "down"
                else:
                    corners = [(sub_x, 0), (sub_x + sub_width, 0),
                               (sub_x + sub_width, height / 2), (sub_x, height / 2)]
                    to_shade = "up"
                hatch = block.add_hatch(color=7)
                hatch.paths.add_polyline_path(corners)

        return self.msp.add_blockref(block_name, (x, y), dxfattribs={"layer": "TITLE_BLOCK"})

    def text_width(self, text: str, height: float) -> float:
        """Rendered width of a single-line string at ``height``, in model units.

        Uses the drawing style's real font metrics (or a metric-compatible
        substitute), so table cells are sized to the text that will actually be
        drawn rather than to a per-character guess.
        """
        font_file = "txt"
        if "SURVEY_TEXT" in self.doc.styles:
            font_file = self.doc.styles.get("SURVEY_TEXT").dxf.font or "txt"
        return self._measurement_font(font_file, height).text_width(str(text))

    def measure_mtext(self, text: str, char_height: float, width: float) -> Tuple[float, float]:
        """Rendered (width, height) of an MTEXT in model units.

        Used to reserve exactly as much sheet as the title stack needs before
        anything is placed, instead of assuming a fixed fraction and having a
        long title spill into the drawing.
        """
        scratch = self.doc.blocks.new(name=f"MEASURE_{uuid.uuid4().hex[:8]}")
        mtext = scratch.add_mtext(text, dxfattribs={"style": "SURVEY_TEXT"})
        mtext.dxf.char_height = char_height
        mtext.dxf.width = width
        size = mtext_size(mtext)
        self.doc.blocks.delete_block(scratch.name, safe=False)
        return size.total_width, size.total_height

    def draw_title_block(self, text: str, x: float, y: float, width: float,
                         title_height: float = 1.0, graphical_scale_length: float = 1000.0,
                         origin: str = "", area: str = "", notes: Optional[List[str]] = None,
                         note_height: Optional[float] = None,
                         scale_text_height: Optional[float] = None):

        block = self.doc.blocks.new(name="TITLE_BLOCK")
        title_mtext = block.add_mtext(
            text=f"{MTextEditor.UNDERLINE_START}{text}{MTextEditor.UNDERLINE_STOP}",
            dxfattribs={"style": "SURVEY_TEXT"},
        )
        title_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_CENTER
        title_mtext.dxf.char_height = title_height
        title_mtext.dxf.width = width

        self.msp.add_blockref("TITLE_BLOCK", (x, y), dxfattribs={"layer": "TITLE_BLOCK"})

        # Measure the wrapped text rather than the block's bounding box:
        # bbox.extents() estimates MTEXT from its definition and comes up a
        # full line short once the title wraps, which used to drop the
        # graphical scale on top of the last title line.
        title_size = mtext_size(title_mtext)
        title_min_y = y - title_size.total_height
        title_min_x = x - title_size.total_width / 2
        title_max_x = x + title_size.total_width / 2

        # draw_graphical_scale snaps the bar to a nice round interval, so
        # center using the length that will actually be drawn.
        graphical_scale_length = nice_round(graphical_scale_length / 5) * 5

        title_center_x = (title_min_x + title_max_x) / 2
        graphical_x = title_center_x - (graphical_scale_length / 2)

        # Clear the title by the full height of the bar's own stack: the tick
        # labels sit above the bar, so placing the bar itself just below the
        # title would run those labels straight into it.
        bar_height = graphical_scale_length * 0.05
        if scale_text_height is None or scale_text_height <= 0:
            scale_text_height = bar_height * 0.5
        bar_stack = bar_height * 1.5 + scale_text_height * 2.8

        # graphical scale below the title
        graphical_ref = self.draw_graphical_scale(
            graphical_x,
            title_min_y - bar_stack,
            graphical_scale_length,
            text_height=scale_text_height,
        )
        graphical_box = bbox.extents(graphical_ref.virtual_entities())
        graphical_min_y = graphical_box.extmin.y

        # area, origin and any extra notes below the graphical scale
        lines = []
        if area:
            lines.append(rf"\C1;{area}")
        if origin:
            lines.append(rf"\C5;{origin}")
        for note in notes or []:
            if note:
                lines.append(rf"\C7;{note}")
        if not lines:
            return

        origin_mtext = self.msp.add_mtext(
            text=f"{MTextEditor.UNDERLINE_START}{MTextEditor.NEW_LINE.join(lines)}{MTextEditor.UNDERLINE_STOP}",
            dxfattribs={"style": "SURVEY_TEXT"},
        )
        origin_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_CENTER
        origin_mtext.dxf.char_height = note_height if note_height else title_height
        origin_mtext.dxf.width = width
        origin_mtext.set_location((x, graphical_min_y - bar_height))

    # ------------------------------------------------------------------
    # Frames & footers
    # ------------------------------------------------------------------
    def draw_footer_box(self, text: str, min_x, min_y, max_x, max_y,
                        font_size: float = 1.0, top_inset: float = 0.0):
        """Draw a footer rectangle with MText content inside.

        The text height is clamped so the estimated number of wrapped lines
        always fits inside the box instead of overflowing across the frame.
        ``top_inset`` reserves space below the box's top edge (e.g. for the
        plan number) before the footer text starts.
        """

        box_width = max_x - min_x
        box_height = max_y - min_y

        self.msp.add_lwpolyline(
            [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)],
            close=True, dxfattribs={"layer": "FOOTER"},
        )

        if not text.strip():
            return

        # Estimate wrapped line count from the plain text and shrink the
        # font until it fits (line spacing ~1.67x char height in MText).
        segments = text.split(MTextEditor.NEW_LINE)
        plain = [re.sub(r"\\f[^;]*;|\\[A-Za-z]|[{}]", "", seg) for seg in segments]
        height = font_size
        for _ in range(4):
            capacity = max((box_width * 0.9) / (0.55 * height), 1.0)
            lines = sum(max(1, math.ceil(len(seg) / capacity)) for seg in plain)
            needed = lines * height * 1.67
            allowed = max(box_height * 0.85 - top_inset, box_height * 0.1)
            if needed <= allowed:
                break
            height *= allowed / needed

        footer_mtext = self.msp.add_mtext(
            text=text,
            dxfattribs={"layer": "FOOTER", "style": "SURVEY_TEXT"},
        )
        footer_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_LEFT
        footer_mtext.dxf.width = box_width * 0.9
        footer_mtext.dxf.char_height = height
        # top-left corner with some padding
        footer_mtext.set_location(
            (min_x + (0.05 * box_width), max_y - (0.1 * box_height) - top_inset)
        )

    def draw_frame(self, min_x, min_y, max_x, max_y):
        """Draw a rectangular frame given min and max coordinates."""

        self.msp.add_lwpolyline(
            [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)],
            close=True, dxfattribs={"layer": "FRAME"},
        )

    def draw_table(self, x: float, y: float, rows: List[List[str]],
                   col_widths: List[float], row_height: float,
                   text_height: float = 1.0, layer: str = "TEXT",
                   span_rows: Optional[set] = None):
        """Draw a simple grid table with (x, y) as its top-left corner.

        ``rows`` is a list of rows; each row is a list of cell strings.
        ``col_widths`` and ``row_height`` are in model units. Rows listed in
        ``span_rows`` run the full width of the table -- the column dividers
        stop above and below them -- which is how a schedule's title row is
        drawn.
        """
        span_rows = span_rows or set()

        table_width = sum(col_widths)
        table_height = row_height * len(rows)

        # outer border
        self.msp.add_lwpolyline(
            [(x, y), (x + table_width, y), (x + table_width, y - table_height), (x, y - table_height)],
            close=True, dxfattribs={"layer": layer},
        )

        # horizontal lines
        for i in range(1, len(rows)):
            line_y = y - i * row_height
            self.msp.add_line((x, line_y), (x + table_width, line_y), dxfattribs={"layer": layer})

        # Vertical dividers, broken across any row that spans the table.
        cx = x
        for width in col_widths[:-1]:
            cx += width
            run_start = None
            for i in range(len(rows) + 1):
                spans = i < len(rows) and i in span_rows
                if not spans and run_start is None:
                    run_start = i
                if (spans or i == len(rows)) and run_start is not None:
                    self.msp.add_line(
                        (cx, y - run_start * row_height),
                        (cx, y - i * row_height),
                        dxfattribs={"layer": layer},
                    )
                    run_start = None

        # cell text (left-aligned, vertically centered)
        padding = text_height * 0.4
        for i, row in enumerate(rows):
            cell_x = x
            cell_y = y - (i + 0.5) * row_height
            for j, cell in enumerate(row):
                self.msp.add_text(
                    str(cell),
                    dxfattribs={
                        "layer": layer,
                        "height": text_height,
                        "style": "SURVEY_TEXT",
                    },
                ).set_placement((cell_x + padding, cell_y), align=TextEntityAlignment.MIDDLE_LEFT)
                cell_x += col_widths[j]

    # ------------------------------------------------------------------
    # Topographic primitives
    # ------------------------------------------------------------------
    def draw_topo_point(self, x: float, y: float, z: float = 0,
                        label: Optional[str] = None, text_height: float = 1.0):

        self.msp.add_blockref("TOPO_POINT", (x, y, z), dxfattribs={"layer": "SPOT_HEIGHTS"})

        if label is not None:
            offset = 0.25 * text_height
            self.msp.add_text(
                label,
                dxfattribs={
                    "layer": "SPOT_HEIGHTS",
                    "height": text_height,
                    "style": "SURVEY_TEXT",
                    "color": 7,
                },
            ).set_placement((x + offset, y + offset, z + offset))

    def add_tin_mesh(self, points: List[Tuple[float, float, float]]):
        self.msp.add_polyline3d(points, dxfattribs={"layer": "TIN_MESH"})

    def add_grid_mesh(self, points: List[Tuple[float, float, float]]):
        self.msp.add_polyline3d(points, dxfattribs={"layer": "GRID_MESH"})

    def add_grid_mesh_border(self, points: List[Tuple[float, float, float]]):
        self.msp.add_polyline3d(
            points,
            dxfattribs={"layer": "GRID_MESH", "lineweight": 25},
        )

    def add_grid_mesh_label(self, x: float, y: float, z: float, label: str,
                            text_height: float = 1.0, rotation: float = 0.0):

        self.msp.add_text(label, dxfattribs={
            "layer": "GRID_MESH",
            "height": text_height,
            "style": "SURVEY_TEXT",
            "rotation": rotation,
        }).set_placement((x, y, z))

    def add_3d_contour(self, points: List[Tuple[float, float, float]], layer: str = "CONTOUR_MINOR"):
        self.msp.add_polyline3d(points, dxfattribs={"layer": layer})

    def add_spline(self, points: List[Tuple[float, float, float]], layer: str = "CONTOUR_MINOR"):
        self.msp.add_spline(points, degree=3, dxfattribs={"layer": layer})

    def add_contour_label(self, x: float, y: float, z: float, label: str, text_height: float = 1.0):
        self.msp.add_text(label, dxfattribs={
            "layer": "CONTOUR_LABELS",
            "height": text_height,
        }).set_placement((x, y, z), align=TextEntityAlignment.MIDDLE_CENTER)

    # ------------------------------------------------------------------
    # Route (longitudinal profile) primitives
    # ------------------------------------------------------------------
    def add_grid_line(self, x1: float, y1: float, x2: float, y2: float):
        self.msp.add_line(
            (x1, y1),
            (x2, y2),
            dxfattribs={"layer": "GRID"},
        )

    def add_f_grid_line(self, x1: float, y1: float, x2: float, y2: float):
        self.msp.add_line(
            (x1, y1),
            (x2, y2),
            dxfattribs={"layer": "F-GRID"},
        )

    def add_profile(self, points: List[Tuple[float, float]]):
        self.msp.add_spline(points, dxfattribs={"layer": "PROFILE"})

    # ------------------------------------------------------------------
    # Layer visibility & output
    # ------------------------------------------------------------------
    def toggle_layer(self, layer: str, state: bool):
        layer_entity = self.doc.layers.get(layer)
        if state:
            layer_entity.on()
        else:
            layer_entity.off()

    def _measurement_font(self, font_name: str, cap_height: float):
        """Font used to estimate text extents, cached per (font, height).

        Falls back to a metric-compatible substitute when the style's font is
        not installed, so estimated widths stay close to what CAD software
        with the real font will render.
        """
        cache = getattr(self, "_font_cache", None)
        if cache is None:
            cache = self._font_cache = {}
        key = (font_name, round(cap_height, 9))
        font = cache.get(key)
        if font is None:
            name = font_name
            face = ezfonts.font_manager.get_font_face(name)
            stem = os.path.splitext(os.path.basename(font_name))[0].lower()
            if face is not None and stem not in face.family.lower():
                substitute = MEASUREMENT_FONT_SUBSTITUTES.get(stem)
                if substitute is not None:
                    sub_face = ezfonts.font_manager.get_font_face(substitute)
                    if sub_face is not None and "liberation" in sub_face.family.lower():
                        name = substitute
            font = cache[key] = ezfonts.make_font(name, cap_height)
        return font

    def fix_justified_text_insert_points(self):
        """Recompute the baseline-left insertion point of justified TEXT.

        AutoCAD draws DWG TEXT glyphs starting at the insertion point
        (group 10) and keeps the alignment point (group 11) as editing
        metadata, while the ODA converter copies both points through
        unchanged. ezdxf leaves the insertion point equal to the alignment
        point (the DXF reference allows this because DXF readers must use
        the alignment point), so every centered/right/top justified label
        rendered as left/baseline justified once converted to DWG.
        """
        spaces = [self.msp] + [block for block in self.doc.blocks]
        for space in spaces:
            for text in space.query("TEXT"):
                halign = text.dxf.halign
                valign = text.dxf.valign
                if (halign == 0 and valign == 0) or halign in (3, 5):
                    # baseline-left already, or ALIGNED/FIT dual-point modes
                    continue
                if not text.dxf.hasattr("align_point"):
                    continue

                style_name = text.dxf.style
                font_file = "txt"
                if style_name in self.doc.styles:
                    font_file = self.doc.styles.get(style_name).dxf.font or "txt"
                cap_height = text.dxf.height
                font = self._measurement_font(font_file, cap_height)

                width = font.text_width(text.dxf.text) * text.dxf.width
                m = font.measurements

                dx = 0.0
                if halign in (1, 4):  # center / middle
                    dx = -width / 2
                elif halign == 2:  # right
                    dx = -width

                if halign == 4:  # MIDDLE: centered on the full glyph extent
                    dy = (m.descender_height - m.cap_height) / 2
                elif valign == 1:  # bottom (descender line)
                    dy = m.descender_height
                elif valign == 2:  # middle of capitals
                    dy = -m.cap_height / 2
                elif valign == 3:  # top of capitals
                    dy = -m.cap_height
                else:  # baseline
                    dy = 0.0

                rot = math.radians(text.dxf.rotation)
                cos_r, sin_r = math.cos(rot), math.sin(rot)
                align = text.dxf.align_point
                text.dxf.insert = (
                    align.x + dx * cos_r - dy * sin_r,
                    align.y + dx * sin_r + dy * cos_r,
                    align.z,
                )

    def get_filename(self) -> str:
        plan_name = self.plan_name.lower()
        plan_name = re.sub(r"\s+", "_", plan_name)
        plan_name = re.sub(r"[^a-z0-9._-]", "", plan_name)
        plan_name = re.sub(r"_+", "_", plan_name)
        return f"{plan_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def save_dxf(self, filepath: Optional[str] = None):
        if not filepath:
            filepath = f"{self.get_filename()}.dxf"
        self.fix_justified_text_insert_points()
        self.doc.saveas(filepath)

    def save_pdf(self, filepath: Optional[str] = None, paper_size: str = "A4",
                 orientation: str = "portrait", scale: Optional[float] = None):
        """Render the modelspace to a PDF.

        ``scale`` is the printed millimetres per model unit -- ``1000/500 = 2``
        for a 1:500 plan drawn in metres. When given, the sheet is plotted at
        exactly that scale, so a distance measured on the paper is the real
        ground distance divided by the scale denominator. When it is ``None``
        the content is fitted to the page instead (used by route sheets, whose
        horizontal and vertical scales differ and so have no single map
        scale).

        Known limitation: the PyMuPDF backend writes the page box in whole
        points (A4 becomes 595 x 841 pt instead of 595.276 x 841.890), so the
        rendered content is uniformly 0.106% smaller than the true scale --
        0.13 mm over a 120 mm distance. The DXF and DWG carry exact true
        coordinates and are unaffected; on the PDF the graphical scale bar is
        rendered in the same space, so measuring against the bar cancels the
        error out.
        """
        width, height = PAPER_SIZES.get(paper_size.upper(), PAPER_SIZES["A4"])
        if orientation.lower() == "landscape":
            width, height = height, width

        context = RenderContext(self.doc)
        backend = pymupdf.PyMuPdfBackend()
        cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE)
        frontend = Frontend(context, backend, config=cfg)
        frontend.draw_layout(self.msp)

        page = layout.Page(width, height, layout.Units.mm,
                           margins=layout.Margins.all(PAGE_MARGIN_MM))
        if scale is None:
            settings = layout.Settings(fit_page=True)
        else:
            settings = layout.Settings(fit_page=False, scale=scale)

        if not filepath:
            filepath = f"{self.get_filename()}.pdf"

        pdf_bytes = backend.get_pdf_bytes(page, settings=settings)
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)

    def save_dwg(self, dxf_filepath: str, filepath: Optional[str] = None):
        """Convert a saved DXF to DWG using the ODA File Converter."""
        if not filepath:
            filepath = f"{self.get_filename()}.dwg"
        odafc.convert(dxf_filepath, filepath, version=self.dxf_version)

    def save(self, paper_size: str = "A4", orientation: str = "portrait",
             extra_files: Optional[dict] = None, scale: Optional[float] = None) -> str:
        """Export DXF + DWG + PDF, zip them, and upload the archive.

        ``extra_files`` maps file names to text content and is bundled into
        the ZIP as well (e.g. setting-out coordinate CSVs).
        Returns the public URL of the uploaded ZIP archive.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = self.get_filename()
            dxf_path = os.path.join(tmpdir, f"{filename}.dxf")
            dwg_path = os.path.join(tmpdir, f"{filename}.dwg")
            pdf_path = os.path.join(tmpdir, f"{filename}.pdf")
            zip_path = os.path.join(tmpdir, f"{filename}.zip")

            self.save_dxf(dxf_path)
            self.save_dwg(dxf_path, dwg_path)
            self.save_pdf(pdf_path, paper_size=paper_size, orientation=orientation,
                          scale=scale)

            with zipfile.ZipFile(zip_path, "w") as zipf:
                zipf.write(dxf_path, os.path.basename(dxf_path))
                zipf.write(dwg_path, os.path.basename(dwg_path))
                zipf.write(pdf_path, os.path.basename(pdf_path))
                for name, content in (extra_files or {}).items():
                    zipf.writestr(name, content)

            url = upload_file(zip_path, folder="survey_plans", file_name=filename)
            if url is None:
                raise RuntimeError("Failed to upload generated plan archive")
            return url
