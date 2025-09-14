from enum import Enum
from typing import List, Optional, Union
from datetime import datetime
from pydantic import BaseModel

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
class ParcelProps(BaseModel):
    name: str
    ids: List[str]

class CoordinateProps(BaseModel):
    x: float
    y: float
    z: Optional[float] = None

class TraverseLegProps(BaseModel):
    from_: str  # `from` is reserved in Python
    to: str
    bearing: Optional[float] = None
    observed_angle: Optional[float] = None
    distance: float


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
    scale: float = 1
    beacon_type: BeaconType = BeaconType.NONE
    personel_name: str = ""
    surveyor_name: str = ""
    forward_computation_data: Optional[ForwardComputationData] = None
    traverse_computation_data: Optional[TraverseComputationData] = None
