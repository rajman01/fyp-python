import ezdxf
import re
from ezdxf.enums import TextEntityAlignment
import matplotlib
matplotlib.use("Agg")  # no GUI
import matplotlib.pyplot as plt
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

class SurveyDXFManager:
    def __init__(self, plan_name: str = "Survey Plan", scale: float = 1.0):
        self.plan_name = plan_name
        self.scale = scale
        self.doc = ezdxf.new(dxfversion="R2010")
        self.msp = self.doc.modelspace()
        self._setup_layers()


        # set units
        # self.doc.header["$INSUNITS"] = 6  # meters

        # dimstyle = self.doc.dimstyles.get("Standard")
        # dimstyle.dxf.dimlunit = 2 # Decimal
        # dimstyle.dxf.dimdec = 3  # 3 decimal places
        # dimstyle.dxf.dimaunit = 1 # Degrees/minutes/seconds
        # dimstyle.dxf.dimadec = 3  # 3 decimal places
        # dimstyle.dxf.dimscale = 1.0

    def _setup_layers(self):
        """Setup standard survey layers"""
        layers = [
            ('POINTS', 7),  # Black/White
            ('LINES', 1),  # Red
            ('LABELS', 7), # Black/White
            ('GRID', 8),  # Dark gray
            ('FRAME', 7),  # Black/White
            ('DIMENSIONS', 4),  # Cyan
            ('TITLE_BLOCK', 7),  # White
            ('TRAVERSE', 6),  # Magenta
        ]

        for name, color in layers:
            self.doc.layers.add(name=name, color=color)

    def setup_beacon_style(self, type_: str = "box", size: float = 1.0):
        # Point styles (using blocks)
        block = self.doc.blocks.new(name='BEACON_POINT')
        radius = size * 0.2  # inner hatch radius
        half = size / 2  # half-size for square

        # Filled (solid hatch) circle
        if type_ == "circle":
            block.add_circle((0, 0), radius=size * 0.5)

            # Hatched inner circle
            hatch = block.add_hatch(color=7)  # 7 = black/white
            path = hatch.paths.add_edge_path()
            path.add_arc((0, 0), radius=radius, start_angle=0, end_angle=360)
        elif type_ == "box":
            # Square boundary
            block.add_lwpolyline(
                [(-half, -half), (half, -half), (half, half), (-half, half)],
                close=True
            )

            # Hatched inner circle
            hatch = block.add_hatch(color=7)
            path = hatch.paths.add_edge_path()
            path.add_arc((0, 0), radius=radius, start_angle=0, end_angle=360)
        elif type_ == "dot":
            # Just hatched circle (no boundary)
            hatch = block.add_hatch(color=7)
            path = hatch.paths.add_edge_path()
            path.add_arc((0, 0), radius=radius, start_angle=0, end_angle=360)

    def setup_font(self, font_name: str = "Arial"):
        # Add a new text style with the specified font
        self.doc.styles.add('SURVEY_TEXT', font=f'{font_name}.ttf')


    def add_beacon(self, x: float, y: float, z: float = 0, text_height: float = 1.0, label=None):
        # Add a beacon point with optional label
        self.msp.add_blockref(
            'BEACON_POINT',
            (x * self.scale, y * self.scale, z * self.scale),
            dxfattribs={'layer': 'POINTS'}
        )

        # add label
        if label is not None:
            self.msp.add_text(
                label,
                dxfattribs={
                    'layer': 'LABELS',
                    'height': text_height * self.scale,
                    'style': 'SURVEY_TEXT'
                }
            ).set_placement(
                (x * self.scale + 1, y * self.scale + 1)
            )

    def add_parcel(self, parcel_id: str, points: list):
        """Add a parcel given its ID and list of (x, y) points"""
        # scale points
        points = [(x * self.scale, y * self.scale) for x, y, *rest in points]

        self.msp.add_lwpolyline(points, close=True, dxfattribs={
            'layer': 'LINES'
        })

        # Add parcel ID label at centroid
        if points and parcel_id:
            centroid_x = sum(p[0] for p in points) / len(points)
            centroid_y = sum(p[1] for p in points) / len(points)
            self.msp.add_text(
                parcel_id,
                dxfattribs={
                    'layer': 'LABELS',
                    'height': 2.0 * self.scale,
                    'style': 'SURVEY_TEXT',
                    'color': 2  # Yellow
                }
            ).set_placement(
                (centroid_x, centroid_y),
                align=TextEntityAlignment.MIDDLE_CENTER
            )

    def draw_frame(self, min_x, min_y, max_x, max_y):
        """Draw a rectangle given min and max coordinates"""
        self.msp.add_lwpolyline([
            (min_x * self.scale, min_y* self.scale),
            (max_x* self.scale, min_y* self.scale),
            (max_x* self.scale, max_y* self.scale),
            (min_x* self.scale, max_y* self.scale)
        ], close=True, dxfattribs={
            'layer': 'FRAME',
        })

    def get_filename(self):
        plan_name = self.plan_name.lower()
        plan_name = re.sub(r"\s+", "_",plan_name)
        plan_name = re.sub(r"[^a-z0-9._-]", "", plan_name)
        plan_name = re.sub(r"_+", "_", plan_name)
        return plan_name

    def save_dxf(self):
        """Save the DXF document to a file"""
        self.doc.saveas(f"{self.get_filename()}.dxf")


    def dxf_to_pdf(self, margin_ratio: float = 0.05):


        # doc = ezdxf.readfile(dxf_path)
        # msp = doc.modelspace()

        # compute bounding box
        xs, ys = [], []
        for e in self.msp:
            if e.dxftype() == "LINE":
                xs += [e.dxf.start[0], e.dxf.end[0]]
                ys += [e.dxf.start[1], e.dxf.end[1]]
            elif e.dxftype() == "CIRCLE":
                xs += [e.dxf.center[0] - e.dxf.radius, e.dxf.center[0] + e.dxf.radius]
                ys += [e.dxf.center[1] - e.dxf.radius, e.dxf.center[1] + e.dxf.radius]

        if not xs or not ys:
            xs = [0, 10]; ys = [0, 10]  # fallback box

        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        width = max(max_x - min_x, 1)   # avoid zero
        height = max(max_y - min_y, 1)

        fig, ax = plt.subplots(figsize=(8, 8 * height / width))
        ax.set_xlim(min_x, max_x)
        ax.set_ylim(min_y, max_y)
        ax.axis("off")

        ctx = RenderContext(self.doc)
        out = MatplotlibBackend(ax)
        Frontend(ctx, out).draw_layout(self.msp, finalize=True)

        pdf_path = f"{self.get_filename()}.pdf"
        fig.savefig(pdf_path, bbox_inches="tight", pad_inches=0)
        plt.close(fig)



