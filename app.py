"""
app.py — Streamlit UI entry point.

Three tabs:
  1. Scenario Input  — show exactly what was fed in (buses, world, weights)
  2. Per-Bus Timetable — each bus's full journey with stops
  3. Per-Station View  — each station's queue in charge order

Run with: streamlit run app.py
"""

import os
import streamlit as st
import pandas as pd

from scheduler.engine import load_scenario, assign_all_combos, run_schedule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def min_to_hhmm(minutes: float) -> str:
    """Convert minutes since midnight to HH:MM, handling past-midnight times."""
    total = int(minutes)
    h = (total // 60) % 24
    m = total % 60
    day = total // (60 * 24)
    suffix = f" (+{day}d)" if day > 0 else ""
    return f"{h:02d}:{m:02d}{suffix}"


def get_scenario_files() -> list:
    """Return sorted list of scenario JSON paths."""
    folder = "scenarios"
    files = sorted(f for f in os.listdir(folder) if f.endswith(".json"))
    return [os.path.join(folder, f) for f in files]


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Bus Charging Scheduler", layout="wide")
st.title("⚡ Bus Charging Scheduler")
st.caption("Bengaluru → Kochi · 540 km · 4 charging stations")

# ---------------------------------------------------------------------------
# Scenario selector — visible immediately on load
# ---------------------------------------------------------------------------

scenario_files = get_scenario_files()
scenario_labels = [os.path.basename(f).replace(".json", "") for f in scenario_files]

selected_label = st.selectbox(
    "Select Scenario",
    scenario_labels,
    index=0
)

selected_path = scenario_files[scenario_labels.index(selected_label)]

# ---------------------------------------------------------------------------
# Run the scheduler
# ---------------------------------------------------------------------------

buses, stations, world, weights = load_scenario(selected_path)
assign_all_combos(buses,stations, world, weights)  # note: corrected arg order
run_schedule(buses, stations, world, weights)


# ---------------------------------------------------------------------------
# Summary Stats Bar
# ---------------------------------------------------------------------------

all_waits = [stop.wait_min for bus in buses for stop in bus.charge_stops]
total_buses = len(buses)
avg_wait = sum(all_waits) / len(all_waits) if all_waits else 0
max_wait = max(all_waits) if all_waits else 0
total_delay = sum(all_waits)

st.markdown("---")
col1, col2, col3, col4 = st.columns(4)
col1.metric("🚌 Buses Scheduled", total_buses)
col2.metric("⏱ Avg Wait", f"{avg_wait:.1f} min")
col3.metric("⚠️ Longest Wait", f"{max_wait:.0f} min")
col4.metric("🌐 Total Network Delay", f"{total_delay:.0f} min")
st.markdown("---")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab1, tab2, tab3 = st.tabs(["📋 Scenario Input", "🚌 Per-Bus Timetable", "🔌 Per-Station View"])


# ── Tab 1: Scenario Input ────────────────────────────────────────────────────

with tab1:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("World Config")
        world_data = {
            "speed_kmh": world.speed_kmh,
            "battery_km": world.battery_km,
            "charge_time_min": world.charge_time_min,
        }
        st.table(pd.DataFrame([world_data]))

        st.subheader("Segments")
        seg_data = [
            {"from": s.from_stop, "to": s.to_stop, "distance_km": s.distance_km}
            for s in world.segments
        ]
        st.table(pd.DataFrame(seg_data))

        st.subheader("Weights")
        w_data = {
            "individual": weights.individual,
            "operator": weights.operator,
            "overall": weights.overall,
        }
        st.table(pd.DataFrame([w_data]))

    with col2:
        st.subheader("Buses")
        bus_data = [
            {
                "id": b.id,
                "operator": b.operator,
                "direction": b.direction,
                "departure": min_to_hhmm(b.departure_min),
                "charge_at": " → ".join(b.charge_stations),
            }
            for b in buses
        ]
        st.dataframe(pd.DataFrame(bus_data), use_container_width=True, hide_index=True)


# ── Tab 2: Per-Bus Timetable ─────────────────────────────────────────────────

with tab2:
    st.subheader("Per-Bus Timetable")
    st.caption("Each row is one bus. Stop columns show arrival / wait / depart at each charging station.")

    all_station_ids = [s.id for s in stations]

    rows = []
    for bus in buses:
        row = {
            "Bus ID": bus.id,
            "Operator": bus.operator,
            "Direction": bus.direction,
            "Departure": min_to_hhmm(bus.departure_min),
        }

        # Add columns for each station — blank if bus doesn't stop there
        stops_by_station = {stop.station_id: stop for stop in bus.charge_stops}
        for sid in all_station_ids:
            if sid in stops_by_station:
                s = stops_by_station[sid]
                row[f"{sid} arrival"] = min_to_hhmm(s.arrival_min)
                row[f"{sid} wait"]    = f"{int(s.wait_min)}m"
                row[f"{sid} depart"]  = min_to_hhmm(s.charge_end_min)
            else:
                row[f"{sid} arrival"] = "—"
                row[f"{sid} wait"]    = "—"
                row[f"{sid} depart"]  = "—"

        row["Final arrival"] = min_to_hhmm(bus.current_time_min)
        rows.append(row)

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ── Tab 3: Per-Station View ──────────────────────────────────────────────────

with tab3:
    st.subheader("Per-Station View")
    st.caption("Each station's charging queue, ordered by charge start time.")

    for station in stations:
        st.markdown(f"### Station {station.id}")

        # Collect all stops at this station across all buses
        station_rows = []
        for bus in buses:
            for stop in bus.charge_stops:
                if stop.station_id == station.id:
                    station_rows.append({
                        "Bus ID":       bus.id,
                        "Operator":     bus.operator,
                        "Direction":    bus.direction,
                        "Arrival":      min_to_hhmm(stop.arrival_min),
                        "Wait":         f"{int(stop.wait_min)}m",
                        "Charge Start": min_to_hhmm(stop.charge_start_min),
                        "Charge End":   min_to_hhmm(stop.charge_end_min),
                    })

        if station_rows:
            # Sort by charge start time so queue order is clear
            station_rows.sort(key=lambda r: r["Charge Start"])
            st.dataframe(
                pd.DataFrame(station_rows),
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info(f"No buses scheduled at station {station.id} in this scenario.")