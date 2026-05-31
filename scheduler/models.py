from dataclasses import dataclass, field
from typing import List

@dataclass
class Segment:
    from_stop: str
    to_stop: str
    distance_km: float

@dataclass
class World:
    speed_kmh: float
    battery_km: float
    charge_time_min: float
    segments: List[Segment]

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

    @property
    def earliest_available(self):
        return self.current_time_min

@dataclass
class Charger:
    station_id: str
    charger_index: int
    free_at_min: float = 0.0