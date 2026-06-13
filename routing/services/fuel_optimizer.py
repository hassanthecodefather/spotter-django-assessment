"""Decides which stations to stop at and how many gallons to buy at each one.

HOW IT WORKS:
  The truck starts with a full tank (500 miles of range).
  We keep driving and asking the same two questions at each stop:

    1. Which station should I stop at?   → always the cheapest one we can reach.
    2. How many gallons should I buy?    → depends on what's ahead (see the 3 cases in optimize()).

  That's the whole algorithm. The rest of this file is just data structures around it.

READ IN THIS ORDER:
  1. MAX_RANGE / MPG / TANK_GALLONS  — the truck's specs. Change these to model a different vehicle.
  2. optimize()                      — the algorithm. The 3 BUY cases inside the loop are the key part.
  3. effective_price()               — how we decide which station is "cheaper". Swap this out to add
                                       discounts or detour penalties without touching the algorithm.
  4. FuelStop / OptimizationResult   — simple data containers for the result.
  5. NoStationInRange                — the error raised when a stretch of road has no stations at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from routing.services.corridor import CandidateStop

logger = logging.getLogger(__name__)


# ── The truck ─────────────────────────────────────────────────────────────────

MAX_RANGE    = 500.0  # miles on a full tank
MPG          = 10.0   # miles per gallon
TANK_GALLONS = 50.0   # MAX_RANGE ÷ MPG — keep these three in sync


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class FuelStop:
    candidate: CandidateStop  # the station
    gallons_purchased: float  # how many gallons we buy here (not always a full tank)
    cost: float               # gallons × price


@dataclass
class OptimizationResult:
    stops: list[FuelStop]  # every stop in driving order
    total_fuel_cost: float
    total_gallons: float


class NoStationInRange(Exception):
    """No station exists within reach — the truck would run dry in this stretch.
    Usually means the route crosses into Canada where we have no data."""
    def __init__(self, pos_start: float, pos_end: float):
        self.pos_start = pos_start
        self.pos_end = pos_end
        super().__init__(
            f"No fuel station in the {pos_end - pos_start:.1f}-mile gap "
            f"from mile {pos_start:.1f} to mile {pos_end:.1f}"
        )


# ── Price function (swap this out to add discounts / detour cost) ─────────────

def effective_price(stop: CandidateStop) -> float:
    """How we compare stations. Right now: just the posted price.

    To add a fuel-card discount:   return stop.price * 0.97
    To penalise long detours:      return stop.price + (stop.detour_miles * cost_per_mile)
    No other code needs to change.
    """
    return stop.price


# ── The algorithm ─────────────────────────────────────────────────────────────

def optimize(
    stations: list[CandidateStop],
    total_distance: float,
    price_fn: Callable[[CandidateStop], float] = effective_price,
) -> OptimizationResult:
    """Plan the cheapest fuel stops for this trip.

    Starts with a full tank. Keeps looping until the destination is reachable.
    Each loop iteration = one stop.
    """

    # Trip fits in one tank — no stops needed.
    if total_distance <= MAX_RANGE:
        return OptimizationResult(stops=[], total_fuel_cost=0.0, total_gallons=0.0)

    stations = sorted(stations, key=lambda s: (s.route_position_miles, s.opis_id))

    stops: list[FuelStop] = []
    pos  = 0.0        # miles driven so far
    fuel = MAX_RANGE  # miles left in the tank

    while pos + fuel < total_distance:

        # ── WHICH STATION? ────────────────────────────────────────────────────
        # All stations we can reach without running dry.
        reachable = [s for s in stations if pos < s.route_position_miles <= pos + fuel]

        if not reachable:
            raise NoStationInRange(pos, pos + fuel)

        # Pick the cheapest one. Tie-break by ID so the result is always the same.
        cheapest = min(reachable, key=lambda s: (price_fn(s), s.opis_id))

        # Drive there.
        fuel -= cheapest.route_position_miles - pos
        pos   = cheapest.route_position_miles

        # ── HOW MANY GALLONS? ─────────────────────────────────────────────────
        # Look ahead: is there a cheaper station within the next full tank?
        cheaper_ahead = [
            s for s in stations
            if pos < s.route_position_miles <= pos + MAX_RANGE
            and price_fn(s) < price_fn(cheapest)
        ]

        if pos + MAX_RANGE >= total_distance and not cheaper_ahead:
            # CASE 1 — destination is within reach, nothing cheaper ahead.
            # Buy just enough to finish. Don't overfill.
            need = (total_distance - pos) - fuel

        elif cheaper_ahead:
            # CASE 2 — cheaper station ahead within one tank.
            # Buy just enough to get there. Save the big fill for the lower price.
            need = (cheaper_ahead[0].route_position_miles - pos) - fuel

        else:
            # CASE 3 — more road ahead, no cheaper station in sight.
            # This is the best price we'll see for a while — fill the tank now.
            need = MAX_RANGE - fuel

        need = max(need, 0.0)  # can't buy negative gallons
        gallons = need / MPG
        cost    = gallons * price_fn(cheapest)

        stops.append(FuelStop(candidate=cheapest, gallons_purchased=gallons, cost=cost))
        fuel += need

    return OptimizationResult(
        stops=stops,
        total_fuel_cost=sum(s.cost for s in stops),
        total_gallons=sum(s.gallons_purchased for s in stops),
    )
