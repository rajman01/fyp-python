"""Models for imported CAD drawings (Task 11 -- legacy DWG/DXF import).

These describe the *neutral intermediate* the extractor produces: what a
drawing contains, expressed in survey terms (rings, points, labels) rather
than CAD terms (entities, blocks, splines). Plan-type interpreters map this
onto a plan payload; nothing downstream needs to know about ezdxf.

The driving use case is restoring old drawings, where the surveyor has a DWG
and nothing else, so the extractor is deliberately generous about what it will
accept and reports what it found rather than guessing.
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

#: ``$INSUNITS`` header values mapped to metres. Anything absent from this map
#: (including 0 = "unitless") is treated as metres, which is what survey
#: drawings on a projected grid almost always are -- but the value is reported
#: so the user can confirm or override it rather than being silently trusted.
INSUNITS_TO_METRES: Dict[int, float] = {
    1: 0.0254,       # inches
    2: 0.3048,       # feet
    4: 0.001,        # millimetres
    5: 0.01,         # centimetres
    6: 1.0,          # metres
    7: 1000.0,       # kilometres
    8: 0.0000254,    # microinches
    9: 0.0000000254,  # mils
    10: 0.9144,      # yards
    21: 1200.0 / 3937.0,  # US survey feet
}

INSUNITS_NAMES: Dict[int, str] = {
    0: "unitless",
    1: "inches",
    2: "feet",
    4: "millimetres",
    5: "centimetres",
    6: "metres",
    7: "kilometres",
    8: "microinches",
    9: "mils",
    10: "yards",
    21: "US survey feet",
}


class RingSource(str, Enum):
    """How a closed ring was recovered from the drawing."""
    #: A single entity that was already closed (LWPOLYLINE, POLYLINE, SPLINE).
    POLYLINE = "polyline"
    #: Rebuilt by chaining loose segments end to end within a gap tolerance --
    #: how a boundary drawn as separate lines is recovered.
    CHAINED = "chained"


class CadVertex(BaseModel):
    easting: float
    northing: float
    elevation: float = 0.0


class CadLabel(BaseModel):
    """A TEXT/MTEXT entity, kept with its position so it can be matched to the
    feature it annotates."""
    text: str
    easting: float
    northing: float
    height: float = 0.0
    layer: str = ""


class CadStation(BaseModel):
    """One row of a coordinate register recovered from a drawing."""
    id: str
    easting: float
    northing: float
    elevation: float = 0.0
    #: True when the id was invented (``PB1``, ``PB2`` ...) because the drawing
    #: carried no station name for that corner.
    generated: bool = False


class CadRing(BaseModel):
    """A closed ring that could be a boundary."""
    id: str
    layer: str
    source: RingSource
    vertices: List[CadVertex]
    area: float
    perimeter: float
    centroid_easting: float
    centroid_northing: float
    #: Set when the ring was chained from loose segments that did not quite
    #: meet; the size of the largest gap that had to be bridged, in metres.
    gap_closed: Optional[float] = None
    #: The ring as a coordinate register, ordered and named. Computed here so
    #: there is one implementation of the rule -- a client rebuilding it from
    #: the vertices would be a second one to keep in step.
    coordinates: List[CadStation] = Field(default_factory=list)

    @property
    def vertex_count(self) -> int:
        return len(self.vertices)


class CadPoint(BaseModel):
    """A point feature: a POINT entity or a block insert (a beacon symbol)."""
    easting: float
    northing: float
    elevation: float = 0.0
    layer: str = ""
    block: str = ""
    #: Nearby text taken to be this point's station id, when one was found.
    label: Optional[str] = None


class CadLayer(BaseModel):
    """What a layer holds, so the user can tell which one is the boundary."""
    name: str
    entity_count: int = 0
    ring_count: int = 0
    point_count: int = 0
    label_count: int = 0
    entity_types: List[str] = Field(default_factory=list)


class CadInspection(BaseModel):
    """Everything the extractor found in one drawing.

    Coordinates are converted to metres using ``units_factor``. The detected
    units are reported alongside so the caller can present them for
    confirmation and rescale if the user overrides them -- a drawing in feet
    that is read as metres produces a plan that is confidently wrong rather
    than visibly broken.
    """
    file_name: str
    file_format: str  # "dwg" | "dxf"
    dxf_version: str = ""

    units: str = "metres"
    units_code: int = 0
    units_factor: float = 1.0

    #: Extents in metres, after unit conversion.
    min_easting: Optional[float] = None
    min_northing: Optional[float] = None
    max_easting: Optional[float] = None
    max_northing: Optional[float] = None

    layers: List[CadLayer] = Field(default_factory=list)
    rings: List[CadRing] = Field(default_factory=list)
    points: List[CadPoint] = Field(default_factory=list)
    labels: List[CadLabel] = Field(default_factory=list)

    #: Non-fatal problems worth showing the user (search timed out, drawing
    #: truncated, no units in the header, ...).
    warnings: List[str] = Field(default_factory=list)
