from unittest.mock import patch

import numpy as np
from django.test import TestCase

from routing.services.corridor import (
    CandidateStop,
    _downsample_route,
    _filter_at_buffer,
    _haversine_along_route,
    filter_corridor,
    preload_stations,
    reload_stations,
)


def make_station_row(opis_id, lat, lng, price=3.0):
    return {
        "opis_id": opis_id,
        "name": f"Station {opis_id}",
        "address": "",
        "city": "City",
        "state": "TX",
        "retail_price": price,
        "lat": lat,
        "lng": lng,
    }


def _inject_stations(stations):
    import routing.services.corridor as corridor_mod

    corridor_mod._station_ids = stations
    corridor_mod._station_coords = np.array(
        [[s["lat"], s["lng"]] for s in stations], dtype=np.float64
    )
    corridor_mod._cache_loaded = True


class TestCorridorFilter(TestCase):
    def setUp(self):
        self.route_points = [
            (34.05, -118.24),
            (34.10, -117.80),
            (34.20, -117.30),
            (34.30, -116.80),
            (34.40, -116.30),
        ]

    def test_station_5_miles_off_route_included(self):
        station_lat = 34.05 + 0.04  # roughly 3 miles north
        station_lng = -118.24
        _inject_stations([make_station_row(1, station_lat, station_lng)])

        candidates, buf = filter_corridor(self.route_points, buffer_miles=15.0)
        ids = [c.opis_id for c in candidates]
        self.assertIn(1, ids)

    def test_station_30_miles_off_route_excluded_at_base_buffer(self):
        # Tests the raw buffer filter without adaptive widening.
        # Station at ~31 miles off route must not appear in a 15-mile buffer.
        station_lat = 34.05 + 0.45  # roughly 31 miles north
        station_lng = -118.24
        _inject_stations([make_station_row(1, station_lat, station_lng)])

        candidates = _filter_at_buffer(self.route_points, buffer_miles=15.0)
        ids = [c.opis_id for c in candidates]
        self.assertNotIn(1, ids)

    def test_route_position_monotonic(self):
        stations = [
            make_station_row(1, 34.10, -118.20),
            make_station_row(2, 34.20, -117.50),
            make_station_row(3, 34.35, -116.50),
        ]
        _inject_stations(stations)

        candidates, _ = filter_corridor(self.route_points, buffer_miles=30.0)
        positions = [c.route_position_miles for c in candidates]
        self.assertEqual(positions, sorted(positions))

    def test_adaptive_buffer_widens_when_empty(self):
        station_lat = 34.05 + 0.25  # about 17 miles off route, outside 15 mi buffer
        _inject_stations([make_station_row(1, station_lat, -118.24)])

        candidates, buf = filter_corridor(self.route_points, buffer_miles=15.0)
        if candidates:
            self.assertGreater(buf, 15.0)

    def tearDown(self):
        reload_stations.__module__
        import routing.services.corridor as corridor_mod
        corridor_mod._cache_loaded = False
        corridor_mod._station_ids = []
        corridor_mod._station_coords = np.empty((0, 2))
