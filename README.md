# Bus Charging Scheduler

Streamlit app that schedules electric bus charging along a fixed route with contention-aware, weight-tunable prioritisation.

**Route:** Bengaluru → A → B → C → D → Kochi (540 km, 4 charging stations)

---

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open [http://localhost:8501](http://localhost:8501). Select a scenario from the dropdown.

---

## How to change a weight

Open the scenario JSON file in `scenarios/` and edit the `weights` object:

```json
"weights": {
  "individual": 1.0,
  "operator": 2.0,
  "overall": 1.0
}
```

Reload the app. No code change needed.

| Weight | Effect |
|--------|--------|
| `individual` | Penalises long waits for individual buses. Higher = scheduler prioritises minimising the single worst wait. |
| `operator` | Equity signal across operators. Higher = buses from the most-delayed fleet get a stronger priority boost. Most visible in high-contention scenarios (2, 5). |
| `overall` | Penalises late absolute charging times. Higher = scheduler keeps the whole network moving earlier. |

---

## How to add a new rule

**1. Add the function to `scheduler/rules.py`:**

```python
@rule("electricity_cost")
def electricity_cost_penalty(bus, slot_time, context):
    # Cheaper off-peak: 00:00–06:00
    hour = (slot_time % (24 * 60)) / 60
    rate = 0.5 if 0 <= hour < 6 else 1.0
    return slot_time * rate
```

**2. Add the weight field to `scheduler/models.py`:**

```python
@dataclass
class Weights:
    individual: float
    operator: float
    overall: float
    electricity_cost: float = 0.0   # default keeps old scenarios valid
```

**3. Add the weight to scenario JSON files:**

```json
"weights": { "individual": 1.0, "operator": 1.0, "overall": 1.0, "electricity_cost": 0.01 }
```

The engine picks it up automatically. No other changes.

---

## How to add a new scenario

Create a new JSON file in `scenarios/`. The app discovers all `*.json` files there automatically.

Minimum structure:

```json
{
  "world": {
    "speed_kmh": 60,
    "battery_km": 240,
    "charge_time_min": 25,
    "segments": [
      {"from": "Bengaluru", "to": "A", "distance_km": 100},
      {"from": "A",         "to": "B", "distance_km": 120},
      {"from": "B",         "to": "C", "distance_km": 100},
      {"from": "C",         "to": "D", "distance_km": 120},
      {"from": "D",         "to": "Kochi", "distance_km": 100}
    ]
  },
  "stations": [
    {"id": "A", "charger_count": 1},
    {"id": "B", "charger_count": 2},
    {"id": "C", "charger_count": 1},
    {"id": "D", "charger_count": 1}
  ],
  "weights": {"individual": 1.0, "operator": 1.0, "overall": 1.0},
  "buses": [
    {"id": "bus-BK-01", "operator": "kpn", "direction": "BK", "departure": "19:00"}
  ]
}
```

`charger_count` can be any positive integer. `direction` is `"BK"` (Bengaluru→Kochi) or `"KB"` (Kochi→Bengaluru).

---

## Project layout

```
app.py                  Streamlit UI
scheduler/
  engine.py             load_scenario(), assign_all_combos(), run_schedule()
  feasibility.py        Valid combo enumeration and load-aware assignment
  rules.py              @rule decorator registry + all scoring functions
  models.py             Dataclasses: Bus, World, Charger, ChargeStop, …
scenarios/
  scenario_1.json       Even spacing (baseline)
  scenario_2.json       Bunched start (heavy early contention)
  scenario_3.json       Asymmetric load (10 BK, 4 KB)
  scenario_4.json       Operator-heavy KPN (operator weight = 2.0)
  scenario_5.json       Worst-case convergence (max contention)
```

---

## What's done / not done

**Done:**
- Event-driven scheduler with correct arrival-order processing
- Score-based contention resolution (weights control slot ordering)
- Load-aware combo assignment with round-robin tiebreak (buses spread across valid station combinations)
- Multi-charger support (`charger_count` in JSON, respected by engine)
- All 5 scenarios produce valid schedules (no bus exceeds 240 km range)
- Operator equity rule: buses from the most-delayed fleet get a priority boost
- Three-tab Streamlit UI: scenario input, per-bus timetable, per-station queue view

**Limitations / what's next:**
- Combo assignment is pre-computed and fixed before the simulation; a fully dynamic replanning step could further reduce waits in extreme cases
- Operator weight effect is most visible in high-contention scenarios; with 15-minute even spacing, cross-operator queue competition is rare
- No persistent storage — state is in-memory per Streamlit session
- Hard rule support (e.g., absolute time windows, maintenance blocks) would require a constraint layer on top of the current soft-score system
