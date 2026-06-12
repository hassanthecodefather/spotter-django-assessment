"""Greedy fixed-range fuel optimizer.

This is the canonical greedy algorithm for fixed-range refueling, which is
provably optimal for the case of a single vehicle with a fixed tank size and
known fuel prices at fixed locations along a route.

The effective_price seam is the single extension point for detour cost,
fuel-card discounts, and time cost without touching the greedy loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from routing.services.corridor import CandidateStop

logger = logging.getLogger(__name__)

MAX_RANGE = 500.0  # miles
MPG = 10.0
TANK_GALLONS = 50.0


@dataclass
class FuelStop:
    candidate: CandidateStop
    gallons_purchased: float
    cost: float


@dataclass
class OptimizationResult:
    stops: list[FuelStop]
    total_fuel_cost: float
    total_gallons: float


class NoStationInRange(Exception):
    def __init__(self, pos_start: float, pos_end: float):
        self.pos_start = pos_start
        self.pos_end = pos_end
        super().__init__(
            f"No fuel station in the {pos_end - pos_start:.1f}-mile segment "
            f"from mile {pos_start:.1f} to mile {pos_end:.1f}"
        )


def effective_price(stop: CandidateStop) -> float:
    """Pluggable objective function.

    Currently returns the raw retail price. This is the seam where detour
    cost, fuel-card discounts, and time cost plug in later. Changing this
    function is the only edit needed to alter the optimization objective.
    """
    return stop.price


def optimize(
    stations: list[CandidateStop],
    total_distance: float,
    price_fn: Callable[[CandidateStop], float] = effective_price,
) -> OptimizationResult:
    """Run the greedy fixed-range optimizer and return fuel stops + total cost.

    Vehicle starts with a full tank (MAX_RANGE miles of range).
    Stations must be sorted by route_position_miles ascending.
    """
    if total_distance <= MAX_RANGE:
        return OptimizationResult(stops=[], total_fuel_cost=0.0, total_gallons=0.0)

    stations = sorted(stations, key=lambda s: (s.route_position_miles, s.opis_id))

    stops: list[FuelStop] = []
    pos = 0.0
    fuel = MAX_RANGE

    while pos + fuel < total_distance:
        reachable = [
            s for s in stations if pos < s.route_position_miles <= pos + fuel
        ]
        if not reachable:
            raise NoStationInRange(pos, pos + fuel)

        cheapest = min(reachable, key=lambda s: (price_fn(s), s.opis_id))

        drive = cheapest.route_position_miles - pos
        fuel -= drive
        pos = cheapest.route_position_miles

        lookahead = [
            s for s in stations
            if pos < s.route_position_miles <= pos + MAX_RANGE
            and price_fn(s) < price_fn(cheapest)
        ]

        if pos + MAX_RANGE >= total_distance and not lookahead:
            need = (total_distance - pos) - fuel
        elif lookahead:
            need = (lookahead[0].route_position_miles - pos) - fuel
        else:
            need = MAX_RANGE - fuel

        need = max(need, 0.0)
        gallons = need / MPG
        cost = gallons * price_fn(cheapest)

        stops.append(FuelStop(candidate=cheapest, gallons_purchased=gallons, cost=cost))
        fuel += need

    total_cost = sum(s.cost for s in stops)
    total_gallons = sum(s.gallons_purchased for s in stops)
    return OptimizationResult(
        stops=stops,
        total_fuel_cost=total_cost,
        total_gallons=total_gallons,
    )
