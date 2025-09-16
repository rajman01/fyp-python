from enum import Enum
from typing import List, Optional, Union
from datetime import datetime
from pydantic import BaseModel, Field
from bs4 import BeautifulSoup

# ---------- Enums ----------
class PlanType(str, Enum):
    CADASTRAL = "cadastral"
    LAYOUT = "layout"
    TOPOGRAPHIC = "topographic"
    ROUTE = "route"

class PlanOrigin(str, Enum):
    UTM_ZONE_31 = "utm_zone_31"

class BeaconType(str, Enum):
    DOT = "dot"
    CIRCLE = "circle"
    BOX = "box"
    NONE = "none"


# ---------- Supporting models ----------
class CoordinateProps(BaseModel):
    id: str
    northing: Optional[float] = 0.0
    easting: Optional[float] = 0.0
    elevation: Optional[float] = 0.0

class BearingProps(BaseModel):
    degrees: Optional[int] = 0.0
    minutes: Optional[int] = 0.0
    seconds: Optional[float] = 0.0
    decimal: Optional[float] = 0.0

class TraverseLegProps(BaseModel):
    from_: CoordinateProps = Field(alias="from")  # 👈 use alias
    to: CoordinateProps
    bearing: Optional[BearingProps] = None
    observed_angle: Optional[BearingProps] = None
    distance: Optional[float] = None

class ParcelProps(BaseModel):
    name: str
    ids: List[str]
    area: Optional[float] = None  # in square meters
    legs: List[TraverseLegProps] = []

# ---------- Computation models ----------
class ForwardComputationData(BaseModel):
    coordinates: Optional[List[CoordinateProps]] = None
    start: CoordinateProps
    legs: List[TraverseLegProps]
    misclosure_correction: Optional[bool] = False

class TraverseComputationData(BaseModel):
    coordinates: List[CoordinateProps]
    legs: List[TraverseLegProps]
    misclosure_correction: Optional[bool] = False


# ---------- Main Plan Model ----------
class PlanProps(BaseModel):
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    user: Union[str, dict]
    project: Union[str, dict]
    name: str
    type: PlanType = PlanType.CADASTRAL
    font: str = "Arial"
    font_size: int = 12
    coordinates: List[CoordinateProps] = []
    parcels: List[ParcelProps] = []
    title: str = "Untitled Plan"
    address: str = ""
    local_govt: str = ""
    state: str = ""
    plan_number: str = ""
    origin: PlanOrigin = PlanOrigin.UTM_ZONE_31
    scale: float = 1000
    beacon_type: BeaconType = BeaconType.NONE
    personel_name: str = ""
    surveyor_name: str = ""
    forward_computation_data: Optional[ForwardComputationData] = None
    traverse_computation_data: Optional[TraverseComputationData] = None

    def get_drawing_scale(self):
        if not self.scale:
            return 1.0
        return 1000 / self.scale

    def get_extent(self) -> float:
        # get bounding box
        min_x, min_y, max_x, max_y = self.get_bounding_box()
        if min_x is None or min_y is None or max_x is None or max_y is None:
            return 0.0

        width = max_x - min_x
        height = max_y - min_y
        extent = max(width, height)
        return extent

    def get_bounding_box(self) -> Optional[tuple]:
        if len(self.coordinates) == 0:
            return None

        xs = [p.easting for p in self.coordinates]
        ys = [p.northing for p in self.coordinates]

        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        return min_x, min_y, max_x, max_y

    def build_title(self) -> str:
        soup = BeautifulSoup(self.title.upper(), "html.parser")

        if self.address:
            p1 = soup.new_tag("p")
            p1.string = self.address.upper()
            soup.append(p1)

        if self.local_govt:
            p2 = soup.new_tag("p")
            p2.string = self.local_govt.upper()
            soup.append(p2)

        if self.state:
            p3 = soup.new_tag("p")
            p3.string = f"{self.state.upper()} STATE"
            soup.append(p3)

        if self.scale:
            p4 = soup.new_tag("p")
            p4.string = f"SCALE :- 1 : {int(self.scale)}"
            soup.append(p4)

        return soup.prettify()

