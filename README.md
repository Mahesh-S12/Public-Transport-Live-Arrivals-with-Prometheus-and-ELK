## Public Transport Live Arrivals (PTLA)

Real-time transport observability stack that ingests GTFS‑Realtime feeds, exposes operational metrics to Prometheus, and ships metrics/logs to Elasticsearch for analysis in Kibana. One-command startup via Docker Compose. No synthetic data.

### What this project does

- Ingests live GTFS‑RT VehiclePositions and TripUpdates from a public transit agency
- Calculates useful operational KPIs (vehicle counts, headways, delays, on‑time ratio)
- Exposes metrics in Prometheus format at `/metrics`
- Scrapes those metrics with Prometheus and forwards them to Elasticsearch via Metricbeat
- Writes structured JSON snapshots to a log file, shipped to Elasticsearch via Filebeat
- Visualizes time‑series in Kibana (7.17.x)

### Architecture

- **App (`FastAPI` + `prometheus_client`)**: Polls GTFS‑RT feeds every `POLL_SECONDS`, parses Protobuf messages, updates Prometheus metrics, and emits JSON snapshots to `/var/log/ptla/app.log`.
- **Prometheus**: Scrapes `app:8000/metrics` to validate metrics locally.
- **Metricbeat (Prometheus module)**: Scrapes the same `/metrics` endpoint and publishes time‑series to Elasticsearch.
- **Filebeat**: Tails the JSON log file and ships documents to Elasticsearch.
- **Elasticsearch + Kibana (7.17.14)**: Stores and visualizes data.

Services are defined in `docker-compose.yml`. Beats images are built locally to bake config files inside the images (avoids Windows bind‑mount permission checks).

### Real‑time data source (GTFS‑RT)

- Format: GTFS‑Realtime (Protobuf). Two feeds are used:
  - `VehiclePositions.pb`: real‑time vehicle locations/timestamps per route
  - `TripUpdates.pb`: real‑time trip and stop time updates (arrival/departure delays)
- Defaults provided (you can replace them):
  - `FEED_VEHICLE_POSITIONS=https://cdn.mbta.com/realtime/VehiclePositions.pb`
  - `FEED_TRIP_UPDATES=https://cdn.mbta.com/realtime/TripUpdates.pb`
- Configuration lives in `docker-compose.yml` under the `app` service (no `.env`). If your agency requires API keys or headers, front the feed with a small proxy or adjust the app to add headers.

Data usage note: Always review the agency’s Open Data terms before use. Many agencies allow non‑commercial/attribution use and may require API keys. Replace the example URLs with your agency’s feeds as needed.

### Quick start (no .env files)

1) Configure feeds and context in `docker-compose.yml` → service `app` → `environment`:

```
FEED_VEHICLE_POSITIONS=<your_vehicle_positions_url>
FEED_TRIP_UPDATES=<your_trip_updates_url>
AGENCY_ID=<your_agency_id>
CITY=<your_city>
POLL_SECONDS=15
```

Example MBTA values are already present.

2) Launch the stack:

```bash
docker compose up -d --build
```

3) Open services:
- App: http://localhost:8000 (health at `/health`, metrics at `/metrics`)
- Prometheus: http://localhost:9090 (Status → Targets)
- Elasticsearch: http://localhost:9200
- Kibana: http://localhost:5601

### Prometheus metrics exposed by the app

- `ptla_vehicle_count_by_route{agency,city,route_id}`: Vehicles seen per route in the last poll.
- `ptla_headway_seconds{agency,city,route_id}`: Naive headway estimate per route (median gap of observed vehicle timestamps).
- `ptla_arrival_delay_seconds{agency,city,route_id,trip_id,stop_id}`: Current observed delay for stops from TripUpdates.
- `ptla_on_time_ratio{agency,city,route_id}`: Fraction of observations with |delay| ≤ 60s for the current poll cycle.
- `ptla_scrape_status{feed}`: 1 if last feed fetch succeeded, else 0.
- `ptla_scrape_latency_seconds{feed}`: Histogram of feed fetch latency.
- `ptla_events_ingested_total{type}`: Counter of ingested GTFS‑RT entities.

### Structured JSON logs shipped by Filebeat

The app writes snapshots at each poll to `/var/log/ptla/app.log` (shared volume `ptla-logs`). Each line is a JSON document like:

```
{
  "@timestamp": "2025-10-31T08:00:00Z",
  "type": "ptla_snapshot",
  "agency": "mbta",
  "city": "boston",
  "routes": [
    {"route_id": "Red", "vehicle_count": 12, "on_time_ratio": 0.83, "headway_seconds": 300}
  ]
}
```

Filebeat parses JSON (`beats/filebeat.yml`) and publishes to Elasticsearch.

### From Prometheus to Elasticsearch (Metricbeat)

Metricbeat’s Prometheus module scrapes `http://app:8000/metrics` every 15s and publishes documents to `metricbeat-*` indices. This lets you analyze and visualize the same metrics in Kibana.

### Kibana setup

- Create data views:
  - `metricbeat-*` (time field: `@timestamp`)
  - `filebeat-*` (time field: `@timestamp`) — once logs appear
- Example visualizations (Lens):
  - Headway by route (line): from `prometheus.ptla_headway_seconds` grouped by `labels.route_id`
  - On‑time ratio by route (area): from `prometheus.ptla_on_time_ratio` grouped by `labels.route_id`
  - Arrival delay heatmap: from `prometheus.ptla_arrival_delay_seconds` across `labels.route_id` × `labels.stop_id`, metric: avg `value`
  - Vehicle counts by route (bar): from `prometheus.ptla_vehicle_count_by_route` grouped by `labels.route_id`

### Windows notes (Beats config permissions)

On Windows bind‑mounted config files appear world‑writable inside Linux containers, causing Beats to exit with:

```
error loading config file: config file ("filebeat.yml") can only be writable by the owner
```

This project builds small wrapper images (`beats/filebeat.Dockerfile`, `beats/metricbeat.Dockerfile`) that COPY the configs into the image, avoiding the permission check. `docker compose up -d --build` handles this automatically.

### Troubleshooting

- Check container status: `docker compose ps`
- Tail logs:
  - App: `docker compose logs -f app`
  - Filebeat: `docker compose logs -f filebeat`
  - Metricbeat: `docker compose logs -f metricbeat`
  - Elasticsearch/Kibana: `docker compose logs -f elasticsearch kibana`
- Verify indices: `GET http://localhost:9200/_cat/indices?v`
- Verify Metricbeat docs: `GET http://localhost:9200/metricbeat-*/_count`
- Verify Filebeat docs: `GET http://localhost:9200/filebeat-*/_count`
- Reset stack: `docker compose down -v && docker compose up -d --build`

### Notes

- Replace the example GTFS‑RT URLs with your agency’s feeds as needed; many agencies require API keys/headers. Do not use synthetic data.
- Label cardinality can be high for `trip_id`/`stop_id`; adjust retention or filter metrics as appropriate for your Prometheus/ES capacity.


