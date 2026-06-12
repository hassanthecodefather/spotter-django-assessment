from django.test import TestCase

from routing.services.corridor import CandidateStop
from routing.services.fuel_optimizer import (
    MAX_RANGE,
    MPG,
    NoStationInRange,
    OptimizationResult,
    effective_price,
    optimize,
)


def make_stop(opis_id, pos, price):
    return CandidateStop(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        address="",
        city="City",
        state="TX",
        price=price,
        lat=0.0,
        lng=0.0,
        route_position_miles=pos,
        detour_miles=0.0,
    )


class TestOptimizerShortTrip(TestCase):
    def test_trip_under_500_miles_has_no_stops(self):
        stations = [make_stop(1, 100, 3.50), make_stop(2, 300, 3.00)]
        result = optimize(stations, 450)
        self.assertEqual(result.stops, [])
        self.assertEqual(result.total_fuel_cost, 0.0)

    def test_trip_exactly_500_miles_has_no_stops(self):
        result = optimize([], 500)
        self.assertEqual(result.stops, [])

    def test_start_equals_finish_zero_distance(self):
        result = optimize([], 0.0)
        self.assertEqual(result.stops, [])


class TestOptimizerGreedy(TestCase):
    def test_greedy_picks_cheapest_reachable(self):
        stations = [
            make_stop(1, 100, 4.00),  # expensive, close
            make_stop(2, 450, 2.50),  # cheap, far
        ]
        result = optimize(stations, 700)
        opis_ids = [s.candidate.opis_id for s in result.stops]
        self.assertIn(2, opis_ids)

    def test_greedy_skips_expensive_and_goes_to_cheaper(self):
        # Both stations are within initial 500-mile range.
        # The optimizer picks the cheapest reachable (station 2) directly,
        # skipping the expensive station 1 entirely.
        stations = [
            make_stop(1, 200, 4.00),
            make_stop(2, 400, 2.50),
        ]
        result = optimize(stations, 800)
        opis_ids = [s.candidate.opis_id for s in result.stops]
        self.assertNotIn(1, opis_ids)  # expensive stop skipped
        self.assertIn(2, opis_ids)

    def test_fills_up_when_no_cheaper_station_ahead(self):
        # Station 1 at 200 (cheap), station 2 at 600 (expensive), total 1100 mi.
        # At station 1 we arrive with (500-200)=300 miles of fuel remaining.
        # No cheaper station ahead, destination not reachable: fill to MAX_RANGE.
        # That means buying (500-300)=200 miles worth = 20 gallons.
        stations = [
            make_stop(1, 200, 2.50),
            make_stop(2, 600, 3.50),
        ]
        result = optimize(stations, 1100)
        stop_at_200 = next(s for s in result.stops if s.candidate.opis_id == 1)
        fuel_on_arrival = MAX_RANGE - 200.0  # 300 miles remaining
        expected_gallons = (MAX_RANGE - fuel_on_arrival) / MPG  # 20 gallons
        self.assertAlmostEqual(stop_at_200.gallons_purchased, expected_gallons, places=2)

    def test_buys_just_enough_to_finish_on_final_leg(self):
        stations = [make_stop(1, 300, 3.00)]
        total_dist = 700.0
        result = optimize(stations, total_dist)
        self.assertEqual(len(result.stops), 1)
        stop = result.stops[0]
        fuel_at_stop = MAX_RANGE - 300.0
        fuel_needed = (total_dist - 300.0) - fuel_at_stop
        self.assertAlmostEqual(stop.gallons_purchased, max(fuel_needed, 0) / MPG, places=3)

    def test_cost_arithmetic_exact(self):
        stations = [make_stop(1, 300, 3.0)]
        result = optimize(stations, 800.0)
        expected_cost = sum(s.gallons_purchased * s.candidate.price for s in result.stops)
        self.assertAlmostEqual(result.total_fuel_cost, expected_cost, places=6)


class TestOptimizerNoStation(TestCase):
    def test_no_station_in_range_raises(self):
        stations = [make_stop(1, 600, 3.00)]  # outside initial range
        with self.assertRaises(NoStationInRange) as ctx:
            optimize(stations, 1000)
        self.assertGreater(ctx.exception.pos_end - ctx.exception.pos_start, 0)


class TestOptimizerTieBreak(TestCase):
    def test_tie_breaks_deterministically_by_opis_id(self):
        stations = [
            make_stop(20, 300, 3.00),
            make_stop(5, 300, 3.00),
        ]
        result = optimize(stations, 700)
        self.assertEqual(result.stops[0].candidate.opis_id, 5)


class TestEffectivePriceSeam(TestCase):
    def test_custom_price_function_changes_choice(self):
        stations = [
            make_stop(1, 200, 3.00),
            make_stop(2, 250, 2.50),
        ]

        def penalized_price(stop):
            return stop.price + (10.0 if stop.opis_id == 2 else 0.0)

        result = optimize(stations, 700, price_fn=penalized_price)
        opis_ids = [s.candidate.opis_id for s in result.stops]
        self.assertIn(1, opis_ids)
        self.assertNotIn(2, opis_ids)
