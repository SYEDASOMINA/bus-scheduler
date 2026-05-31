"""
engine.py — Event-driven bus charging scheduler.

Key changes from original greedy bus-by-bus loop:
─────────────────────────────────────────────────
1. Event-driven priority queue.
   Events are (arrival_time, departure_tiebreak, bus_id, station_id,
               combo_idx).  We process events in arrival-time order,
   not bus-ID order.  This means the scoring function actually controls
   who charges first when two buses arrive around the same time.

2. Intra-station contention resolved by score, not insertion order.
   When multiple buses are waiting for the same charger, we re-score
   all waiters and serve the lowest-scoring bus first.  This is how
   the weight system visibly changes the schedule (operator weight,
   individual wait penalty, network delay).

3. Combo assignment is now load-aware (see feasibility.py).
   Buses are distributed across (A,C), (B,C), (B,D) etc. instead of
   all routing to the same pair of stations.

Public API (unchanged from original):
  load_scenario(path) → buses, stations, world, weights
  assign_all_combos(buses, world, stations, chargers)
  run_schedule(buses, chargers, world, weights)
"""

import json
import heapq
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

from .models import Bus, ChargeStop, Charger, World, StationConfig, Weights, Segment
from .feasibility import (
    assign_all_combos,
    get_stop_order,
    build_distance_map_simple,
)
from .rules import score_bus, build_context


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------

def _parse_departure(dep_str: str) -> float:
    h, m = dep_str.split(":")
    return int(h) * 60 + int(m)


def load_scenario(path: str):
    """
    Read a JSON scenario file and return (buses, stations, chargers, world, weights).
    """
    with open(path) as f:
        data = json.load(f)

    world_data = data["world"]
    raw_segments = world_data["segments"]
    segments = [
        Segment(from_stop=s["from"], to_stop=s["to"], distance_km=s["distance_km"])
        for s in raw_segments
    ]

    station_ids = [s["id"] for s in data["stations"]]
    station_distances: dict = {}
    cumulative = 0.0
    for seg in segments:
        cumulative += seg.distance_km
        if seg.to_stop in station_ids:
            station_distances[seg.to_stop] = cumulative

    world = World(
        segments=segments,
        speed_kmh=world_data.get("speed_kmh", 60),
        charge_time_min=world_data.get("charge_time_min", 25),
        battery_range_km=world_data.get("battery_km", 240),
        total_route_km=sum(s["distance_km"] for s in raw_segments),
        station_order=station_ids,
        station_distances=station_distances,
    )

    stations = {s["id"]: StationConfig(**s) for s in data["stations"]}
    chargers = {
        sid: [Charger(station_id=sid, charger_index=i, free_at_min=0.0)
              for i in range(stations[sid].charger_count)]
        for sid in stations
    }
    weights = Weights(**data["weights"])
    buses = []
    for b in data["buses"]:
        b = dict(b)
        b["departure_min"] = _parse_departure(b.pop("departure"))
        buses.append(Bus(**b))

    return buses, stations, chargers, world, weights


# ---------------------------------------------------------------------------
# Travel helpers
# ---------------------------------------------------------------------------

def _dist_between(from_id: str, to_id: str, direction: str, world: World) -> float:
    """
    Distance in km between two consecutive stops for a given direction.
    Both must be in get_stop_order() or be the origin/destination sentinels.
    """
    dist_map = build_distance_map_simple(direction, world)
    return abs(dist_map[to_id] - dist_map[from_id])


def _travel_min(from_id: str, to_id: str, direction: str, world: World) -> float:
    return _dist_between(from_id, to_id, direction, world) / world.speed_kmh * 60


# ---------------------------------------------------------------------------
# Event-driven scheduler
# ---------------------------------------------------------------------------

def run_schedule(buses: List[Bus], chargers: Dict[str, List[Charger]],
                 world: World, weights: Weights) -> None:
    """
    Event-driven simulation.  Modifies buses in place (charge_stops,
    current_time_min, final_arrival_min).

    Algorithm
    ─────────
    Each event = (arrival_time, departure_tiebreak, bus_id, station_id,
                  combo_idx).

    On processing an event:
      1. The bus has arrived at station_id.
      2. If the charger is busy, the bus joins an explicit waiting list
         for that station rather than being blocked inline.
      3. When a charger becomes free, it picks the best waiter by score
         (lowest wins) and starts charging.
      4. After charging, the bus schedules its next stop (combo_idx+1).
      5. When a bus finishes its last combo stop, final_arrival_min is set.

    This correctly implements: "different weights → different schedule"
    because the tie-breaking at step 3 uses score_bus().
    """
    bus_map: Dict[str, Bus] = {b.id: b for b in buses}

    # Per-direction helpers (cached)
    stop_orders: Dict[str, List[str]] = {}
    dist_maps: Dict[str, Dict[str, float]] = {}
    for direction in ("BK", "KB"):
        stop_orders[direction] = get_stop_order(direction, world)
        dist_maps[direction] = build_distance_map_simple(direction, world)

    # Reset all bus state
    for bus in buses:
        bus.charge_stops = []
        bus.current_time_min = bus.departure_min
        bus.final_arrival_min = None
        bus._range_remaining_km = world.battery_range_km

    # Reset chargers
    for charger_list in chargers.values():
        for c in charger_list:
            c.free_at_min = 0

    # Per-station queue of (score, arrival_time, bus_id, combo_idx)
    # Buses sitting here have arrived but the charger was busy.
    waiting: Dict[str, List] = {sid: [] for sid in chargers}

    # ── Priority queue ──────────────────────────────────────────────────────
    # (arrival_time, departure_min_tiebreak, bus_id, station_id, combo_idx)
    events: List[Tuple] = []

    def push_arrival(bus: Bus, station_id: str, arrival_time: float,
                     combo_idx: int) -> None:
        heapq.heappush(events,
                       (arrival_time, bus.departure_min, bus.id,
                        station_id, combo_idx))

    # Seed events: each bus heads to its first assigned combo stop
    for bus in buses:
        if not bus.assigned_combo:
            # No charge stops needed (edge case: short route)
            dist_map = dist_maps[bus.direction]
            total = world.total_route_km
            bus.final_arrival_min = (bus.departure_min
                                     + total / world.speed_kmh * 60)
            continue

        first_station = bus.assigned_combo[0]
        dist_to_first = dist_maps[bus.direction][first_station]
        arrival = bus.departure_min + dist_to_first / world.speed_kmh * 60
        bus._range_remaining_km -= dist_to_first
        push_arrival(bus, first_station, arrival, combo_idx=0)

    # ── Main loop ────────────────────────────────────────────────────────────
    def start_charging(bus: Bus, station_id: str, arrival_time: float,
                       combo_idx: int) -> None:
        """Commit bus to charge at station_id starting now (charger is free)."""
        charger = min(chargers[station_id], key=lambda c: c.free_at_min)
        slot_time = max(charger.free_at_min, arrival_time)
        wait_min = max(0.0, slot_time - arrival_time)
        depart_time = slot_time + world.charge_time_min

        bus.charge_stops.append(ChargeStop(
            station_id=station_id,
            arrival_min=arrival_time,
            charge_start_min=slot_time,
            charge_end_min=depart_time,
            wait_min=wait_min,
        ))
        charger.free_at_min = depart_time
        bus.current_time_min = depart_time
        bus._range_remaining_km = world.battery_range_km  # full charge

        # Schedule next stop, or compute final arrival
        next_idx = combo_idx + 1
        if next_idx < len(bus.assigned_combo):
            next_station = bus.assigned_combo[next_idx]
            dist = _dist_between(station_id, next_station,
                                 bus.direction, world)
            bus._range_remaining_km -= dist
            next_arrival = depart_time + dist / world.speed_kmh * 60
            push_arrival(bus, next_station, next_arrival, next_idx)
        else:
            # No more charging stops — compute final arrival at destination
            dist_map = dist_maps[bus.direction]
            last_dist = dist_map[station_id]
            dist_to_dest = world.total_route_km - last_dist
            bus.final_arrival_min = (depart_time
                                     + dist_to_dest / world.speed_kmh * 60)

    def drain_waiting(station_id: str) -> None:
        """
        After a charger becomes free, pick the best waiter by score and
        start charging.  Loops to fill all currently free charger slots
        (supports charger_count > 1).
        """
        context = build_context(list(bus_map.values()), station_id)
        while waiting[station_id]:
            free_charger = min(chargers[station_id], key=lambda c: c.free_at_min)
            if free_charger.free_at_min > _current_time:
                return

            scored = []
            for (arr_time, bid, cidx) in waiting[station_id]:
                b = bus_map[bid]
                effective_slot = max(free_charger.free_at_min, arr_time)
                sc = score_bus(b, effective_slot, context, weights)
                scored.append((sc, arr_time, bid, cidx))

            scored.sort(key=lambda x: (x[0], x[1]))
            best = scored.pop(0)
            waiting[station_id] = [(a, bi, ci) for (_, a, bi, ci) in scored]
            _, arr_time, bid, cidx = best
            start_charging(bus_map[bid], station_id, arr_time, cidx)

    _current_time = 0.0

    while events:
        arrival_time, dep_tiebreak, bus_id, station_id, combo_idx = \
            heapq.heappop(events)

        _current_time = arrival_time
        bus = bus_map[bus_id]

        def _min_free(sid: str) -> float:
            return min(c.free_at_min for c in chargers[sid])

        if _min_free(station_id) <= arrival_time:
            # At least one charger is free — drain waiters first, then take a slot
            drain_waiting(station_id)
            if _min_free(station_id) <= arrival_time:
                start_charging(bus, station_id, arrival_time, combo_idx)
            else:
                waiting[station_id].append((arrival_time, bus_id, combo_idx))
        else:
            waiting[station_id].append((arrival_time, bus_id, combo_idx))

        # Try to drain other stations that may have freed up a charger slot.
        for sid, wq in waiting.items():
            if wq and _min_free(sid) <= _current_time:
                drain_waiting(sid)

    # ── Flush any remaining waiters ─────────────────────────────────────────
    # Some waiters may still be in queues if no subsequent event triggered
    # drain_waiting for their station.  Process them now in score order.
    max_iterations = len(buses) * 10
    iteration = 0
    pending = True
    while pending and iteration < max_iterations:
        pending = False
        iteration += 1
        for station_id, wq in waiting.items():
            if not wq:
                continue
            pending = True
            charger = min(chargers[station_id], key=lambda c: c.free_at_min)
            context = build_context(list(bus_map.values()), station_id)
            scored = []
            for (arr_time, bid, cidx) in wq:
                b = bus_map[bid]
                effective_slot = max(charger.free_at_min, arr_time)
                sc = score_bus(b, effective_slot, context, weights)
                scored.append((sc, arr_time, bid, cidx))
            scored.sort(key=lambda x: (x[0], x[1]))
            best = scored.pop(0)
            waiting[station_id] = [(a, bi, ci) for (_, a, bi, ci) in scored]
            _, arr_time, bid, cidx = best
            start_charging(bus_map[bid], station_id, arr_time, cidx)

    # ── Fill in final_arrival_min for any bus not yet complete ───────────────
    for bus in buses:
        if bus.final_arrival_min is not None:
            continue
        dist_map = dist_maps[bus.direction]
        if bus.charge_stops:
            last = bus.charge_stops[-1]
            last_dist = dist_map[last.station_id]
            dist_remaining = world.total_route_km - last_dist
            bus.final_arrival_min = (last.charge_end_min
                                     + dist_remaining / world.speed_kmh * 60)
        else:
            bus.final_arrival_min = (bus.departure_min
                                     + world.total_route_km / world.speed_kmh * 60)