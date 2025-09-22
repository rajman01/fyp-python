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
from ezdxf import bbox, colors
import subprocess
import math
from typing import List, Tuple, Dict, Optional, Union



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
        self.doc.layers.add(name="BEACONS", color=colors.BLACK)
        self.doc.layers.add(name="PARCELS", color=colors.RED)
        self.doc.layers.add(name="LABELS", color=colors.BLACK)
        self.doc.layers.add(name="FRAME", color=colors.BLACK)
        self.doc.layers.add(name="TITLE_BLOCK", color=colors.BLACK)
        self.doc.layers.add(name="FOOTER", color=colors.BLACK)
        self.doc.layers.add(name="BOUNDARY", color=colors.RED)
        self.doc.layers.add('CONTOUR_MAJOR', true_color=ezdxf.colors.rgb2int((127, 31, 0)), linetype="Continuous", lineweight=35)
        self.doc.layers.add('CONTOUR_MINOR', true_color=ezdxf.colors.rgb2int((127, 31, 0)), linetype="Continuous",
                            lineweight=18)
        self.doc.layers.add('CONTOUR_LABELS', true_color=ezdxf.colors.rgb2int((127, 31, 0)))
        self.doc.layers.add('TIN_MESH', color=colors.GRAY, linetype="Continuous",
                            lineweight=9)
        self.doc.layers.add('GRID_MESH', color=colors.LIGHT_GRAY, linetype="Dot",
                            lineweight=9)
        self.doc.layers.add('SPOT_HEIGHTS', true_color=ezdxf.colors.rgb2int((205, 105, 40)), linetype="Continuous",
                            lineweight=25)

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

    def setup_topo_point_style(self, type_: str = "cross", size: float = 1):
        size = size * self.scale

        # Point styles (using blocks)
        block = self.doc.blocks.new(name='TOPO_POINT')

        # cross only
        block.add_line((-size, -size), (size, size))
        block.add_line((-size, size), (size, -size))
        block.add_point((0, 0), dxfattribs={"true_color": ezdxf.colors.rgb2int((205, 105, 40))})  # Green

    def setup_font(self, font_name: str = "Times New Roman"):
        # Add a new text style with the specified font
        self.doc.styles.add('SURVEY_TEXT', font=f'{font_name}.ttf')

    def draw_beacon(self, x: float, y: float, z: float = 0, text_height: float = 1.0, extent: float = 1000, label=None):
        # Add a beacon point with optional label
        x = x * self.scale
        y = y * self.scale
        z = z * self.scale
        text_height = text_height * self.scale

        self.msp.add_blockref(
            'BEACON_POINT',
            (x, y, z),
            dxfattribs={'layer': 'BEACONS'}
        )

        # add label
        if label is not None:
            offset = self.scale * extent * 0.01
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

    def add_parcel(self, parcel_id: str, points: List[Tuple[float, float]], label_size: float = 1.0):
        """Add a parcel given its ID and list of (x, y) points"""
        # scale points
        points = [(x * self.scale, y * self.scale) for x, y, *rest in points]
        label_size = label_size * self.scale

        self.msp.add_lwpolyline(points, close=True, dxfattribs={
            'layer': 'PARCELS'
        })

        # Add parcel ID label at centroid
        # if points and parcel_id:
        #     centroid_x = sum(p[0] for p in points) / len(points)
        #     centroid_y = sum(p[1] for p in points) / len(points)
        #     self.msp.add_text(
        #         parcel_id,
        #         dxfattribs={
        #             'layer': 'LABELS',
        #             'height': label_size,
        #             'style': 'SURVEY_TEXT',
        #             'color': 2  # Yellow
        #         }
        #     ).set_placement(
        #         (centroid_x, centroid_y),
        #         align=TextEntityAlignment.MIDDLE_CENTER
        #     )

    def add_boundary(self, points: List[Tuple[float, float]]):
        """Add a boundaty given its ID and list of (x, y) points"""
        # scale points
        points = [(x * self.scale, y * self.scale) for x, y, *rest in points]

        self.msp.add_lwpolyline(points, close=True, dxfattribs={
            'layer': 'BOUNDARY'
        })

    def add_text(self, text: str, x: float, y: float, angle: float = 0.0, height: float = 1.0):
        x = x * self.scale
        y = y * self.scale
        height = height * self.scale

        """Add arbitrary text at given coordinates with optional rotation"""
        text = self.msp.add_text(
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

    def draw_north_arrow(self, x: float, y: float, height: float = 100.0):
        height = height * self.scale
        x = x * self.scale
        y = y * self.scale

        # create a block for the north arrow
        block = self.doc.blocks.new(name='NORTH_ARROW')

        arrow_size = height * 0.4
        bulge = math.tan(math.radians(250) / 4) * -1
        block.add_lwpolyline(
            [(0, 0), (0, height), (-arrow_size / 2, height - arrow_size, bulge), (-arrow_size / 2, height - (arrow_size * 2))],
            format='xyb', dxfattribs={'color': 5}
        )

        # add text above arrow
        block.add_text(
            "U",
            dxfattribs={
                'height': height * 0.2,
                'color': 5,
            }
        ).set_placement(
            ( -height * 0.3, height - (height * 0.25)),
            align=TextEntityAlignment.MIDDLE_CENTER
        )

        block.add_text(
            "N",
            dxfattribs={
                'height': height * 0.2,
                'color': 5,
            }
        ).set_placement(
            (height * 0.2, height - (height * 0.25)),
            align=TextEntityAlignment.MIDDLE_CENTER
        )

        # add to modelspace
        self.msp.add_blockref(
            'NORTH_ARROW',
            (x, y),
        )

    def draw_graphical_scale(self, x: float, y: float, length: float = 1000.0):
        X = x * self.scale
        Y = y * self.scale
        length = length * self.scale
        height = length * 0.05  # 5% of length

        interval = length / 5  # 5 intervals

        # Create a block for the graphical scale
        block = self.doc.blocks.new(name='GRAPHICAL_SCALE')

        # draw large rectangle
        block.add_lwpolyline(
            [(0, 0), (length, 0), (length, height), (0, height)],
            close=True,
            dxfattribs={'color': 7}  # Black/White
        )

        # draw middle line
        block.add_line(
            (0, height / 2),
            (length, height / 2),
            dxfattribs={'color': 7}
        )

        text_interval = 1000 / self.scale / 10 / 5

        # draw interval lines
        to_shade = "up"
        for i in range(6):
            x = i * interval
            line_height = height * 1.5
            block.add_line(
                (x, 0),
                (x, line_height),
                dxfattribs={'color': 7}
            )

            text = f"{int((i - 1) * text_interval)}"
            alignment = TextEntityAlignment.TOP_CENTER
            if i == 0:
                text = f"Meters {int(text_interval)}"
                alignment = TextEntityAlignment.TOP_RIGHT
            if i == 5:
                text = f"{int((i - 1) * text_interval)} Meters"
                alignment = TextEntityAlignment.TOP_LEFT

            # add text above line
            block.add_text(
                text,
                dxfattribs={
                    'height': height * 0.5,
                    'color': 7,
                    'style': 'SURVEY_TEXT'
                }
            ).set_placement(
                (x, height * 2.3),
                align=alignment
            )

            if i == 5:
                continue

            if i == 0:
                mini_interval = interval / 2
                for j in range(2):
                    mini_x = j * mini_interval
                    if to_shade == "up":
                        # shade first upper half
                        hatch = block.add_hatch(color=7)
                        hatch.paths.add_polyline_path([(mini_x, height / 2), (mini_x + mini_interval, height / 2),
                                                       (mini_x + mini_interval, height), (mini_x, height)])
                        to_shade = "down"
                    else:
                        # shade lower half
                        hatch = block.add_hatch(color=7)
                        hatch.paths.add_polyline_path(
                            [(mini_x, 0), (mini_x + mini_interval, 0), (mini_x + mini_interval, height / 2),
                             (mini_x, height / 2)])
                        to_shade = "up"
            else:
                if to_shade == "up":
                    hatch = block.add_hatch(color=7)
                    hatch.paths.add_polyline_path(
                        [(x, height / 2), (x + interval, height / 2), (x + interval, height), (x, height)])
                    to_shade = "down"
                else:
                    hatch = block.add_hatch(color=7)
                    hatch.paths.add_polyline_path(
                        [(x, 0), (x + interval, 0), (x + interval, height / 2), (x, height / 2)])
                    to_shade = "up"

        return self.msp.add_blockref(
            'GRAPHICAL_SCALE',
            (X, Y),
            dxfattribs={'layer': 'TITLE_BLOCK'}
        )

    def draw_title_block(self, text: str, x: float, y: float, width: float, title_height: float = 1.0, graphical_scale_length: float = 1000.0, origin: str = "", area: str = ""):
        x = x * self.scale
        y = y * self.scale
        title_height = title_height * self.scale
        width = width * self.scale
        graphical_scale_length = graphical_scale_length * self.scale

        block = self.doc.blocks.new(name='TITLE_BLOCK')
        title_mtext = block.add_mtext(
            text=f"{MTextEditor.UNDERLINE_START}{text}{MTextEditor.UNDERLINE_STOP}",
            dxfattribs={'style': 'SURVEY_TEXT'},
        )
        title_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_CENTER
        title_mtext.dxf.char_height = title_height
        title_mtext.dxf.width = width

        # add block to modelspace
        title_ref = self.msp.add_blockref(
            'TITLE_BLOCK',
            (x, y),
            dxfattribs={'layer': 'TITLE_BLOCK'}
        )

        title_box = bbox.extents(title_ref.virtual_entities())
        title_min_y = title_box.extmin.y
        title_min_x = title_box.extmin.x
        title_max_x = title_box.extmax.x

        title_length = title_max_x - title_min_x
        graphical_x = title_min_x + ((title_length / 2) - (graphical_scale_length / 2))

        # draw graphical scale below title
        graphical_ref = self.draw_graphical_scale(graphical_x / self.scale, (title_min_y - (graphical_scale_length * 0.05 * 3)) / self.scale, graphical_scale_length / self.scale)
        graphical_box = bbox.extents(graphical_ref.virtual_entities())
        graphical_min_y = graphical_box.extmin.y

        origin_mtext = self.msp.add_mtext(
            text=f"{MTextEditor.UNDERLINE_START}\C5;{area}{MTextEditor.NEW_LINE}\C1;{origin}{MTextEditor.UNDERLINE_STOP}",
            dxfattribs={'style': 'SURVEY_TEXT'},
        )
        origin_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_CENTER
        origin_mtext.dxf.char_height = title_height
        origin_mtext.dxf.width = width
        origin_mtext.set_location((x, graphical_min_y - ((graphical_scale_length * 0.05) / 3)))

    def draw_footer_box(self, text: str, min_x, min_y, max_x, max_y, font_size: float = 1.0):
        font_size = font_size * self.scale
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
            'layer': 'FOOTER',
        })

        # add text inside box
        footer_mtext = self.msp.add_mtext(
            text=text,
            dxfattribs={
                'layer': 'FOOTER',
                'style': 'SURVEY_TEXT',
                # 'height': (max_y - min_y) * 0.8,
            }
        )
        footer_mtext.dxf.attachment_point = ezdxf.enums.MTextEntityAlignment.TOP_LEFT
        footer_mtext.dxf.width = (max_x - min_x) * 0.9
        # set location at top-left corner with some padding
        footer_mtext.set_location((min_x + (0.05 * (max_x - min_x)), max_y - (0.1 * (max_y - min_y))))
        footer_mtext.dxf.char_height = font_size

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

    def draw_topo_point(self, x: float, y: float, z: float = 0, label: str = None, text_height: float = 1.0):
        # Add a topo point with optional label
        x = x * self.scale
        y = y * self.scale
        z = z * self.scale
        text_height = text_height * self.scale

        self.msp.add_blockref(
            'TOPO_POINT',
            (x, y, z),
            dxfattribs={'layer': 'SPOT_HEIGHTS'}
        )

        # add label
        if label is not None:
            offset = 0.25 * text_height
            self.msp.add_text(
                label,
                dxfattribs={
                    'layer': 'SPOT_HEIGHTS',
                    'height': text_height,
                    'style': 'Standard',
                    'color': 7  # Black/White
                }
            ).set_placement(
                (x + offset, y + offset, z + offset)
            )

    def add_tin_mesh(self, points: List[Tuple[float, float, float]]):
        points = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in points]

        # Add as 3D polyline
        self.msp.add_polyline3d(
            points,
            dxfattribs={'layer': 'TIN_MESH'}
        )

    def add_grid_mesh(self, points: List[Tuple[float, float, float]]):
        points = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in points]

        # Add as 3D polyline
        self.msp.add_polyline3d(
            points,
            dxfattribs={'layer': 'GRID_MESH'}
        )

    def add_grid_mesh_label(self, x: float, y: float, z: float, label: str, text_height: float = 1.0, rotation: float = 0.0):
        x = x * self.scale
        y = y * self.scale
        z = z * self.scale
        text_height = text_height * self.scale

        self.msp.add_text(label, dxfattribs={
            "layer": "GRID_MESH",
            "height": text_height,
            "style": "Standard",
            "rotation": rotation
        }).set_placement((x, y, z),)

    def add_grid_mesh_border(self, points: List[Tuple[float, float, float]]):
        points = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in points]

        self.msp.add_polyline3d(
            points,
            dxfattribs={
                'layer': 'GRID_MESH',
                'lineweight': 25  # Slightly thicker for border
            }
        )

    def add_grid_mesh_corner_coords(self, x: float, y: float, z: float, label: str, text_height: float = 1.0, rotation: float = 0.0):
        x = x * self.scale
        y = y * self.scale
        z = z * self.scale
        text_height = text_height * self.scale

        self.msp.add_text(label, dxfattribs={
            "layer": "GRID_MESH",
            "height": text_height,
            "style": "Standard",
            "rotation": rotation
        }).set_placement((x, y, z),)

    def add_3d_contour(self, points: List[Tuple[float, float, float]], layer = "CONTOUR_MINOR"):
        points = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in points]

        # Add as 3D polyline
        self.msp.add_polyline3d(
            points,
            dxfattribs={'layer': layer}
        )

    def add_contour_label(self, x: float, y: float, z: float, label: str, text_height: float = 1.0):
        x = x * self.scale
        y = y * self.scale
        z = z * self.scale
        text_height = text_height * self.scale

        self.msp.add_text(label, dxfattribs={
            "layer": "CONTOUR_LABELS",
            "height": text_height,
        }).set_placement((x, y, z), align=TextEntityAlignment.MIDDLE_CENTER)

    def add_spline(self, points: List[Tuple[float, float, float]], layer="CONTOUR_MINOR"):
        points = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in points]

        # Add as 3D polyline
        self.msp.add_spline(
            points,
            degree=3,
            dxfattribs={'layer': layer}
        )

    def toggle_layer(self, layer: str, state: bool):
        """Toggle the visibility of a layer"""
        layer_ = self.doc.layers.get(layer)
        layer_.off() if state is False else layer_.on()

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
            dwg_path =  os.path.join(tmpdir, f"{filename}.dwg")
            pdf_path =  os.path.join(tmpdir, f"{filename}.pdf")
            zip_path = os.path.join(tmpdir, f"{filename}.zip")

            self.save_dxf(dxf_path)
            self.save_dwg(dxf_path, dwg_path)
            self.save_pdf(pdf_path, paper_size=paper_size, orientation=orientation)

            # Create a ZIP file containing all three formats
            with zipfile.ZipFile(zip_path, "w") as zipf:
                zipf.write(dxf_path, os.path.basename(dxf_path))
                zipf.write(dwg_path, os.path.basename(dwg_path))
                zipf.write(pdf_path, os.path.basename(pdf_path))

            url = upload_file(zip_path, folder="survey_plans", file_name=filename)
            if url is None:
                raise Exception("Upload failed")
            return url




