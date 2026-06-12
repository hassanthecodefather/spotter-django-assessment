"""API views for the fuel route optimizer."""

import logging
import uuid
from datetime import timezone

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone as django_timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView, exception_handler

from routing.locations import LOCATIONS
from routing.models import FuelStation
from routing.serializers import RouteRequestSerializer
from routing.services import corridor as corridor_service
from routing.services import fuel_optimizer
from routing.services import osrm
from routing.services.geocoder import (
    InvalidCoordinates,
    LocationNotFound,
    LocationOutsideUSA,
    resolve,
)

logger = logging.getLogger(__name__)


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": str(exc),
                "detail": response.data,
            }
        }
    return response


def _error(code: str, message: str, detail: dict = None, http_status: int = 400) -> Response:
    body = {"error": {"code": code, "message": message, "detail": detail or {}}}
    return Response(body, status=http_status)


class LocationsView(APIView):
    def get(self, request):
        q = request.GET.get("q", "").strip()
        limit = min(int(request.GET.get("limit", 20)), 100)
        offset = max(int(request.GET.get("offset", 0)), 0)

        if not q:
            return Response({"locations": LOCATIONS, "total": len(LOCATIONS)})

        from routing.services.geocoder import _CITY_LABELS
        q_lower = q.lower()
        unique = sorted(set(_CITY_LABELS))
        starts = [c for c in unique if c.lower().startswith(q_lower)]
        contains = [c for c in unique if q_lower in c.lower() and not c.lower().startswith(q_lower)]
        matches = starts + contains
        total = len(matches)
        page = matches[offset : offset + limit]
        return Response({
            "locations": [{"label": c, "value": c} for c in page],
            "total": total,
            "offset": offset,
            "limit": limit,
        })


class HealthView(APIView):
    def get(self, request):
        count = FuelStation.objects.count()
        if count == 0:
            return Response(
                {
                    "status": "degraded",
                    "stations_loaded": 0,
                    "prices_as_of": None,
                    "message": "No stations loaded. Run load_stations.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        latest = FuelStation.objects.order_by("-ingested_at").values_list("ingested_at", flat=True).first()
        return Response(
            {
                "status": "ok",
                "stations_loaded": count,
                "prices_as_of": latest.isoformat() if latest else None,
            }
        )


class RouteView(APIView):
    def post(self, request):
        ser = RouteRequestSerializer(data=request.data)
        if not ser.is_valid():
            return _error("VALIDATION_ERROR", "Invalid request body.", ser.errors)

        start_input = ser.validated_data["start"]
        finish_input = ser.validated_data["finish"]

        try:
            start_loc = resolve(start_input)
        except InvalidCoordinates as exc:
            detail = {}
            if exc.args[1:]:
                hint = exc.args[1]
                if hint:
                    detail["hint"] = hint
            return _error("INVALID_COORDINATES", str(exc.args[0]), detail)
        except LocationOutsideUSA as exc:
            return _error(
                "LOCATION_OUTSIDE_USA",
                f"{exc.query!r} is outside the USA.",
                {"nearest_us_city": exc.nearest_city, "distance_miles": round(exc.distance_miles, 1)},
            )
        except LocationNotFound as exc:
            return _error(
                "LOCATION_NOT_FOUND",
                f"Could not find location: {exc.query!r}",
                {"suggestions": exc.suggestions},
            )

        try:
            finish_loc = resolve(finish_input)
        except InvalidCoordinates as exc:
            detail = {}
            if exc.args[1:]:
                hint = exc.args[1]
                if hint:
                    detail["hint"] = hint
            return _error("INVALID_COORDINATES", str(exc.args[0]), detail)
        except LocationOutsideUSA as exc:
            return _error(
                "LOCATION_OUTSIDE_USA",
                f"{exc.query!r} is outside the USA.",
                {"nearest_us_city": exc.nearest_city, "distance_miles": round(exc.distance_miles, 1)},
            )
        except LocationNotFound as exc:
            return _error(
                "LOCATION_NOT_FOUND",
                f"Could not find location: {exc.query!r}",
                {"suggestions": exc.suggestions},
            )

        try:
            route = osrm.get_route(start_loc.lat, start_loc.lng, finish_loc.lat, finish_loc.lng)
        except osrm.RoutingUnavailable as exc:
            return _error(
                "ROUTING_UNAVAILABLE",
                "The routing service is currently unavailable. Please try again shortly.",
                {},
                http_status=502,
            )
        except osrm.RouteNotFound as exc:
            note = None
            if _route_transits_canada(route_points=None, start=start_loc, finish=finish_loc):
                note = "route may transit Canada; Canadian fuel stations are out of scope"
            return _error(
                "ROUTE_NOT_FOUND",
                f"No drivable route found between {start_input!r} and {finish_input!r}.",
                {"note": note} if note else {},
                http_status=422,
            )

        total_distance = route["distance_miles"]
        route_points = route["points"]

        buffer_miles = getattr(settings, "CORRIDOR_BUFFER_MILES", 15)
        candidates, buffer_used = corridor_service.filter_corridor(route_points, buffer_miles)

        try:
            result = fuel_optimizer.optimize(candidates, total_distance)
        except fuel_optimizer.NoStationInRange as exc:
            note = None
            if _gap_transits_canada(route_points, exc.pos_start, exc.pos_end):
                note = "route transits Canada; Canadian fuel stations are out of scope"
            return _error(
                "NO_STATION_IN_RANGE",
                f"No fuel station found between mile {exc.pos_start:.1f} and mile {exc.pos_end:.1f}.",
                {"gap_start_miles": round(exc.pos_start, 1), "gap_end_miles": round(exc.pos_end, 1), "note": note},
                http_status=422,
            )

        latest = (
            FuelStation.objects.order_by("-ingested_at")
            .values_list("ingested_at", flat=True)
            .first()
        )

        stops_data = []
        for stop in result.stops:
            c = stop.candidate
            stops_data.append(
                {
                    "opis_id": c.opis_id,
                    "name": c.name,
                    "address": c.address,
                    "city": c.city,
                    "state": c.state,
                    "price_per_gallon": c.price,
                    "route_position_miles": round(c.route_position_miles, 1),
                    "gallons_purchased": round(stop.gallons_purchased, 3),
                    "cost_usd": round(stop.cost, 2),
                    "lat": c.lat,
                    "lng": c.lng,
                }
            )

        token = str(uuid.uuid4())
        response_data = {
            "start": {
                "input": start_input,
                "lat": round(start_loc.lat, 5),
                "lng": round(start_loc.lng, 5),
                "resolved_city": start_loc.resolved_city,
                "snap_distance_miles": start_loc.snap_distance_miles,
            },
            "finish": {
                "input": finish_input,
                "lat": round(finish_loc.lat, 5),
                "lng": round(finish_loc.lng, 5),
                "resolved_city": finish_loc.resolved_city,
                "snap_distance_miles": finish_loc.snap_distance_miles,
            },
            "total_distance_miles": round(total_distance, 1),
            "route_geometry": route["geometry"],
            "prices_as_of": latest.isoformat() if latest else None,
            "corridor_buffer_miles": buffer_used,
            "fuel": {
                "mpg": fuel_optimizer.MPG,
                "max_range_miles": fuel_optimizer.MAX_RANGE,
                "total_gallons": round(result.total_gallons, 3),
                "total_fuel_cost_usd": round(result.total_fuel_cost, 2),
                "stops": stops_data,
            },
            "map_url": f"/map/?token={token}",
        }

        cache.set(f"map:{token}", response_data, timeout=3600)
        return Response(response_data)


def _route_transits_canada(route_points, start, finish):
    return False


def _gap_transits_canada(route_points: list, gap_start: float, gap_end: float) -> bool:
    """Check if the gap segment is far from any US city (>30 mi), indicating a Canada transit."""
    if not route_points:
        return False

    from routing.services.corridor import _haversine_along_route
    from routing.services import geocoder
    import numpy as np

    pts = np.array(route_points)
    cumulative = _haversine_along_route(pts)

    gap_pts = pts[(cumulative >= gap_start) & (cumulative <= gap_end)]
    if len(gap_pts) == 0:
        return False

    sample = gap_pts[len(gap_pts) // 2]
    lat, lng = float(sample[0]), float(sample[1])

    if len(geocoder._CITY_COORDS) == 0:
        return False

    dists = geocoder._haversine_miles(lat, lng, geocoder._CITY_COORDS[:, 0], geocoder._CITY_COORDS[:, 1])
    min_dist = float(dists.min())
    return min_dist > 30
