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

    Cost: timing-aware estimated total wait across all stops in the combo.
    For each stop:
        arrival  = departure_min + cumulative travel time (accounting for
                   charge durations at prior stops in the same combo)
        est_wait = max(0, dir_free[direction][sid] - arrival)

    dir_free tracks tentative charger availability *per direction* so that
    BK buses aren't misled by KB buses booking a slot they'd arrive at
    much later (and vice versa).  Cross-direction competition is handled by
    run_schedule which processes events in true arrival-time order.

    Primary sort key: fewest stops (each extra stop = 25 min of charging).
    Secondary: lowest estimated wait. Tertiary: round-robin tiebreak.
    """
    sorted_buses = sorted(buses, key=lambda b: b.departure_min)
    combo_count: Dict[tuple, int] = defaultdict(int)

    # Per-direction tentative station free times (station_id -> float)
    dir_free: Dict[str, Dict[str, float]] = {
        "BK": {sid: 0.0 for sid in chargers},
        "KB": {sid: 0.0 for sid in chargers},
    }

    for bus in sorted_buses:
        valid = get_valid_combos(bus.direction, world, stations)
        dist_map = build_distance_map_simple(bus.direction, world)
        free = dir_free[bus.direction]

        def combo_key(combo: List[str]) -> tuple:
            total_wait = 0.0
            current_time = bus.departure_min
            for i, sid in enumerate(combo):
                seg_km = dist_map[sid] if i == 0 else dist_map[sid] - dist_map[combo[i - 1]]
                arrival = current_time + seg_km / world.speed_kmh * 60
                wait = max(0.0, free[sid] - arrival) if sid in free else 0.0
                total_wait += wait
                current_time = arrival + wait + world.charge_time_min
            return (len(combo), total_wait, combo_count[tuple(combo)])

        chosen = min(valid, key=combo_key) if valid else []
        bus.assigned_combo = chosen
        combo_count[tuple(chosen)] += 1

        # Commit predicted slot times into this direction's free map.
        current_time = bus.departure_min
        for i, sid in enumerate(chosen):
            seg_km = dist_map[sid] if i == 0 else dist_map[sid] - dist_map[chosen[i - 1]]
            arrival = current_time + seg_km / world.speed_kmh * 60
            slot_time = max(free.get(sid, 0.0), arrival)
            free[sid] = slot_time + world.charge_time_min
            current_time = free[sid]

    # Reset actual charger objects — run_schedule starts from scratch.
    for charger_list in chargers.values():
        for c in charger_list:
            c.free_at_min = 0