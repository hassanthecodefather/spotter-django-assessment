"""API endpoint tests. All external HTTP is mocked."""

import json
from unittest.mock import MagicMock, patch

import responses as responses_lib
from django.test import TestCase
from django.urls import reverse

from routing.models import FuelStation
from routing.services.corridor import CandidateStop


MOCK_GEOMETRY = "yrnyHhhlpF"
MOCK_ROUTE = {
    "distance_miles": 800.0,
    "geometry": MOCK_GEOMETRY,
    "points": [(34.05, -118.24), (35.0, -117.0), (36.0, -115.0)],
}

MOCK_CANDIDATES = [
    CandidateStop(
        opis_id=7,
        name="WOODSHED OF BIG CABIN",
        address="I-44, EXIT 283",
        city="Big Cabin",
        state="OK",
        price=3.007,
        lat=36.53,
        lng=-95.22,
        route_position_miles=412.0,
    )
]


def _create_station():
    FuelStation.objects.create(
        opis_id=7,
        name="WOODSHED OF BIG CABIN",
        address="I-44, EXIT 283",
        city="Big Cabin",
        state="OK",
        retail_price="3.007333",
        lat=36.53,
        lng=-95.22,
        geocode_quality="city_exact",
    )


class TestRouteHappyPath(TestCase):
    def setUp(self):
        _create_station()

    @patch("routing.views.osrm.get_route", return_value=MOCK_ROUTE)
    @patch("routing.views.corridor_service.filter_corridor", return_value=(MOCK_CANDIDATES, 15.0))
    def test_happy_path_returns_200(self, mock_corridor, mock_osrm):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("fuel", data)
        self.assertIn("route_geometry", data)
        self.assertIn("map_url", data)

    @patch("routing.views.osrm.get_route", return_value=MOCK_ROUTE)
    @patch("routing.views.corridor_service.filter_corridor", return_value=(MOCK_CANDIDATES, 15.0))
    def test_request_id_header_present(self, mock_corridor, mock_osrm):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertIn("X-Request-ID", resp)

    @patch("routing.views.osrm.get_route", return_value=MOCK_ROUTE)
    @patch("routing.views.corridor_service.filter_corridor", return_value=(MOCK_CANDIDATES, 15.0))
    def test_response_has_start_finish_metadata(self, mock_corridor, mock_osrm):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        data = resp.json()
        self.assertIn("resolved_city", data["start"])
        self.assertIn("snap_distance_miles", data["start"])


class TestRouteErrors(TestCase):
    def test_missing_body_returns_400(self):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_location_not_found_returns_400(self):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Xyzzyville, TX", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["error"]["code"], "LOCATION_NOT_FOUND")

    def test_location_outside_usa_returns_400(self):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "43.65,-79.38", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertEqual(data["error"]["code"], "LOCATION_OUTSIDE_USA")

    @patch("routing.views.osrm.get_route")
    def test_routing_unavailable_returns_502(self, mock_osrm):
        from routing.services.osrm import RoutingUnavailable
        mock_osrm.side_effect = RoutingUnavailable("OSRM down")
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 502)
        data = resp.json()
        self.assertEqual(data["error"]["code"], "ROUTING_UNAVAILABLE")

    @patch("routing.views.osrm.get_route")
    def test_route_not_found_returns_422(self, mock_osrm):
        from routing.services.osrm import RouteNotFound
        mock_osrm.side_effect = RouteNotFound("No route")
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 422)
        data = resp.json()
        self.assertEqual(data["error"]["code"], "ROUTE_NOT_FOUND")

    @patch("routing.views.osrm.get_route", return_value=MOCK_ROUTE)
    @patch("routing.views.corridor_service.filter_corridor", return_value=([], 15.0))
    def test_no_station_in_range_returns_422(self, mock_corridor, mock_osrm):
        resp = self.client.post(
            "/api/v1/route/",
            data=json.dumps({"start": "Los Angeles, CA", "finish": "New York, NY"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 422)
        data = resp.json()
        self.assertEqual(data["error"]["code"], "NO_STATION_IN_RANGE")

    def test_get_method_not_allowed(self):
        resp = self.client.get("/api/v1/route/")
        self.assertEqual(resp.status_code, 405)


class TestHealthEndpoint(TestCase):
    def test_health_ok_when_stations_exist(self):
        _create_station()
        resp = self.client.get("/api/v1/health/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertGreater(data["stations_loaded"], 0)

    def test_health_503_when_no_stations(self):
        FuelStation.objects.all().delete()
        resp = self.client.get("/api/v1/health/")
        self.assertEqual(resp.status_code, 503)
