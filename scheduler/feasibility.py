"""
feasibility.py — Pre-compute valid charging station combos and assign them.

Key change from original:
  assign_combo() is now load-aware.  Instead of always picking the first
  (fewest-stop) combo, it picks the valid combo whose stations have the
  lowest total projected queue at the time the bus would arrive.

This distributes buses across multiple valid combos (e.g. BK gets spread
across (A,C), (B,C), (B,D)) rather than piling them all onto one pair of
stations, which was the root cause of the cascade wait-time explosion.
"""

from collections import defaultdict
from itertools import combinations
from typing import List, Dict, Tuple, Optional


# ---------------------------------------------------------------------------
# Route order helpers
# ---------------------------------------------------------------------------

def get_stop_order(direction: str, world) -> List[str]:
    """
    Return the ordered list of charging station IDs a bus passes through.
    BK (Bengaluru→Kochi): stations in forward order, e.g. ['A','B','C','D']
    KB (Kochi→Bengaluru): stations in reverse order, e.g. ['D','C','B','A']
    """
    # Stations are stored in BK order; world.station_order is that list.
    fwd = world.station_order  # e.g. ['A','B','C','D']
    if direction == "BK":
        return list(fwd)
    else:
        return list(reversed(fwd))



def build_distance_map_simple(direction: str, world) -> Dict[str, float]:
    """
    Simplified distance map builder that uses world.station_distances directly.
    world.station_distances: Dict[str, float] — cumulative km from Bengaluru
                             to each charging station (BK direction).
    world.total_route_km: total Bengaluru-to-Kochi distance.
    """
    if direction == "BK":
        return dict(world.station_distances)
    else:
        total = world.total_route_km
        return {sid: total - bk_dist
                for sid, bk_dist in world.station_distances.items()}


# ---------------------------------------------------------------------------
# Validity check
# ---------------------------------------------------------------------------

def is_valid_combo(combo: Tuple[str, ...], direction: str, world,
                   dist_map: Dict[str, float]) -> bool:
    """
    A combo is valid iff:
      - No gap between consecutive charge points exceeds battery_range_km.
      - A "gap" is: distance from origin to first station,
                    distance between each consecutive pair of stations,
                    distance from last station to destination.
    """
    battery = world.battery_range_km
    total = world.total_route_km
    stops = list(combo)

    # Gaps to check:
    # [origin → stop[0], stop[0]→stop[1], ..., stop[-1]→destination]
    points = [0.0] + [dist_map[s] for s in stops] + [total]

    for i in range(len(points) - 1):
        gap = points[i + 1] - points[i]
        if gap > battery:
            return False
    return True


def get_valid_combos(direction: str, world, stations: Dict) -> List[List[str]]:
    """
    Return all valid combos, sorted by length (fewest stops first),
    then by total distance covered (to prefer evenly-spread combos).
    """
    dist_map = build_distance_map_simple(direction, world)
    stop_order = get_stop_order(direction, world)
    all_combos = []

    for size in range(1, len(stop_order) + 1):
        for combo in combinations(stop_order, size):
            # Enforce route order within the combo
            ordered = tuple(s for s in stop_order if s in combo)
            if len(ordered) != size:
                continue
            if is_valid_combo(ordered, direction, world, dist_map):
                all_combos.append(list(ordered))

    # Sort: fewest stops first; ties broken by most even spacing (prefer spread)
    def combo_key(c):
        total = world.total_route_km
        points = [0.0] + [dist_map[s] for s in c] + [total]
        max_gap = max(points[i+1] - points[i] for i in range(len(points)-1))
        return (len(c), max_gap)

    return sorted(all_combos, key=combo_key)


def assign_all_combos(buses, world, stations: Dict, chargers: Dict) -> None:
    """
    Assign a charging combo to every bus in departure-time order.

    Primary sort key: lowest total projected load across the combo's stations.
    Tiebreak: fewest times this combo has already been assigned (round-robin
    among equal-load combos).  Without the tiebreak, Python's min() always
    returns the first equal item, so all buses collapse onto one combo even
    when alternatives exist.
    """
    sorted_buses = sorted(buses, key=lambda b: b.departure_min)
    combo_count: Dict[tuple, int] = defaultdict(int)

    for bus in sorted_buses:
        valid = get_valid_combos(bus.direction, world, stations)

        def combo_key(combo: List[str]) -> tuple:
            load = sum(
                min(c.free_at_min for c in chargers[sid]) if sid in chargers else 0.0
                for sid in combo
            )
            return (load, combo_count[tuple(combo)])

        chosen = min(valid, key=combo_key) if valid else []
        bus.assigned_combo = chosen
        combo_count[tuple(chosen)] += 1

        # Tentatively increment the earliest-free charger so subsequent buses
        # see this station as more loaded.
        for sid in bus.assigned_combo:
            if sid in chargers:
                earliest = min(chargers[sid], key=lambda c: c.free_at_min)
                earliest.free_at_min += world.charge_time_min

    # Reset after assignment — actual scheduling starts from 0
    for charger_list in chargers.values():
        for c in charger_list:
            c.free_at_min = 0