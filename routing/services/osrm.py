"""OSRM routing client. Makes exactly one external HTTP call per route request."""

import logging
import os
import time

import polyline
import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
TIMEOUT_SECONDS = 10
RETRY_BACKOFF_SECONDS = 0.5


class RoutingUnavailable(Exception):
    """OSRM is unreachable or returned a server error."""


class RouteNotFound(Exception):
    """OSRM responded but could not find a drivable route (e.g. island, ocean crossing)."""


def _cache_key(lat1, lng1, lat2, lng2) -> str:
    return "osrm:{:.4f},{:.4f};{:.4f},{:.4f}".format(lat1, lng1, lat2, lng2)


def get_route(lat1: float, lng1: float, lat2: float, lng2: float) -> dict:
    """Return route data from OSRM, using a 1-hour cache to avoid repeat calls.

    Returns a dict with keys:
      - distance_miles: float
      - geometry: encoded polyline string
      - points: list of (lat, lng) tuples decoded from the polyline
    """
    key = _cache_key(lat1, lng1, lat2, lng2)
    cached = cache.get(key)
    if cached is not None:
        return cached

    result = _fetch_route(lat1, lng1, lat2, lng2)
    cache.set(key, result, timeout=3600)
    return result


def _fetch_route(lat1, lng1, lat2, lng2) -> dict:
    url = (
        f"{OSRM_BASE_URL}/route/v1/driving/"
        f"{lng1},{lat1};{lng2},{lat2}"
        "?overview=full&geometries=polyline"
    )
    response = _request_with_retry(url)
    data = response.json()

    if data.get("code") != "Ok" or not data.get("routes"):
        raise RouteNotFound(
            f"OSRM returned code={data.get('code')!r} for "
            f"({lat1},{lng1})->({lat2},{lng2})"
        )

    route = data["routes"][0]
    distance_miles = route["distance"] / 1609.344
    geometry = route["geometry"]
    points = polyline.decode(geometry)
    return {
        "distance_miles": distance_miles,
        "geometry": geometry,
        "points": [(lat, lng) for lat, lng in points],
    }


def _request_with_retry(url: str) -> requests.Response:
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=TIMEOUT_SECONDS)
            if resp.status_code < 500:
                return resp
            if attempt == 0:
                logger.warning("OSRM returned %d, retrying after backoff", resp.status_code)
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise RoutingUnavailable(f"OSRM returned HTTP {resp.status_code}")
        except requests.exceptions.Timeout:
            if attempt == 0:
                logger.warning("OSRM timed out, retrying")
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise RoutingUnavailable("OSRM request timed out after retry")
        except requests.exceptions.ConnectionError as exc:
            if attempt == 0:
                logger.warning("OSRM connection error, retrying: %s", exc)
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise RoutingUnavailable(f"OSRM connection error: {exc}") from exc
    raise RoutingUnavailable("OSRM unreachable")
