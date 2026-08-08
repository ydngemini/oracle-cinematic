"""Python mirror of oracle-app/src/lib/floorplan/protocol.ts.

Kept as plain dataclasses (not Pydantic) so the pipeline has no web-framework
dependency and can run in a worker, a Lambda, or a unit test. floorplan_api.py
re-validates with Pydantic at the HTTP edge — this is the producer side.

UNITS ARE METRES throughout, matching the TS contract.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

SCHEMA_VERSION = 1

RoomType = Literal[
    "bedroom", "bathroom", "kitchen", "living", "dining",
    "hallway", "garage", "utility", "closet", "other",
]

Point2D = tuple[float, float]


@dataclass(slots=True)
class FloorplanLevel:
    id: str
    name: str
    index: int


@dataclass(slots=True)
class FloorplanWall:
    id: str
    start: Point2D
    end: Point2D
    thickness: float = 0.1
    height: float = 2.5
    levelId: Optional[str] = None
    interior: bool = False

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass(slots=True)
class FloorplanRoom:
    id: str
    name: str
    type: RoomType
    polygon: list[Point2D]
    levelId: Optional[str] = None
    boundaryWallIds: list[str] = field(default_factory=list)

    @property
    def area(self) -> float:
        """Shoelace area in square metres."""
        if len(self.polygon) < 3:
            return 0.0
        twice = 0.0
        for i in range(len(self.polygon)):
            x1, y1 = self.polygon[i - 1]
            x2, y2 = self.polygon[i]
            twice += x1 * y2 - x2 * y1
        return abs(twice) / 2.0


@dataclass(slots=True)
class FloorplanOpening:
    id: str
    kind: Literal["door", "window"]
    wallId: Optional[str] = None
    width: float = 0.0
    height: float = 0.0


@dataclass(slots=True)
class Provenance:
    source: Literal["manual", "ai_vision", "parcel_vector", "imported"]
    ai_generated: bool
    model_version: Optional[str] = None
    confidence: Optional[float] = None
    notes: Optional[str] = None


@dataclass(slots=True)
class FloorplanDocument:
    provenance: Provenance
    schema_version: int = SCHEMA_VERSION
    units: Literal["metric"] = "metric"
    levels: list[FloorplanLevel] = field(default_factory=list)
    walls: list[FloorplanWall] = field(default_factory=list)
    rooms: list[FloorplanRoom] = field(default_factory=list)
    openings: list[FloorplanOpening] = field(default_factory=list)

    @property
    def total_area_m2(self) -> float:
        return sum(room.area for room in self.rooms)

    @property
    def total_sqft(self) -> float:
        return self.total_area_m2 * 10.763910416709722

    def to_json(self) -> dict[str, Any]:
        """Serialise to the exact wire shape floorplan_api accepts."""
        payload = asdict(self)
        # Tuples become lists so json.dumps matches the TS `[number, number]`.
        for wall in payload["walls"]:
            wall["start"] = list(wall["start"])
            wall["end"] = list(wall["end"])
        for room in payload["rooms"]:
            room["polygon"] = [list(p) for p in room["polygon"]]
        return payload
