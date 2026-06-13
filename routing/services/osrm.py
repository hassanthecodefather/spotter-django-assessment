"""Asks an external mapping service for the driving route between two points.

FILE NAVIGATION — follow these in order:
  1. Settings at the top     — Where the mapping service lives and how long
                               to wait before giving up on it.
  2. get_route()             — Start here. This is the only function views.py
                               calls. It checks if we already have this route
                               saved, and if not, goes and fetches it.
  3. _fetch_route()          — Does the actual work: builds the URL, sends the
                               request, reads the response, and pulls out the
                               distance and the GPS path.
  4. _request_with_retry()   — Handles the network call. If it fails once it
                               waits half a second and tries again before
                               giving up completely.
"""

import logging
import os
import time

import polyline
import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)


# ── 1. Settings ───────────────────────────────────────────────────────────────

# The address of the mapping service. We use the public demo server by default.
# In production, set the OSRM_BASE_URL environment variable to point at a
# private server so we are not rate-limited by the public one.
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
TIMEOUT_SECONDS = 10        # give up waiting for a response after 10 seconds
RETRY_BACKOFF_SECONDS = 0.5  # wait this long before the second attempt


class RoutingUnavailable(Exception):
    """The mapping service is down or not responding."""


class RouteNotFound(Exception):
    """The mapping service is up, but it cannot find a road between these two points."""


# ── 2. Public function — this is what views.py calls ─────────────────────────

def get_route(lat1: float, lng1: float, lat2: float, lng2: float) -> dict:
    """Get the driving route between two coordinates.

    Returns a dict with:
      - distance_miles: total trip length
      - geometry: the route path in a compact format for the map
      - points: the same path as a plain list of (lat, lng) pairs
    """
    # Before calling the mapping service, check if we have already fetched
    # this exact route recently. If yes, return the saved copy immediately.
    # This means the second request for Chicago → New York is instant.
    key = _cache_key(lat1, lng1, lat2, lng2)
    cached = cache.get(key)
    if cached is not None:
        return cached

    # No saved copy — go fetch it and save it for the next hour.
    result = _fetch_route(lat1, lng1, lat2, lng2)
    cache.set(key, result, timeout=3600)
    return result


def _cache_key(lat1, lng1, lat2, lng2) -> str:
    # Build a unique name for this coordinate pair so we can store and look
    # it up in the cache. We round to 4 decimal places so that tiny differences
    # in the same city's coordinates still hit the same saved entry.
    return "osrm:{:.4f},{:.4f};{:.4f},{:.4f}".format(lat1, lng1, lat2, lng2)


# ── 3. Fetch and parse the route ──────────────────────────────────────────────

def _fetch_route(lat1, lng1, lat2, lng2) -> dict:
    # Note: the mapping service expects coordinates in longitude, latitude order
    # (the opposite of the usual lat, lng). This is easy to get backwards.
    url = (
        f"{OSRM_BASE_URL}/route/v1/driving/"
        f"{lng1},{lat1};{lng2},{lat2}"
        "?overview=full&geometries=polyline"
        # overview=full      → give us the complete path, not just a summary
        # geometries=polyline → compress the path into a compact string format
    )
    response = _request_with_retry(url)
    data = response.json()

    # The mapping service tells us success or failure through a "code" field
    # in the response body, not just through the HTTP status code.
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RouteNotFound(
            f"Mapping service returned code={data.get('code')!r} for "
            f"({lat1},{lng1})->({lat2},{lng2})"
        )

    route = data["routes"][0]

    # Distance comes back in metres — convert to miles.
    distance_miles = route["distance"] / 1609.344

    # The path comes back as a compact encoded string. We decode it into a
    # plain list of GPS coordinates so corridor.py can work with it directly.
    # We keep the original encoded string too so the frontend map can draw it.
    geometry = route["geometry"]
    points = polyline.decode(geometry)
    return {
        "distance_miles": distance_miles,
        "geometry": geometry,
        "points": [(lat, lng) for lat, lng in points],
    }


# ── 4. Network call with one retry ────────────────────────────────────────────

def _request_with_retry(url: str) -> requests.Response:
    # Try the request up to twice. On the first failure we pause briefly
    # and try once more. If it fails a second time we give up.
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=TIMEOUT_SECONDS)

            # Any response below 500 means the server replied normally —
            # even a 4xx error is a real answer, not a temporary glitch.
            if resp.status_code < 500:
                return resp

            # A 500+ response means the server had an internal problem.
            # That might be temporary, so try once more.
            if attempt == 0:
                logger.warning("Mapping service returned %d, retrying after backoff", resp.status_code)
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise RoutingUnavailable(f"Mapping service returned HTTP {resp.status_code}")

        except requests.exceptions.Timeout:
            # The server took too long to respond. Try once more.
            if attempt == 0:
                logger.warning("Mapping service timed out, retrying")
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise RoutingUnavailable("Mapping service did not respond in time")

        except requests.exceptions.ConnectionError as exc:
            # Could not reach the server at all. Try once more.
            if attempt == 0:
                logger.warning("Could not connect to mapping service, retrying: %s", exc)
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise RoutingUnavailable(f"Could not connect to mapping service: {exc}") from exc

    raise RoutingUnavailable("Mapping service is unreachable")
