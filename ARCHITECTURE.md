# Architecture

## Scheduler approach

### Event-driven priority queue

The engine uses a min-heap of arrival events, processed in real time order. Each event is `(arrival_time, departure_tiebreak, bus_id, station_id, combo_idx)`. When a bus arrives at a station it either starts charging immediately (if a charger is free) or joins an explicit per-station waiting list.

When a charger slot opens, the engine re-scores every bus in that station's waiting list and serves the lowest-scoring bus. Score is computed fresh at that moment so accumulated wait history and operator equity are always current.

**Why this over a greedy bus-by-bus loop:** A greedy loop processes one bus at a time in departure order, which means bus 20 "sees" no queue contention while being assigned—its wait is computed in a vacuum. An event-driven queue processes buses in arrival order, so contention is resolved at the correct moment. This is the only way the weight system can visibly change who charges first.

### Two-phase structure

1. **`assign_all_combos`** — pre-assigns which charging stations each bus will use. Processes buses in departure order; uses a load-aware heuristic with a round-robin tiebreak to spread buses across equally-loaded combos (e.g., BK buses spread across `[A,C]`, `[B,C]`, `[B,D]` instead of all piling onto one pair).

2. **`run_schedule`** — event-driven simulation over the assigned combos. Computes exact charge times, wait times, and final arrival for every bus.

### Rule registry (`rules.py`)

Every scoring rule is a plain function decorated with `@rule("weight_key")`:

```python
@rule("individual")
def wait_penalty(bus, slot_time, context):
    return max(0.0, slot_time - bus.earliest_available)
```

`score_bus()` iterates `RULES` and sums `weight × rule_score`. To add a new rule: write the function, add the decorator. Nothing else changes. The engine never needs to know how many rules exist.

---

## Data structure design

Scenario files own all configuration. Nothing is hardcoded.

```json
{
  "world": {
    "speed_kmh": 60,
    "battery_km": 240,
    "charge_time_min": 25,
    "segments": [
      {"from": "Bengaluru", "to": "A", "distance_km": 100},
      ...
    ]
  },
  "stations": [
    {"id": "A", "charger_count": 1},
    ...
  ],
  "weights": {"individual": 1.0, "operator": 1.0, "overall": 1.0},
  "buses": [
    {"id": "bus-BK-01", "operator": "kpn", "direction": "BK", "departure": "19:00"},
    ...
  ]
}
```

Key design decisions:
- **`segments` array** — the route is fully described by ordered (from, to, distance_km) pairs. Adding or removing stations, changing distances, or extending the route is a data change only.
- **`charger_count` per station** — the engine creates N `Charger` objects per station and always picks the earliest-free one. Doubling chargers at a station is a single JSON integer change.
- **`weights` object** — one number per soft rule, in one place. Changing a weight means editing one value in the JSON.
- **`direction` string** — currently `"BK"` / `"KB"`, but the engine derives route order from segments, not from hardcoded direction semantics. A new direction (e.g. a cross-route) only needs its own segments list.

---

## Anticipated changes and how the design handles them

| Change | How handled | Code change needed? |
|--------|-------------|---------------------|
| Add a new station | Add entry to `segments` and `stations` in JSON | None |
| Change a segment distance | Edit `distance_km` in JSON | None |
| Add more chargers at a station | Change `charger_count` in JSON | None |
| Swap or add an operator | Change `operator` field on buses in JSON | None |
| Change battery range | Edit `battery_km` in `world` | None |
| Change charging time | Edit `charge_time_min` in `world` | None |
| Change travel speed | Edit `speed_kmh` in `world` | None |
| Add a new soft rule | Add `@rule("new_weight")` function in `rules.py`; add the weight to `weights` in JSON | ~4 lines in `rules.py`; add key to JSON |
| Add a new scenario | Write a new JSON file in `scenarios/` | None |
| Multiple routes sharing stations | Each route has its own `segments` list; stations are referenced by ID across routes | Small: loader needs to merge station pools |
| More buses | Add entries to `buses` array in JSON | None |
| Time-of-day electricity costs | Add a `@rule("electricity_cost")` that uses `slot_time` to look up a rate table from `world` | ~6 lines in `rules.py`; add rate table to JSON |
| Priority buses (always charge first) | Add `@rule("priority")` that returns a large negative value for flagged buses | ~4 lines in `rules.py`; add `"priority": true` field to bus JSON |
| Driver shift limits | Add `shift_end_min` to bus JSON; add `@rule("shift")` that returns large positive cost if charging would push beyond shift | ~6 lines in `rules.py`; add field to bus JSON |
| Variable charge times per station | Add `charge_time_min` override to station JSON; engine reads it per charger | Small: `StationConfig` + loader |

---

## How to change a weight

Edit the `weights` object in the scenario JSON:

```json
"weights": {
  "individual": 1.0,
  "operator": 2.0,
  "overall": 1.0
}
```

That's it. No code change needed. The engine reads weights at runtime.

**What each weight does:**
- `individual` — scales how much a bus's own prospective wait penalises its score. Higher value = scheduler tries harder to minimise the single worst wait.
- `operator` — scales an equity signal: buses from operators with above-average historical wait get a priority boost (negative score contribution). Higher value = stronger cross-operator fairness. Visible effect requires buses from different operators to compete at the same station simultaneously (most apparent in bunched/high-contention scenarios).
- `overall` — scales absolute slot time as a cost. Higher value = the engine strongly prefers earlier absolute charging times, reducing total trip durations network-wide.

---

## How to add a new rule

Add a decorated function to `scheduler/rules.py`:

```python
@rule("electricity_cost")
def electricity_cost_penalty(bus, slot_time, context):
    # slot_time is minutes since midnight. Rate is cheaper off-peak (00:00–06:00).
    hour = (slot_time % (24 * 60)) / 60
    rate = 0.5 if 0 <= hour < 6 else 1.0
    return slot_time * rate
```

Then add the matching weight to every scenario JSON:

```json
"weights": {
  "individual": 1.0,
  "operator": 1.0,
  "overall": 1.0,
  "electricity_cost": 0.01
}
```

And add the field to the `Weights` dataclass in `scheduler/models.py`:

```python
@dataclass
class Weights:
    individual: float
    operator: float
    overall: float
    electricity_cost: float = 0.0  # default 0 keeps old scenarios valid
```

The engine picks it up automatically. No other changes.

---

## Assumptions made

- **Speed is uniform** — all buses travel at the same speed defined in `world.speed_kmh`. No acceleration, traffic, or driver variation.
- **Charging always fills to full** — partial charging is not modelled.
- **Buses do not skip assigned combos** — combo assignment is pre-computed and fixed before the simulation. The simulation respects it exactly.
- **`charger_count = 1` for all current scenarios** — the multi-charger path is implemented and tested but all current scenario files use 1 charger per station.
- **Station order is BK-forward in JSON** — `stations` array is listed Bengaluru-to-Kochi; KB distance maps are derived by mirroring.
- **Departure times use 24-hour local time with no timezone** — `"19:00"` is minutes-since-midnight on the same calendar day.
- **Operator weight effect is most visible under bunching** — with even 15-minute spacing, buses from different operators rarely compete in the same waiting queue at the same station. The mechanism is correct; the effect is most visible in Scenarios 2 and 5.
