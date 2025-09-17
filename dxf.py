import ezdxf
import re
from ezdxf.enums import TextEntityAlignment
from ezdxf.addons.drawing import Frontend, RenderContext, pymupdf, layout, config
from ezdxf.tools.text import MTextEditor
from ezdxf.addons import odafc
import tempfile
import os
from datetime import datetime
import uuid
import zipfile
import shutil

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

    def add_text(self, text: str, x: float, y: float, angle: float = 0.0, height: float = 1.0):
        """Add arbitrary text at given coordinates with optional rotation"""
        self.msp.add_text(
            text,
            dxfattribs={
                'layer': 'LABELS',
                'height': height * self.scale,
                'style': 'SURVEY_TEXT',
                'rotation': angle
            }
        ).set_placement(
            (x * self.scale, y * self.scale),
            align=TextEntityAlignment.MIDDLE_CENTER
        )

    def add_title(self, text: str, x: float, y: float, width: float, title_height: float = 1.0):
        title_mtext = self.msp.add_mtext(
            text=f"{MTextEditor.UNDERLINE_START}{text}{MTextEditor.UNDERLINE_STOP}",
            dxfattribs={'layer': 'TITLE_BLOCK', 'style': 'SURVEY_TEXT'},
        )
        title_mtext.set_location((x * self.scale, y * self.scale))
        title_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_CENTER
        title_mtext.dxf.char_height = title_height * self.scale
        title_mtext.dxf.width = width * self.scale

    def draw_frame(self, min_x, min_y, max_x, max_y):
        """Draw a rectangle given min and max coordinates"""
        self.msp.add_lwpolyline([
            (min_x * self.scale, min_y * self.scale),
            (max_x * self.scale, min_y * self.scale),
            (max_x * self.scale, max_y * self.scale),
            (min_x * self.scale, max_y * self.scale)
        ], close=True, dxfattribs={
            'layer': 'FRAME',
        })

    def get_filename(self):
        plan_name = self.plan_name.lower()
        plan_name = re.sub(r"\s+", "_",plan_name)
        plan_name = re.sub(r"[^a-z0-9._-]", "", plan_name)
        plan_name = re.sub(r"_+", "_", plan_name)
        return f"{plan_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def save_dxf(self, filepath: str = None):
        """Save the DXF document to a file"""
        if filepath:
            self.doc.saveas(filepath)
            return
        dxf_path = f"{self.get_filename()}.dxf"
        self.doc.saveas(dxf_path)

    def save_pdf(self, filepath: str = None):
        context = RenderContext(self.doc)
        backend = pymupdf.PyMuPdfBackend()
        cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE)
        frontend = Frontend(context, backend, config=cfg)
        frontend.draw_layout(self.msp)
        page = layout.Page(210, 297, layout.Units.mm, margins=layout.Margins.all(20))
        if not filepath:
            filepath = f"{self.get_filename()}.pdf"
        pdf_bytes = backend.get_pdf_bytes(page)
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)

    def save_dwg(self, dxf_filepath: str, filepath: str = None):
        if not filepath:
            filepath = f"{self.get_filename()}.dwg"
        odafc.convert(dxf_filepath, filepath)

    def save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = self.get_filename()
            dxf_path = os.path.join(tmpdir, f"{filename}.dxf")
            dwg_path =  os.path.join(tmpdir, f"{filename}.dwg")
            pdf_path =  os.path.join(tmpdir, f"{filename}.pdf")
            zip_path = os.path.join(tmpdir, f"{filename}.zip")

            self.save_dxf(dxf_path)
            self.save_dwg(dxf_path, dwg_path)
            self.save_pdf(pdf_path)

            # Create a ZIP file containing all three formats
            with zipfile.ZipFile(zip_path, "w") as zipf:
                zipf.write(dxf_path, os.path.basename(dxf_path))
                zipf.write(dwg_path, os.path.basename(dwg_path))
                zipf.write(pdf_path, os.path.basename(pdf_path))


            # Copy DWG to a permanent location
            shutil.copy(zip_path, f"{filename}.zip")





