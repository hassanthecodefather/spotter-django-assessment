# Design Decisions

## Open Source Used

| Component | What we use it for |
|---|---|
| Django 6.0 | Web framework, ORM, management commands |
| Django REST Framework | API views, serializers, exception handling |
| PostgreSQL 16 | Primary database and cache store |
| gunicorn | WSGI server, multi-worker process management |
| WhiteNoise | Static file serving without a separate nginx |
| numpy | Vectorized haversine for corridor filtering |
| pandas | CSV ingestion and data cleaning |
| polyline | Decoding OSRM encoded polyline on the server |
| dj-database-url | 12-factor DATABASE_URL parsing |
| psycopg (v3) | PostgreSQL driver |
| requests | OSRM HTTP client |
| OSRM (router.project-osrm.org) | Driving route and geometry (one call per request) |
| OpenStreetMap / Leaflet | Client-side map tiles and route rendering |
| US Census Bureau Gazetteer | Offline city coordinate dataset (32K cities, public domain) |

---

## Key Decisions

### Offline geocoding
- One external call per request is already spent on OSRM, so a geocoding API is off limits
- City-centroid precision is enough: fuel routing is a corridor problem, not an address problem
- US membership check: snap to nearest entry in `uscities.csv`, reject if snap distance > 30 miles
- No bounding box anywhere — a rectangle does not describe the US

### Greedy refueling algorithm
- Drive to the cheapest reachable station, buy just enough to reach the next cheaper one
- Provably optimal for fixed-range refueling
- Common wrong answer is "pick the N cheapest stations near the route" — ignores reachability entirely

### Coordinate snapping
- Raw `lat,lng` input is snapped to the nearest `uscities.csv` entry
- Keeps city-name and coordinate inputs on the same canonical path
- 30-mile threshold rejects Toronto (across Lake Ontario) while accepting any populated US area

### Database cache backend
- Two gunicorn workers share one Postgres cache table
- Map tokens written by worker A are visible to worker B
- `LocMemCache` would work locally and silently break in production

### numpy corridor filter instead of PostGIS
- 6,700 stations, vectorized haversine: sub-millisecond
- PostGIS adds significant operational overhead for one query
- Swap point is roughly 100,000 stations — only the `filter_corridor` interface needs to change

### `effective_price` seam
- Optimizer calls `price_fn(stop)` not `stop.price` directly
- Drop-in extension point for detour cost, fuel-card discounts, time cost
- Zero changes to the greedy loop to add any of these

### DTL + vanilla JS
- No build step, no npm, no framework surface to maintain
- All server-to-JS data goes through Django's `json_script` filter (XSS protection)
- Station names never reach the DOM via string interpolation

### Canadian rows excluded
- 620 rows with province codes (BC, ON, AB, etc.) dropped at ingestion
- Avoids polluting geocode hit rate with guaranteed failures
- Routes transiting Canada raise `NO_STATION_IN_RANGE` with a note explaining why

---

## Assumptions

- Vehicle starts with a full tank (500 miles / 50 gallons)
- Duplicate station IDs: keep lowest price row
- Tank capacity and max range are equivalent in this model (50 gal at 10 MPG)
- Optimizer only stops at stations inside the corridor — real off-route detours are out of scope

---

## What is Production Ready

- One-command bootstrap (`docker compose up --build`)
- API versioned under `/api/v1/` from day one
- Structured error responses with machine-readable codes
- Request ID on every log line and response header
- 12-factor config (all settings from env vars, no secrets in code)
- Container runs as non-root user
- Health endpoint usable by load balancers
- Idempotent ingestion (`--skip-if-loaded`)
- Multi-worker safe cache (DB backend)
- Zero external calls in test suite (safe to run in CI with no egress)
- Retry-once OSRM client with backoff

---

## What is Not Production Ready

- **Price data is a frozen CSV snapshot** — no scheduled feed, no staleness alerting
- **City centroid coordinates** — not real station GPS; detour cost is hardcoded 0
- **OSRM public demo server** — no SLA, no rate limit guarantees
- **No auth or rate limiting** — API is fully open
- **No CI/CD pipeline** — no automated build, test, or deploy on push
- **In-memory station index goes stale** — workers must restart after re-ingestion
- **No metrics or alerting** — logs only, no p99 latency tracking or error rate dashboards
- **No audit trail** — can't replay "why did we pick this stop" after the fact
- **No open-hours or fuel-type filtering** — a station closed at 2am still appears as a candidate

---

## Given More Time and Resources

- **Live price feed** — OPIS subscription ingested daily, `ingested_at` already in the model
- **Real station coordinates** — populate `detour_miles` on `CandidateStop`, wire it into `effective_price`
- **Fleet fuel-card discounts** — per-account rate table plugged into the `effective_price` seam
- **Self-hosted OSRM** — swap `OSRM_BASE_URL`, add circuit breaker and secondary-provider fallback
- **Auth and quotas** — DRF throttle classes, API keys, per-account limits
- **PostGIS** — replace numpy corridor filter when station count exceeds ~100k
- **Observability** — Prometheus metrics per phase (geocode, OSRM, corridor, optimizer), Grafana dashboard, staleness alert
- **CI/CD** — GitHub Actions: lint, test, Docker build, load test against LA-to-NY p99 budget
- **Canada support** — ingest Canadian station data, remove province exclusion, handle border-crossing routes
- **Canary deploys** — roll out algorithm changes gradually, compare stop patterns and costs before full release
