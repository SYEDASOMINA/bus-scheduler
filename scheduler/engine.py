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

def load_scenario(path: str):
    """
    Read a JSON scenario file and return (buses, stations, world, weights).
    Keeps exactly the same return signature as the original.
    """
    with open(path) as f:
        data = json.load(f)

    segments = [Segment(**s) for s in data["segments"]]

    # Build a distance map: {station_id: km_from_bengaluru}
    station_distances = {}
    cumulative = 0.0
    for seg in segments:
        cumulative += seg.distance_km
        if seg.to_id in [s["id"] for s in data["stations"]]:
            station_distances[seg.to_id] = cumulative

    world = World(
        segments=segments,
        speed_kmh=data.get("speed_kmh", 60),
        charge_time_min=data.get("charge_time_min", 25),
        battery_range_km=data.get("battery_range_km", 240),
        total_route_km=sum(s["distance_km"] for s in data["segments"]),
        station_order=[s["id"] for s in data["stations"]],
        station_distances=station_distances,
    )

    stations = {s["id"]: StationConfig(**s) for s in data["stations"]}
    chargers = {sid: Charger(station_id=sid, free_at_min=0)
                for sid in stations}
    weights = Weights(**data["weights"])
    buses = [Bus(**b) for b in data["buses"]]

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

def run_schedule(buses: List[Bus], chargers: Dict[str, Charger],
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
    for c in chargers.values():
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
        charger = chargers[station_id]
        slot_time = max(charger.free_at_min, arrival_time)
        wait_min = max(0.0, slot_time - arrival_time)
        depart_time = slot_time + world.charge_time_min

        bus.charge_stops.append(ChargeStop(
            station_id=station_id,
            arrival_min=arrival_time,
            charge_start_min=slot_time,
            depart_min=depart_time,
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
        start charging.  Score is recomputed at this moment so that
        operator fairness and accumulated waits are current.
        """
        wq = waiting[station_id]
        if not wq:
            return
        charger = chargers[station_id]
        if charger.free_at_min > _current_time:
            # Charger still busy — don't drain yet (will be triggered on
            # next event that finishes charging here, or by the event loop).
            return

        # Recompute scores for all waiters with current context
        context = build_context(list(bus_map.values()), station_id)
        scored = []
        for (arr_time, bid, cidx) in wq:
            b = bus_map[bid]
            effective_slot = max(charger.free_at_min, arr_time)
            sc = score_bus(b, effective_slot, context, weights)
            scored.append((sc, arr_time, bid, cidx))

        scored.sort(key=lambda x: (x[0], x[1]))  # score first, FCFS tiebreak
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
        charger = chargers[station_id]

        if charger.free_at_min <= arrival_time:
            # Charger is free — start immediately (scores all waiters first)
            # First drain any existing waiters for this station
            drain_waiting(station_id)
            # If no waiters, or after draining the charger is still free, go
            if charger.free_at_min <= arrival_time:
                start_charging(bus, station_id, arrival_time, combo_idx)
            else:
                # A waiter just grabbed the slot; this bus must wait
                waiting[station_id].append((arrival_time, bus_id, combo_idx))
        else:
            # Charger busy — join the waiting list
            waiting[station_id].append((arrival_time, bus_id, combo_idx))

        # After processing this event, try to drain stations whose charger
        # has just become free (i.e. whose free_at_min <= current time).
        for sid, wq in waiting.items():
            if wq and chargers[sid].free_at_min <= _current_time:
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
            charger = chargers[station_id]
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
            bus.final_arrival_min = (last.depart_min
                                     + dist_remaining / world.speed_kmh * 60)
        else:
            bus.final_arrival_min = (bus.departure_min
                                     + world.total_route_km / world.speed_kmh * 60)