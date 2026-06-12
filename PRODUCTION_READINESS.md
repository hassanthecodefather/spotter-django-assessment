# Production Readiness

---

## What this build already does right

One-command bootstrap via `docker compose up --build`. The API is versioned under `/api/v1/` from day one, so clients can be pinned without breaking when the API evolves. The health endpoint (`/api/v1/health/`) returns structured status and a station count, making it usable by load balancers and Docker healthchecks. Request IDs are assigned by middleware and surfaced in both the `X-Request-ID` response header and every log line, so individual requests can be traced across logs. Ingestion is idempotent via `--skip-if-loaded`, so container restarts are fast. The database cache backend makes map tokens work correctly across multiple gunicorn workers. All config is 12-factor: environment variables with sensible defaults, no secrets in code. The container runs as a non-root user. Every error response is structured and carries a code, which means callers can handle errors programmatically rather than parsing error strings. The test suite makes zero external network calls, which makes it safe to run in CI without special egress rules.

---

## What production actually needs that this does not have

**1. Price freshness**

The CSV is a frozen snapshot. There is no scheduled feed, no per-record timestamp, no freshness SLO, and no alerting if prices go stale. A production system needs a recurring job (say, daily) that pulls from an OPIS subscription feed, applies the same dedup-and-geocode pipeline, writes new records, and bumps a "prices_as_of" timestamp. The `ingested_at` field is already on the model and reported in every API response, giving callers honest provenance today, but the feed mechanism is absent.

**2. Real station coordinates and a detour-aware objective**

City centroids are a reasonable proxy for a coding assessment but not for production. With licensed station coordinates (OPIS or similar), the `detour_miles` field on `CandidateStop` can be populated with the actual off-route distance. Once that is done, the `effective_price` seam in the optimizer becomes `price + (detour_miles / MPG) * average_fuel_price + (detour_miles / speed) * driver_hourly_cost`, turning the greedy into a proper cost-minimizer rather than a price-minimizer. This change requires updating only `corridor.py` (to fill `detour_miles`) and `effective_price` in `fuel_optimizer.py`.

**3. Effective price per account**

Fleet operators have fuel-card discount agreements that mean the cheapest retail station is often not the cheapest for their specific account. The `effective_price` seam already supports per-stop discounts; wiring it to a per-account rate table is the next step.

**4. Routing with an SLA**

The public OSRM demo server has no SLA, rate limit, or guarantee of availability. A production deployment needs either a self-hosted OSRM instance or a licensed routing provider (HERE, Google, Mapbox). The OSRM client already has a retry-once behavior, but that is not a substitute for a real reliability guarantee. A circuit breaker and second-provider fallback would complete the picture.

**5. AuthN/AuthZ, rate limits, and quotas**

The API is currently open. Any request can plan a route and store a map token. Production needs API keys, per-account rate limits, and per-account quotas (OSRM calls cost money at scale). DRF has authentication and throttle classes ready to drop in.

**6. Observability**

Logs alone are not observability. A real system needs:
- Request-level metrics: p50/p99 latency per endpoint, broken down by phase (geocoding, OSRM, corridor filter, optimizer)
- Infrastructure metrics: OSRM cache hit ratio, station load time, worker utilization
- Alerting: when the OSRM error rate spikes, when prices have not been refreshed in 48 hours, when the health endpoint returns 503
- Distributed tracing if OSRM is self-hosted or if there is any downstream service

**7. Scale-out of the in-memory station index**

The numpy arrays in `corridor.py` are a module-level singleton loaded once per worker process at startup. At 6,700 stations this takes roughly 100ms per worker and about 2MB of RAM. At 100,000 stations it still works but takes longer and uses more memory. At that scale, a PostGIS `ST_DWithin` query with a proper spatial index is faster and does not require in-process caching. The swap point is the `filter_corridor` function's interface, which the rest of the code calls without knowing the implementation.

Cache invalidation is also absent today. If `load_stations` runs and updates the database, the in-memory arrays in the running workers are still stale until the workers restart. A production pipeline would call `reload_stations()` after each ingestion, possibly via a management command triggered by the scheduled feed.

**8. Operational data filters**

The corridor filter returns any geocoded station along the route regardless of whether it is open at the predicted arrival time, whether it has diesel fuel, whether it can accommodate trucks, or whether it stocks DEF. Filtering on open hours alone would substantially reduce the NO_STATION_IN_RANGE error rate for overnight routes.

**9. Audit trail**

The response includes `prices_as_of`, but there is no record of which algorithm version was used, which OSRM geometry was returned, or which stations were considered and rejected. For a fleet operator, reproducibility matters: if a driver asks "why did the system say to stop here instead of there?", you should be able to answer.

**10. CI/CD, load tests, and canary deploys**

There is no CI pipeline, no load test fixture, and no canary deployment strategy. Load testing should pin the LA-to-NY route to a latency budget (say, p99 under 2 seconds), fail the build if it regresses, and use a realistic station database. Canary deploys matter when the optimizer algorithm changes, since a new algorithm may produce different stop patterns even for the same route, and a gradual rollout lets you compare costs before full deployment.
