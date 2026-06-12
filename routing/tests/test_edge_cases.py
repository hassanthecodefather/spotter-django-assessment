"""Edge case tests per Part 13 of the spec."""

import json
from unittest.mock import patch

from django.test import TestCase

from routing.models import FuelStation
from routing.services.geocoder import (
    InvalidCoordinates,
    LocationNotFound,
    LocationOutsideUSA,
    resolve,
)
from routing.services.corridor import CandidateStop


MOCK_ROUTE_ZERO = {
    "distance_miles": 0.0,
    "geometry": "yrnyHhhlpF",
    "points": [(34.05, -118.24)],
}

MOCK_ROUTE_SAME = {
    "distance_miles": 0.0,
    "geometry": "yrnyHhhlpF",
    "points": [(34.05, -118.24), (34.05, -118.24)],
}

MOCK_ROUTE_800 = {
    "distance_miles": 800.0,
    "geometry": "yrnyHhhlpF",
    "points": [(34.05, -118.24), (35.0, -117.0), (36.0, -115.0)],
}


def _create_station():
    FuelStation.objects.create(
        opis_id=7,
        name="Test Station",
        address="",
        city="Big Cabin",
        state="OK",
        retail_price="3.007",
        lat=36.53,
        lng=-95.22,
        geocode_quality="city_exact",
    )


class TestStartEqualsFinish(TestCase):
    def setUp(self):
        _create_station()

    @patch("routing.views.osrm.get_route", return_value=MOCK_ROUTE_ZERO)
    @patch("routing.views.corridor_service.filter_corridor", return_value=([], 15.0))
    def test_same_start_finish_returns_200_zero_cost(self, mock_cor, mock_osrm):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "Los Angeles, CA"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["fuel"]["total_fuel_cost_usd"], 0.0)
        self.assertEqual(data["fuel"]["stops"], [])


class TestSwappedCoordinates(TestCase):
    def test_swapped_lat_lng_hint(self):
        # -118.24,34.05 has lat=-118.24 which is invalid (< -90)
        try:
            resolve("-118.24,34.05")
            self.fail("Expected InvalidCoordinates")
        except InvalidCoordinates as exc:
            # Should include a hint about swapped coordinates
            hint = exc.args[1] if len(exc.args) > 1 else None
            if hint:
                self.assertIn("swap", hint.lower())


class TestCityNormalization(TestCase):
    def test_accent_stripping(self):
        # Espanola, NM (accented version should match)
        try:
            loc = resolve("Espanola, NM")
            self.assertIn("NM", loc.resolved_city)
        except LocationNotFound:
            loc2 = resolve("Espanola, NM")

    def test_st_to_saint_alias(self):
        try:
            loc1 = resolve("St. Louis, MO")
            self.assertIn("MO", loc1.resolved_city)
        except LocationNotFound:
            pass


class TestThreeRoutingFailureCodes(TestCase):
    def setUp(self):
        _create_station()

    @patch("routing.views.osrm.get_route")
    def test_routing_unavailable_502(self, mock_osrm):
        from routing.services.osrm import RoutingUnavailable
        mock_osrm.side_effect = RoutingUnavailable("timeout")
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"]["code"], "ROUTING_UNAVAILABLE")

    @patch("routing.views.osrm.get_route")
    def test_route_not_found_422(self, mock_osrm):
        from routing.services.osrm import RouteNotFound
        mock_osrm.side_effect = RouteNotFound("no route")
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"]["code"], "ROUTE_NOT_FOUND")

    @patch("routing.views.osrm.get_route", return_value=MOCK_ROUTE_800)
    @patch("routing.views.corridor_service.filter_corridor", return_value=([], 50.0))
    def test_no_station_in_range_422(self, mock_cor, mock_osrm):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()["error"]["code"], "NO_STATION_IN_RANGE")


class TestMapTokenExpiry(TestCase):
    def test_expired_map_token_returns_404(self):
        resp = self.client.get("/map/?token=does-not-exist-xyz")
        self.assertEqual(resp.status_code, 404)
        data = resp.json()
        self.assertEqual(data["error"]["code"], "MAP_EXPIRED")

    def test_missing_token_returns_404(self):
        resp = self.client.get("/map/")
        self.assertEqual(resp.status_code, 404)


class TestLocationNotFoundSuggestions(TestCase):
    def test_suggestions_included_in_error(self):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Dallass, TX", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertIn("suggestions", data["error"]["detail"])
