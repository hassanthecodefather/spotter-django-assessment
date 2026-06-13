"""API views for the fuel route optimizer.

FILE NAVIGATION — follow these in order to understand a full request:
  1. _error()                  — Skip to this first. Every error the API returns
                                  looks the same: code, message, detail. This is
                                  the one function that builds all of them.
  2. LocationsView             — The city search dropdown. User types "Chi",
                                  this returns "Chicago, IL" and similar.
  3. HealthView                — A quick check: are stations loaded? Used by
                                  monitoring tools to know if the app is ready.
  4. RouteView                 — The main event. User submits start + finish,
                                  this runs the full pipeline and returns stops,
                                  cost, distance, and the map. Read Steps 1–9
                                  inside post() to follow exactly what happens.
  5. _gap_transits_canada()    — If the optimizer can't find a station for 500
                                  miles, this checks whether that gap is in Canada
                                  so we can tell the user why.
"""

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


# ── 1. Framework error wrapper ────────────────────────────────────────────────

def custom_exception_handler(exc, context):
    # Django REST Framework has its own error format. We override it here so
    # that framework errors look the same as our own — one consistent shape
    # for every error the API ever returns, no matter where it comes from.
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


# ── 2. Shared error builder ───────────────────────────────────────────────────

def _error(code: str, message: str, detail: dict = None, http_status: int = 400) -> Response:
    # Every error in this file goes through here.
    # `code` is a short all-caps label the frontend can check ("LOCATION_NOT_FOUND").
    # `message` is human-readable text shown to the user.
    # `detail` holds any extra context, like suggested city names or gap distances.
    body = {"error": {"code": code, "message": message, "detail": detail or {}}}
    return Response(body, status=http_status)


# ── 3. City autocomplete ──────────────────────────────────────────────────────

class LocationsView(APIView):
    def get(self, request):
        q = request.GET.get("q", "").strip()
        limit = min(int(request.GET.get("limit", 20)), 100)  # never return more than 100 at once
        offset = max(int(request.GET.get("offset", 0)), 0)

        # If the user hasn't typed anything yet, return the 37 hand-picked major
        # US cities from locations.py. These appear the moment the input is clicked.
        if not q:
            return Response({"locations": LOCATIONS, "total": len(LOCATIONS)})

        # Once the user starts typing, search the full US cities dataset
        # (about 40,000 cities) that geocoder.py loaded into memory at startup.
        from routing.services.geocoder import _CITY_LABELS
        q_lower = q.lower()

        # Cities that START with what the user typed come first.
        # Cities that just CONTAIN the text come second.
        # So typing "Los" shows "Los Angeles" before "East Los Angeles".
        unique = sorted(set(_CITY_LABELS))
        starts = [c for c in unique if c.lower().startswith(q_lower)]
        contains = [c for c in unique if q_lower in c.lower() and not c.lower().startswith(q_lower)]
        matches = starts + contains

        total = len(matches)
        # Return one page of results. The frontend can request the next page
        # by scrolling down in the dropdown (infinite scroll).
        page = matches[offset : offset + limit]
        return Response({
            "locations": [{"label": c, "value": c} for c in page],
            "total": total,
            "offset": offset,
            "limit": limit,
        })


# ── 4. Health check ───────────────────────────────────────────────────────────

class HealthView(APIView):
    def get(self, request):
        # If the database has no fuel stations the app is running but useless —
        # every route request would return zero stops. Report that clearly.
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

        # Also report when prices were last updated so operators can tell
        # if the data is fresh or has gone stale.
        latest = FuelStation.objects.order_by("-ingested_at").values_list("ingested_at", flat=True).first()
        return Response(
            {
                "status": "ok",
                "stations_loaded": count,
                "prices_as_of": latest.isoformat() if latest else None,
            }
        )


# ── 5. Main route endpoint ────────────────────────────────────────────────────

class RouteView(APIView):
    def post(self, request):

        # ── Step 1: Check the input ───────────────────────────────────────────
        # Make sure start and finish were actually provided and are not blank.
        # We stop here immediately if not — no point doing anything else.
        ser = RouteRequestSerializer(data=request.data)
        if not ser.is_valid():
            return _error("VALIDATION_ERROR", "Invalid request body.", ser.errors)

        start_input = ser.validated_data["start"]
        finish_input = ser.validated_data["finish"]

        # ── Step 2: Turn the start city into coordinates ──────────────────────
        # The user might type "Chicago, IL" or raw coordinates like "41.85,-87.65".
        # resolve() figures out which it is and returns a latitude + longitude.
        # Three things can go wrong, each with its own clear error message:
        #   - Bad coordinates typed (e.g. "999,-200" is not a real location)
        #   - Coordinates that exist but are outside the US
        #   - A city name we don't recognise at all
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

        # ── Step 3: Turn the finish city into coordinates ─────────────────────
        # Exactly the same as step 2, just for the destination.
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

        # ── Step 4: Get the actual driving route ──────────────────────────────
        # We call an external mapping service (OSRM) with the two sets of
        # coordinates. It gives us back the total distance and the full path
        # as a series of GPS points. The result is saved for 1 hour so if
        # someone requests the same trip again we don't call OSRM a second time.
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
        route_points = route["points"]  # the GPS path as a list of lat/lng pairs

        # ── Step 5: Find fuel stations near the route ─────────────────────────
        # We have about 6,700 stations in the database. We don't want all of
        # them — only ones that are actually close to the road the truck will
        # drive. This step filters the full list down to just the nearby ones
        # and records where along the route each station sits.
        buffer_miles = getattr(settings, "CORRIDOR_BUFFER_MILES", 15)
        candidates, buffer_used = corridor_service.filter_corridor(route_points, buffer_miles)

        # ── Step 6: Pick the cheapest stops ───────────────────────────────────
        # Now that we have only the relevant stations, the optimizer decides
        # which ones to stop at and how many gallons to buy at each to minimise
        # the total fuel cost for the trip.
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

        # ── Step 7: Get the price data timestamp ──────────────────────────────
        # Grab when prices were last updated so the response can tell the user
        # how fresh the fuel price data is.
        latest = (
            FuelStation.objects.order_by("-ingested_at")
            .values_list("ingested_at", flat=True)
            .first()
        )

        # ── Step 8: Format the stop list for the response ─────────────────────
        # The optimizer returns internal objects. Here we convert them into
        # plain dictionaries with exactly the fields the frontend needs to
        # draw the table rows and place markers on the map.
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

        # ── Step 9: Save to cache and send the response ────────────────────────
        # We generate a unique token and save the full result under that token.
        # The "Open full map" link uses the token in its URL. When the map page
        # loads, it reads the data from cache using the token — so we never
        # have to recalculate the route just to show the map.
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
            "route_geometry": route["geometry"],  # the drawn path on the map
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


# ── 6 & 7. Canada border detection ───────────────────────────────────────────

def _route_transits_canada(route_points, start, finish):
    # Not implemented yet — always returns False.
    # Intended to catch routes like Detroit → Buffalo that clip into Ontario.
    return False


def _gap_transits_canada(route_points: list, gap_start: float, gap_end: float) -> bool:
    # When there are no fuel stations for a long stretch, we want to explain why.
    # This takes the middle of that empty stretch and checks: is the nearest US
    # city more than 30 miles away? If yes, the truck is probably in Canada,
    # which is why there are no stations — we only have US data.
    if not route_points:
        return False

    from routing.services.corridor import _haversine_along_route
    from routing.services import geocoder
    import numpy as np

    pts = np.array(route_points)
    cumulative = _haversine_along_route(pts)

    # Pick only the GPS points that fall inside the gap stretch.
    gap_pts = pts[(cumulative >= gap_start) & (cumulative <= gap_end)]
    if len(gap_pts) == 0:
        return False

    # Take the point in the middle of the gap as our sample location.
    sample = gap_pts[len(gap_pts) // 2]
    lat, lng = float(sample[0]), float(sample[1])

    if len(geocoder._CITY_COORDS) == 0:
        return False

    # Measure the distance from that sample point to every US city.
    # If even the nearest US city is over 30 miles away, we're in Canada.
    dists = geocoder._haversine_miles(lat, lng, geocoder._CITY_COORDS[:, 0], geocoder._CITY_COORDS[:, 1])
    min_dist = float(dists.min())
    return min_dist > 30
