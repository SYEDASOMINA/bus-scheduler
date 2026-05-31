from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class Segment:
    from_stop: str
    to_stop: str
    distance_km: float

@dataclass
class World:
    speed_kmh: float
    battery_range_km: float
    charge_time_min: float
    segments: List[Segment]
    total_route_km: float = 0.0
    station_order: List[str] = field(default_factory=list)
    station_distances: Dict[str, float] = field(default_factory=dict)

@dataclass
class StationConfig:
    id: str
    charger_count: int

@dataclass
class Weights:
    individual: float
    operator: float
    overall: float

@dataclass
class ChargeStop:
    station_id: str
    arrival_min: float
    wait_min: float
    charge_start_min: float
    charge_end_min: float

@dataclass
class Bus:
    id: str
    operator: str
    direction: str
    departure_min: float
    charge_stations: List[str] = field(default_factory=list)
    charge_stops: List[ChargeStop] = field(default_factory=list)
    current_time_min: float = 0.0
    assigned_combo: List[str] = field(default_factory=list)
    final_arrival_min: Optional[float] = None
    _range_remaining_km: float = 0.0

    @property
    def earliest_available(self):
        return self.current_time_min

@dataclass
class Charger:
    station_id: str
    charger_index: int
    free_at_min: float = 0.0