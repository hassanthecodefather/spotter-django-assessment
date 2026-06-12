"""Corridor filter: find fuel stations within N miles of a route polyline."""

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

_EARTH_RADIUS_MILES = 3958.8
_MIN_SPACING_MILES = 3.0

_station_coords: np.ndarray = np.empty((0, 2))
_station_ids: list = []
_cache_loaded = False


@dataclass
class CandidateStop:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lng: float
    route_position_miles: float
    detour_miles: float = 0.0


def _haversine_miles_matrix(
    lats1: np.ndarray, lngs1: np.ndarray, lats2: np.ndarray, lngs2: np.ndarray
) -> np.ndarray:
    """Vectorized haversine. Returns (M, N) matrix where M=len(lats1), N=len(lats2)."""
    lats1_r = np.radians(lats1)[:, None]
    lats2_r = np.radians(lats2)[None, :]
    dlat = lats2_r - lats1_r
    dlng = np.radians(lngs2)[None, :] - np.radians(lngs1)[:, None]
    a = np.sin(dlat / 2) ** 2 + np.cos(lats1_r) * np.cos(lats2_r) * np.sin(dlng / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def _haversine_along_route(
    pts: np.ndarray,
) -> np.ndarray:
    """Cumulative distance along a list of (lat, lng) route points, in miles."""
    if len(pts) < 2:
        return np.zeros(len(pts))
    dlat = np.radians(np.diff(pts[:, 0]))
    dlng = np.radians(np.diff(pts[:, 1]))
    mlat = np.radians((pts[:-1, 0] + pts[1:, 0]) / 2)
    a = np.sin(dlat / 2) ** 2 + np.cos(mlat) ** 2 * np.sin(dlng / 2) ** 2
    segs = 2 * _EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    return np.concatenate([[0.0], np.cumsum(segs)])


def _downsample_route(points: list, min_spacing: float = _MIN_SPACING_MILES):
    """Keep one point every min_spacing miles along the route."""
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

    if len(kept_pts) == 0 or not np.allclose(kept_pts[-1], pts[-1]):
        kept_pts.append(pts[-1])
        kept_dists.append(cumulative[-1])

    return np.array(kept_pts), np.array(kept_dists)


def preload_stations():
    """Load all geocoded stations into module-level numpy arrays."""
    global _station_coords, _station_ids, _cache_loaded
    if _cache_loaded:
        return

    from routing.models import FuelStation

    qs = FuelStation.objects.filter(
        lat__isnull=False, lng__isnull=False
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
    """Force a cache refresh (called after load_stations management command)."""
    global _cache_loaded
    _cache_loaded = False
    preload_stations()


def filter_corridor(
    route_points: list,
    buffer_miles: float = None,
) -> tuple:
    """Return (candidates, buffer_used_miles) for stations near the route.

    Tries buffer_miles first, then widens to 30, then 50 if any 500-mile
    window has no candidate.
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


def _candidate_buffers(initial: float):
    seen = set()
    for b in [initial, 30.0, 50.0]:
        if b not in seen:
            seen.add(b)
            yield b


def _filter_at_buffer(route_points: list, buffer_miles: float) -> list:
    sampled_pts, sampled_dists = _downsample_route(route_points)

    station_lats = _station_coords[:, 0]
    station_lngs = _station_coords[:, 1]

    route_lats = sampled_pts[:, 0]
    route_lngs = sampled_pts[:, 1]

    dist_matrix = _haversine_miles_matrix(
        station_lats, station_lngs, route_lats, route_lngs
    )

    min_dists = dist_matrix.min(axis=1)
    nearest_route_idx = dist_matrix.argmin(axis=1)

    mask = min_dists <= buffer_miles
    candidates = []

    for i, in_corridor in enumerate(mask):
        if not in_corridor:
            continue
        row = _station_ids[i]
        nearest_idx = int(nearest_route_idx[i])
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
                route_position_miles=float(sampled_dists[nearest_idx]),
                detour_miles=0.0,
            )
        )

    candidates.sort(key=lambda s: s.route_position_miles)
    return candidates
