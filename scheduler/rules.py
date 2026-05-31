"""
rules.py — Rule Registry + all scoring functions.

HOW IT WORKS:
  1. @rule("weight_key") registers a function into RULES list
  2. The engine calls every rule for every (bus, slot_time) candidate
  3. cost = sum of weight * rule_score for all rules
  4. Lowest cost bus wins the slot

HOW TO ADD A NEW RULE:
  @rule("individual")
  def my_new_rule(bus, slot_time, context):
      return some_number

  That's it. No engine changes. No imports to update.
"""

from typing import List, Tuple, Callable
from .models import Bus
from collections import defaultdict

# ---------------------------------------------------------------------------
# Registry — populated automatically by @rule decorator
# ---------------------------------------------------------------------------

RULES: List[Tuple[str, Callable]] = []


def rule(weight_key: str):
    """
    Decorator that registers a scoring function.
    weight_key must match a key in scenario["weights"].
    """
    def decorator(fn):
        RULES.append((weight_key, fn))
        return fn
    return decorator

def build_context(buses, station_id: str) -> dict:
    """
    Pre-compute operator average waits so score_bus() doesn't recompute
    this N times per station.  Returns:
      {
        "operator_avg_wait": {"kpn": 12.5, "freshbus": 8.0, ...}
      }
    """
    from collections import defaultdict
    totals = defaultdict(float)
    counts = defaultdict(int)

    for bus in buses:
        for stop in bus.charge_stops:
            totals[bus.operator] += stop.wait_min
            counts[bus.operator] += 1

    operator_avg_wait = {
        op: totals[op] / counts[op]
        for op in totals
        if counts[op] > 0
    }
    return {"operator_avg_wait": operator_avg_wait}
# ---------------------------------------------------------------------------
# context dict passed to every rule:
# {
#   "operator_avg_wait": {"kpn": 12.0, "freshbus": 5.0, ...},
#   "weights": Weights object
# }
# ---------------------------------------------------------------------------


@rule("individual")
def wait_penalty(bus: Bus, slot_time: float, context: dict) -> float:
    """
    How long does THIS bus wait for this slot?
    slot_time is when the charger is free.
    bus.earliest_available is when the bus arrives.
    
    If slot_time < arrival, wait = 0 (charger was free, no queue).
    If slot_time > arrival, wait = slot_time - arrival (bus queues).
    
    Higher wait = higher cost = bus less likely to get this slot
    unless no better option exists.
    """
    return max(0.0, slot_time - bus.earliest_available)


@rule("operator")
def operator_fairness_penalty(bus: Bus, slot_time: float, context: dict) -> float:
    """
    If this operator's fleet already has a HIGH average wait,
    giving this bus yet another long wait makes it worse.
    
    We return the operator's current average wait as extra cost,
    so the engine is nudged toward reducing wait for overloaded operators.
    
    When operator weight = 2.0 (Scenario 4), this rule's influence doubles,
    visibly shifting the schedule to protect KPN buses.
    """
    avg_waits = context.get("operator_avg_wait", {})
    return avg_waits.get(bus.operator, 0.0)


@rule("overall")
def network_delay_penalty(bus: Bus, slot_time: float, context: dict) -> float:
    """
    Penalise late absolute slot times.
    A slot at minute 1400 costs more than one at minute 1200,
    nudging the engine to keep the whole network moving early.
    
    This is a tiebreaker for network-wide efficiency —
    when individual and operator scores are equal, earlier slots win.
    """
    return slot_time

# ---------------------------------------------------------------------------
# Scoring helper — called by engine for every candidate bus
# ---------------------------------------------------------------------------

def score_bus(bus: Bus, slot_time: float, context: dict, weights) -> float:
    """
    Compute total cost for assigning `bus` to `slot_time`.
    
    cost = sum over all rules of: weight * rule_score
    
    Lower cost = better fit for this slot.
    The weights come from the scenario JSON — one value, one place.
    """
    total = 0.0
    for weight_key, rule_fn in RULES:
        w = getattr(weights, weight_key)   # weights.individual / .operator / .overall
        total += w * rule_fn(bus, slot_time, context)
    return total