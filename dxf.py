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
from upload import upload_file

class SurveyDXFManager:
    def __init__(self, plan_name: str = "Survey Plan", scale: float = 1.0):
        self.plan_name = plan_name
        self.scale = scale
        self.doc = ezdxf.new(dxfversion="R2010")
        self.msp = self.doc.modelspace()
        self._setup_layers()


        # set units
        self.doc.header["$INSUNITS"] = 6  # meters
        self.doc.header["$LUNITS"] = 2 # Decimal
        self.doc.header["$LUPREC"] = 3 # 3 decimal places
        self.doc.header["$AUNITS"] = 1 # Degrees/minutes/seconds
        self.doc.header["$AUPREC"] = 3 # 0d00'00"
        self.doc.header["$ANGBASE"] = 90.0  # set 0° direction to North

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
            ('SPOT_HEIGHTS', 3),  # Green
        ]

        for name, color in layers:
            self.doc.layers.add(name=name, color=color)

        # self.doc.layers.new('CONTOURS_MAJOR', dxfattribs={'color': colors.RED, 'lineweight': 50})
        # self.doc.layers.new('CONTOURS_MINOR', dxfattribs={'color': colors.BLUE, 'lineweight': 25})
        # self.doc.layers.new('CONTOUR_LABELS', dxfattribs={'color': colors.BLACK})
        # self.doc.layers.new("TIN_TRIANGLES", dxfattribs={'color': colors.CYAN, 'lineweight': 10})

    def setup_beacon_style(self, type_: str = "box", size: float = 1.0):
        size = size * self.scale

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

    def setup_font(self, font_name: str = "Times New Roman"):
        # Add a new text style with the specified font
        self.doc.styles.add('SURVEY_TEXT', font=f'{font_name}.ttf')

    def draw_beacon(self, x: float, y: float, z: float = 0, text_height: float = 1.0, label=None):
        # Add a beacon point with optional label
        x = x * self.scale
        y = y * self.scale
        z = z * self.scale
        text_height = text_height * self.scale

        self.msp.add_blockref(
            'BEACON_POINT',
            (x, y, z),
            dxfattribs={'layer': 'POINTS'}
        )

        # add label
        if label is not None:
            offset = 1 * self.scale
            self.msp.add_text(
                label,
                dxfattribs={
                    'layer': 'LABELS',
                    'height': text_height,
                    'style': 'SURVEY_TEXT'
                }
            ).set_placement(
                (x + offset, y + offset)
            )

    def add_parcel(self, parcel_id: str, points: list, label_scale: float = 1.0):
        """Add a parcel given its ID and list of (x, y) points"""
        # scale points
        points = [(x * self.scale, y * self.scale) for x, y, *rest in points]
        label_scale = label_scale * self.scale

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
                    'height': label_scale,
                    'style': 'SURVEY_TEXT',
                    'color': 2  # Yellow
                }
            ).set_placement(
                (centroid_x, centroid_y),
                align=TextEntityAlignment.MIDDLE_CENTER
            )

    def add_text(self, text: str, x: float, y: float, angle: float = 0.0, height: float = 1.0):
        x = x * self.scale
        y = y * self.scale
        height = height * self.scale

        """Add arbitrary text at given coordinates with optional rotation"""
        self.msp.add_text(
            text,
            dxfattribs={
                'layer': 'LABELS',
                'height': height,
                'style': 'SURVEY_TEXT',
                'rotation': angle
            }
        ).set_placement(
            (x , y),
            align=TextEntityAlignment.MIDDLE_CENTER
        )

    def add_title(self, text: str, x: float, y: float, width: float, title_height: float = 1.0):
        x = x * self.scale
        y = y * self.scale
        title_height = title_height * self.scale
        width = width * self.scale

        # Add title block text with underline
        title_mtext = self.msp.add_mtext(
            text=f"{MTextEditor.UNDERLINE_START}{text}{MTextEditor.UNDERLINE_STOP}",
            dxfattribs={'layer': 'TITLE_BLOCK', 'style': 'SURVEY_TEXT'},
        )
        title_mtext.set_location((x, y))
        title_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_CENTER
        title_mtext.dxf.char_height = title_height
        title_mtext.dxf.width = width

    def draw_frame(self, min_x, min_y, max_x, max_y):
        min_x = min_x * self.scale
        min_y = min_y * self.scale
        max_x = max_x * self.scale
        max_y = max_y * self.scale

        """Draw a rectangle given min and max coordinates"""
        self.msp.add_lwpolyline([
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y)
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

    def save_pdf(self, filepath: str = None, paper_size: str = "A4", orientation: str = "portrait"):
        # Paper sizes in mm
        paper_sizes = {
            "A4": (210, 297),
            "A3": (297, 420),
            "A5": (148, 210),
            "Letter": (216, 279),
            "Legal": (216, 356),
        }

        # Default to A4 if not found
        width, height = paper_sizes.get(paper_size.upper(), (210, 297))

        # Apply orientation
        if orientation.lower() == "landscape":
            width, height = height, width

        # Rendering
        context = RenderContext(self.doc)
        backend = pymupdf.PyMuPdfBackend()
        cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE)
        frontend = Frontend(context, backend, config=cfg)
        frontend.draw_layout(self.msp)

        # Create page with margins (20 mm here, can be parameterized)
        page = layout.Page(width, height, layout.Units.mm, margins=layout.Margins.all(20))

        # Output path
        if not filepath:
            filepath = f"{self.get_filename()}.pdf"

        # Save PDF
        pdf_bytes = backend.get_pdf_bytes(page)
        with open(filepath, "wb") as f:
            f.write(pdf_bytes)

    def save_dwg(self, dxf_filepath: str, filepath: str = None):
        if not filepath:
            filepath = f"{self.get_filename()}.dwg"
        odafc.convert(dxf_filepath, filepath)

    def save(self, paper_size: str = "A4", orientation: str = "portrait"):
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = self.get_filename()
            dxf_path = os.path.join(tmpdir, f"{filename}.dxf")
            # dwg_path =  os.path.join(tmpdir, f"{filename}.dwg")
            pdf_path =  os.path.join(tmpdir, f"{filename}.pdf")
            zip_path = os.path.join(tmpdir, f"{filename}.zip")

            self.save_dxf(dxf_path)
            # self.save_dwg(dxf_path, dwg_path)
            self.save_pdf(pdf_path, paper_size=paper_size, orientation=orientation)

            # Create a ZIP file containing all three formats
            with zipfile.ZipFile(zip_path, "w") as zipf:
                zipf.write(dxf_path, os.path.basename(dxf_path))
                # zipf.write(dwg_path, os.path.basename(dwg_path))
                zipf.write(pdf_path, os.path.basename(pdf_path))

            url = upload_file(zip_path, folder="survey_plans", file_name=filename)
            if url is None:
                raise Exception("Upload failed")
            return url

    # def add_topo_point(self, x: float, y: float, z: float, label: str = None, text_height: float = 1.0):
    #     self.msp.add_blockref(
    #         'TOPO_POINT',
    #         (x * self.scale, y * self.scale, z * self.scale),
    #         dxfattribs={'layer': 'SPOT_HEIGHTS'}
    #     )
    #
    #     # add label
    #     if label is not None:
    #         self.msp.add_text(
    #             label,
    #             dxfattribs={
    #                 'layer': 'LABELS',
    #                 'height': text_height * self.scale,
    #                 'style': 'SURVEY_TEXT'
    #             }
    #         ).set_placement(
    #             (x * self.scale + 1, y * self.scale + 1)
    #         )

    # def setup_topo_point_style(self, type_: str = "dot", size: float = 1.0):
    #     block = self.doc.blocks.new("TOPO_POINT")
    #
    #     if type_ == "dot":
    #         # just a dot
    #         block.add_circle((0, 0), radius=size * 0.1, dxfattribs={'color': 3})  # Green
    #     elif type_ == "circle":
    #         # circle with cross
    #         block.add_circle((0, 0), radius=size * 0.2, dxfattribs={'color': 3})
    #         block.add_line((-size * 0.2, 0), (size * 0.2, 0), dxfattribs={'color': 3})
    #         block.add_line((0, -size * 0.2), (0, size * 0.2), dxfattribs={'color': 3})
    #     else:
    #         # cross only
    #         block.add_line((-size * 0.2, 0), (size * 0.2, 0), dxfattribs={'color': 3})
    #         block.add_line((0, -size * 0.2), (0, size * 0.2), dxfattribs={'color': 3})
    #
    #     # Add a point entity at the center for snapping
    #     block.add_point((0, 0), dxfattribs={'color': 3})  # Green





