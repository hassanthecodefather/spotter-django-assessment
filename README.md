# Fuel Route Optimizer

A Django backend that finds the cheapest fuel stops for a long-haul US driving route. You give it a start and finish location; it calls OSRM once to get the route, finds stations within 15 miles of the road, and runs a greedy fixed-range algorithm to decide where to stop and how many gallons to buy. The web UI shows a Leaflet map with numbered markers and a summary table.

Django version: **6.0.6** (latest stable as of June 2026).

---

## Quick start

```bash
docker compose up --build
```

Then open http://localhost:8000. That's it. The container runs migrations, creates the cache table, and loads all fuel stations on first boot. Subsequent restarts skip the load because of `--skip-if-loaded`.

### Local dev (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createcachetable
python manage.py load_stations
python manage.py runserver
```

Running tests (SQLite, no Docker needed):

```bash
python manage.py test routing.tests
```

---

## API reference

### POST /api/v1/route/

Plan a fuel-optimized route.

```bash
curl -s -X POST http://localhost:8000/api/v1/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Los Angeles, CA", "finish": "New York, NY"}'
```

Response (abbreviated):

```json
{
  "start": {"input": "Los Angeles, CA", "lat": 34.05266, "lng": -118.24289, "resolved_city": "Los Angeles, CA", "snap_distance_miles": 0.0},
  "finish": {"input": "New York, NY", "lat": 40.71274, "lng": -74.00597, "resolved_city": "New York, NY", "snap_distance_miles": 0.0},
  "total_distance_miles": 2789.4,
  "route_geometry": "<encoded polyline>",
  "prices_as_of": "2026-06-11T12:00:00+00:00",
  "corridor_buffer_miles": 15,
  "fuel": {
    "mpg": 10,
    "max_range_miles": 500,
    "total_gallons": 278.9,
    "total_fuel_cost_usd": 838.52,
    "stops": [
      {
        "opis_id": 7,
        "name": "WOODSHED OF BIG CABIN",
        "address": "I-44, EXIT 283 & US-69",
        "city": "Big Cabin",
        "state": "OK",
        "price_per_gallon": 3.00733,
        "route_position_miles": 412.6,
        "gallons_purchased": 41.2,
        "cost_usd": 123.90,
        "lat": 36.53,
        "lng": -95.22
      }
    ]
  },
  "map_url": "/map/?token=<uuid>"
}
```

Measured timing (LA to NY, i7 laptop on a home connection):

```
Cold request (OSRM call + station filter + optimizer):
  real  0m0.871s

Cached request (OSRM result from Django cache):
  real  0m0.043s
```

### GET /api/v1/health/

Returns `{"status": "ok", "stations_loaded": 6820, "prices_as_of": "..."}`.
Returns 503 if no stations are loaded.

### GET /map/?token=UUID

Shareable Leaflet map page for a recently planned route. Tokens expire after 1 hour.

### Error codes

| Code | HTTP | Meaning |
|------|------|---------|
| `LOCATION_NOT_FOUND` | 400 | City name not in the US cities dataset. Response includes `suggestions`. |
| `LOCATION_OUTSIDE_USA` | 400 | Coordinates resolve to a point more than 30 miles from any US city. |
| `INVALID_COORDINATES` | 400 | Malformed coordinate string. May include a `hint` if coordinates look swapped. |
| `ROUTE_NOT_FOUND` | 422 | OSRM found no drivable path (Catalina Island, Hawaii, etc.). |
| `NO_STATION_IN_RANGE` | 422 | A segment of the route has no fuel station within range. May include a note about Canada transit. |
| `ROUTING_UNAVAILABLE` | 502 | OSRM timed out or returned a server error. |
| `MAP_EXPIRED` | 404 | Map token is unknown or has expired. |

---

## Free APIs used

**OSRM public demo server** (router.project-osrm.org): routing, free, no API key required. Set `OSRM_BASE_URL` in `.env` to swap in a self-hosted instance.

**OpenStreetMap tiles via Leaflet CDN**: map rendering, free, client-side tile loads only. These do not count against the one-external-call budget.

---

## Configuration

All settings come from environment variables. Copy `.env.example` to `.env` before running locally.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | (required in prod) | Django secret key |
| `DEBUG` | `True` | Django debug mode |
| `DATABASE_URL` | (SQLite fallback) | Postgres connection string |
| `OSRM_BASE_URL` | public server | OSRM endpoint |
| `MAX_SNAP_MILES` | `30` | Max distance to snap a lat,lng input to the nearest US city |
| `CORRIDOR_BUFFER_MILES` | `15` | Starting corridor width around the route |
| `CSRF_TRUSTED_ORIGINS` | localhost | Origins allowed for CSRF |
