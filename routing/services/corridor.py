"""Narrows 6,700 nationwide stations down to only those near the route.

HOW IT WORKS IN THREE STEPS:
  1. filter_corridor()     ← views.py calls this. Give it the route, get back nearby stations.
  2. _filter_at_buffer()   ← does the actual work: measures each station's distance to the road.
  3. preload_stations()    ← runs once at startup, puts all stations in memory so step 2 is fast.

Everything else in this file is math that supports those three steps.
"""

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

_EARTH_RADIUS_MILES = 3958.8
_MIN_SPACING_MILES = 3.0


# All 6,700 stations live here after the first request.
# We load them once from the database and reuse them forever — no DB hit per request.
_station_coords: np.ndarray = np.empty((0, 2))  # lat/lng numbers only, for fast math
_station_ids: list = []                          # full station details, looked up after filtering
_cache_loaded = False


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CandidateStop:
    """A station that passed the distance filter.

    The important field added here is route_position_miles.
    It answers: "at which mile of the trip is this station?"
    That single number is what lets the optimizer sort stops in driving order.
    """
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lng: float
    route_position_miles: float  # mile marker along the route
    detour_miles: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# The three math helpers below are used inside _filter_at_buffer().
# You don't need to read them line by line — just understand what each one answers.
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_miles_matrix(lats1, lngs1, lats2, lngs2):
    # ANSWERS: "how many miles is each station from each point on the route?"
    # Returns a grid — one row per station, one column per route point.
    # Every cell is the distance between that station and that route point.
    lats1_r = np.radians(lats1)[:, None]
    lats2_r = np.radians(lats2)[None, :]
    dlat = lats2_r - lats1_r
    dlng = np.radians(lngs2)[None, :] - np.radians(lngs1)[:, None]
    a = np.sin(dlat / 2) ** 2 + np.cos(lats1_r) * np.cos(lats2_r) * np.sin(dlng / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _haversine_along_route(pts):
    # ANSWERS: "how far from the start is each GPS point on the route?"
    # Returns a running total — e.g. [0, 12.3, 31.7, 55.2 ...] miles.
    if len(pts) < 2:
        return np.zeros(len(pts))
    dlat = np.radians(np.diff(pts[:, 0]))
    dlng = np.radians(np.diff(pts[:, 1]))
    mlat = np.radians((pts[:-1, 0] + pts[1:, 0]) / 2)
    a = np.sin(dlat / 2) ** 2 + np.cos(mlat) ** 2 * np.sin(dlng / 2) ** 2
    segs = 2 * _EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    return np.concatenate([[0.0], np.cumsum(segs)])


def _downsample_route(points, min_spacing=_MIN_SPACING_MILES):
    # ANSWERS: "what if we kept only one GPS point every 3 miles?"
    # The mapping service returns thousands of points for a long trip.
    # We only need one every 3 miles — still accurate enough, much faster to process.
    if len(points) <= 2:
        return list(points), np.array([0.0, _haversine_along_route(np.array(points))[-1]])

    pts = np.array(points)
    cumulative = _haversine_along_route(pts)
    kept_pts = [pts[0]]
    kept_dists = [0.0]
    last_kept_dist = 0.0

    for i in range(1, len(pts)):
        if cumulative[i] - last_kept_dist >= min_spacing:
            kept_pts.append(pts[i])
            kept_dists.append(cumulative[i])
            last_kept_dist = cumulative[i]

    # Always keep the destination even if the last segment is under 3 miles.
    if len(kept_pts) == 0 or not np.allclose(kept_pts[-1], pts[-1]):
        kept_pts.append(pts[-1])
        kept_dists.append(cumulative[-1])

    return np.array(kept_pts), np.array(kept_dists)


# ─────────────────────────────────────────────────────────────────────────────

def preload_stations():
    """Load all stations from the database into memory. Runs once, then skips itself."""
    global _station_coords, _station_ids, _cache_loaded
    if _cache_loaded:
        return  # already loaded — nothing to do

    from routing.models import FuelStation

    qs = FuelStation.objects.filter(
        lat__isnull=False, lng__isnull=False  # skip stations with no location data
    ).values("opis_id", "name", "address", "city", "state", "retail_price", "lat", "lng")

    rows = list(qs)
    if not rows:
        _cache_loaded = True
        return

    _station_ids = rows
    _station_coords = np.array([[r["lat"], r["lng"]] for r in rows], dtype=np.float64)
    _cache_loaded = True
    logger.info("Loaded %d stations into corridor cache", len(rows))


def reload_stations():
    """Force a fresh load from the database. Called after new price data is ingested."""
    global _cache_loaded
    _cache_loaded = False
    preload_stations()


# ─────────────────────────────────────────────────────────────────────────────

def filter_corridor(route_points, buffer_miles=None):
    """Main entry point. Takes the route, returns only stations close to it.

    Starts with a 15-mile search band around the road.
    If that leaves any part of the route uncovered, widens to 30, then 50 miles.
    """
    if buffer_miles is None:
        import django.conf
        buffer_miles = getattr(django.conf.settings, "CORRIDOR_BUFFER_MILES", 15)

    preload_stations()

    if len(_station_ids) == 0:
        return [], buffer_miles

    for buf in _candidate_buffers(buffer_miles):
        candidates = _filter_at_buffer(route_points, buf)
        if candidates:
            return candidates, buf

    return [], buffer_miles


def _candidate_buffers(initial):
    # Try the configured width first, then fall back to wider bands if needed.
    seen = set()
    for b in [initial, 30.0, 50.0]:
        if b not in seen:
            seen.add(b)
            yield b


def _filter_at_buffer(route_points, buffer_miles):
    """Find all stations within buffer_miles of the route and tag each with its mile marker."""

    # Thin the route to one point every 3 miles — fast enough, accurate enough.
    sampled_pts, sampled_dists = _downsample_route(route_points)

    # Measure every station's closest distance to any point on the route.
    dist_matrix = _haversine_miles_matrix(
        _station_coords[:, 0], _station_coords[:, 1],
        sampled_pts[:, 0],     sampled_pts[:, 1],
    )

    # For each station: how close does it get to the road, and at which mile?
    min_dists = dist_matrix.min(axis=1)
    nearest_route_idx = dist_matrix.argmin(axis=1)

    # Keep only stations within the buffer, and stamp each with its mile marker.
    candidates = []
    for i, close_enough in enumerate(min_dists <= buffer_miles):
        if not close_enough:
            continue
        row = _station_ids[i]
        mile_marker = float(sampled_dists[int(nearest_route_idx[i])])
        candidates.append(
            CandidateStop(
                opis_id=row["opis_id"],
                name=row["name"],
                address=row["address"],
                city=row["city"],
                state=row["state"],
                price=float(row["retail_price"]),
                lat=row["lat"],
                lng=row["lng"],
                route_position_miles=mile_marker,
                detour_miles=0.0,
            )
        )

    # Return in driving order so the optimizer can walk the list front to back.
    candidates.sort(key=lambda s: s.route_position_miles)
    return candidates
