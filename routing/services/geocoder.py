"""Offline geocoder using the bundled uscities.csv dataset.

All lookups are pure in-memory after module load. No external API calls.
"""

import math
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
USCITIES_CSV = DATA_DIR / "uscities.csv"

MAX_SNAP_MILES = float(os.environ.get("MAX_SNAP_MILES", "30"))

STATE_ABBREVS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
}

_CITY_LOOKUP: dict = {}
_CITY_COORDS: np.ndarray = np.empty((0, 2))
_CITY_LABELS: list = []


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    # "st." before a space or end-of-string, and standalone "st" → "saint"
    text = re.sub(r"\bst\.(\s|$)", r"saint\1", text)
    text = re.sub(r"\bst\b", "saint", text)
    return text


def _build_cache():
    global _CITY_LOOKUP, _CITY_COORDS, _CITY_LABELS
    df = pd.read_csv(USCITIES_CSV, dtype=str)
    df = df.dropna(subset=["city", "state_id", "lat", "lng"])
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lng"] = pd.to_numeric(df["lng"], errors="coerce")
    df = df.dropna(subset=["lat", "lng"])

    lookup = {}
    coords = []
    labels = []

    for _, row in df.iterrows():
        city_norm = _normalize(row["city"])
        state = row["state_id"].strip().upper()
        lat = float(row["lat"])
        lng = float(row["lng"])
        key = (city_norm, state)
        if key not in lookup:
            lookup[key] = (lat, lng)
        coords.append([lat, lng])
        labels.append(f"{row['city']}, {state}")

    _CITY_LOOKUP = lookup
    _CITY_COORDS = np.array(coords, dtype=np.float64)
    _CITY_LABELS = labels


_build_cache()


@dataclass(frozen=True)
class ResolvedLocation:
    lat: float
    lng: float
    resolved_city: str
    snap_distance_miles: float


class LocationNotFound(Exception):
    def __init__(self, query: str, suggestions: list = None):
        self.query = query
        self.suggestions = suggestions or []
        super().__init__(f"Location not found: {query}")


class LocationOutsideUSA(Exception):
    def __init__(self, query: str, nearest_city: str, distance_miles: float):
        self.query = query
        self.nearest_city = nearest_city
        self.distance_miles = distance_miles
        super().__init__(
            f"{query!r} is {distance_miles:.1f} miles from nearest US city ({nearest_city})"
        )


class InvalidCoordinates(ValueError):
    pass


_EARTH_RADIUS_MILES = 3958.8


def _haversine_miles(lat1: float, lng1: float, lats: np.ndarray, lngs: np.ndarray) -> np.ndarray:
    r = _EARTH_RADIUS_MILES
    dlat = np.radians(lats - lat1)
    dlng = np.radians(lngs - lng1)
    a = (
        np.sin(dlat / 2) ** 2
        + np.cos(np.radians(lat1)) * np.cos(np.radians(lats)) * np.sin(dlng / 2) ** 2
    )
    return 2 * r * np.arcsin(np.sqrt(a))


def _nearest_city(lat: float, lng: float):
    if len(_CITY_COORDS) == 0:
        raise LocationNotFound(f"{lat},{lng}")
    dists = _haversine_miles(lat, lng, _CITY_COORDS[:, 0], _CITY_COORDS[:, 1])
    idx = int(np.argmin(dists))
    return _CITY_LABELS[idx], float(dists[idx]), _CITY_COORDS[idx]


def _try_parse_coords(text: str):
    parts = text.split(",")
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].strip())
        lng = float(parts[1].strip())
    except ValueError:
        return None
    return lat, lng


def _coords_valid(lat: float, lng: float):
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def _suggest_cities(city_input: str, state: str) -> list:
    import difflib
    candidates = [k[0] for k in _CITY_LOOKUP if k[1] == state]
    return difflib.get_close_matches(city_input.lower(), candidates, n=3, cutoff=0.5)


def resolve(location: str) -> ResolvedLocation:
    """Resolve a location string to a canonical US city coordinate.

    Accepted formats:
      - "lat,lng" (e.g., "34.05,-118.24")
      - "City, ST" (e.g., "Los Angeles, CA")
      - "City, StateName" (e.g., "Los Angeles, California")

    Raises LocationOutsideUSA if coordinates snap to > MAX_SNAP_MILES from any US city.
    Raises LocationNotFound if city-name lookup fails.
    Raises InvalidCoordinates for malformed coordinate input.
    """
    text = location.strip()

    parsed = _try_parse_coords(text)
    if parsed is not None:
        lat, lng = parsed
        if not _coords_valid(lat, lng):
            hint = None
            rev_parsed = _try_parse_coords(f"{lng},{lat}")
            if rev_parsed and _coords_valid(rev_parsed[0], rev_parsed[1]):
                nearest, dist, _ = _nearest_city(rev_parsed[0], rev_parsed[1])
                if dist <= MAX_SNAP_MILES:
                    hint = f"coordinates may be swapped; try {lng},{lat}"
            raise InvalidCoordinates(
                f"Invalid coordinates: {text!r}",
                hint,
            )
        nearest_label, dist_miles, nearest_coords = _nearest_city(lat, lng)
        if dist_miles > MAX_SNAP_MILES:
            raise LocationOutsideUSA(text, nearest_label, dist_miles)
        return ResolvedLocation(
            lat=float(nearest_coords[0]),
            lng=float(nearest_coords[1]),
            resolved_city=nearest_label,
            snap_distance_miles=round(dist_miles, 2),
        )

    parts = text.rsplit(",", 1)
    if len(parts) == 2:
        city_part = parts[0].strip()
        state_part = parts[1].strip()

        state_up = state_part.upper()
        if len(state_up) == 2:
            state_id = state_up
        else:
            state_id = STATE_ABBREVS.get(state_part.lower())

        if state_id:
            city_norm = _normalize(city_part)
            key = (city_norm, state_id)
            if key in _CITY_LOOKUP:
                lat, lng = _CITY_LOOKUP[key]
                return ResolvedLocation(
                    lat=lat,
                    lng=lng,
                    resolved_city=f"{city_part}, {state_id}",
                    snap_distance_miles=0.0,
                )

            fuzzy = _normalize(
                re.sub(r"[^a-z0-9 ]", "", city_part.lower())
            )
            key_fuzzy = (fuzzy, state_id)
            if key_fuzzy in _CITY_LOOKUP:
                lat, lng = _CITY_LOOKUP[key_fuzzy]
                return ResolvedLocation(
                    lat=lat,
                    lng=lng,
                    resolved_city=f"{city_part}, {state_id}",
                    snap_distance_miles=0.0,
                )

            suggestions = _suggest_cities(city_part, state_id)
            raise LocationNotFound(text, suggestions=suggestions)

    raise LocationNotFound(text)
