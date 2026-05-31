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


def build_distance_map(direction: str, world) -> Dict[str, float]:
    """
    For a given direction, return {station_id: cumulative_km_from_origin}.
    Origin is Bengaluru (BK) or Kochi (KB).
    """
    # world.segments is ordered BK: [(Bengaluru,A,100),(A,B,120),...]
    # world.terminal_to_first_station_km is the distance from terminal to
    # the first station in BK order.

    stop_order = get_stop_order(direction, world)
    fwd_cumulative = {}  # station_id -> km from Bengaluru
    running = 0.0
    for seg in world.segments:
        # Each segment has .from_id, .to_id, .distance_km
        # Segments are stored in BK order
        if hasattr(seg, 'to_id') and seg.to_id in world.station_order:
            running += seg.distance_km
            fwd_cumulative[seg.to_id] = running

    if direction == "BK":
        return fwd_cumulative
    else:
        # For KB, distance from Kochi = (total_route_km - BK_cumulative_from_Bengaluru)
        # But we also need to subtract the terminal legs.
        # Simpler: compute from the last station backward.
        # Total distance from Kochi to Bengaluru = sum of all segments
        total = sum(s.distance_km for s in world.segments)
        # BK: Bengaluru->(terminal distance)->A->B->C->D->(terminal distance)->Kochi
        # We want: from Kochi, D is first, then C, B, A
        kb_dist = {}
        for sid, bk_dist in fwd_cumulative.items():
            # distance from Kochi = total_bk_route - bk_cumulative_to_sid
            # But bk_cumulative is to the station; we need from Kochi terminal
            # Kochi terminal is at the end, so kochi_to_station = total - fwd_cumulative[sid]
            # minus the Kochi terminal leg (last segment that is not a station-to-station)
            pass

        # Cleaner approach: build by walking segments in reverse
        kb_dist = {}
        running_kb = 0.0
        rev_segments = list(reversed(world.segments))
        for seg in rev_segments:
            running_kb += seg.distance_km
            # The "from" side in KB is seg.to_id (BK order) and "to" side is seg.from_id
            if hasattr(seg, 'from_id') and seg.from_id in world.station_order:
                kb_dist[seg.from_id] = running_kb - seg.distance_km  # distance already passed

        # Re-compute cleanly: distance from KB origin (Kochi) to each station
        kb_dist_clean = {}
        running_kb = 0.0
        for seg in rev_segments:
            # In KB travel order: we move Kochi→D→C→B→A→Bengaluru
            # Segments reversed: (D,Kochi,100), (C,D,120), (B,C,100), (A,B,120), (Bengaluru,A,100)
            # seg.to_id in BK = seg.from_id in KB travel
            if seg.to_id in world.station_order or seg.from_id in world.station_order:
                running_kb += seg.distance_km
                # The station we just reached in KB direction is seg.from_id (BK notation)
                # because reversed BK segment (A→B) means in KB we go B→A
                if seg.from_id in world.station_order:
                    kb_dist_clean[seg.from_id] = running_kb

        return kb_dist_clean


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


# ---------------------------------------------------------------------------
# Load-aware combo assignment  ← KEY CHANGE
# ---------------------------------------------------------------------------

def assign_combo(bus, valid_combos: List[List[str]], chargers: Dict) -> List[str]:
    """
    Pick the valid combo that minimises total projected queue load.

    'Load' for a combo = sum of charger.free_at_min across all stations
    in that combo.  Lower value → less contention expected.

    This distributes buses across multiple valid combos rather than
    sending every bus to the same pair of stations.

    Falls back to the first (fewest-stop) combo if chargers dict is empty
    or all combos have equal load.
    """
    if not valid_combos:
        return []

    def combo_load(combo: List[str]) -> float:
        return sum(
            chargers[sid].free_at_min if sid in chargers else 0.0
            for sid in combo
        )

    return min(valid_combos, key=combo_load)


def assign_all_combos(buses, world, stations: Dict, chargers: Dict) -> None:
    """
    Assign a charging combo to every bus in departure-time order.

    Processing in departure order means early buses claim stations first,
    and later buses see those stations as more loaded, naturally spreading
    traffic across alternative combos.

    After assignment we reset charger.free_at_min to 0 so the actual
    scheduling loop starts clean.
    """
    sorted_buses = sorted(buses, key=lambda b: b.departure_min)

    for bus in sorted_buses:
        valid = get_valid_combos(bus.direction, world, stations)
        bus.assigned_combo = assign_combo(bus, valid, chargers)

        # Tentatively mark these stations as slightly more loaded so the next
        # bus sees them as less attractive. Each bus adds one charge slot.
        for sid in bus.assigned_combo:
            if sid in chargers:
                chargers[sid].free_at_min += world.charge_time_min

    # Reset after assignment — actual scheduling starts from 0
    for c in chargers.values():
        c.free_at_min = 0