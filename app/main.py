import os
import time
import threading
import logging
import json
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import requests
from fastapi import FastAPI, Response
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
from google.transit import gtfs_realtime_pb2


# Environment
FEED_VEHICLE_POSITIONS = os.getenv("FEED_VEHICLE_POSITIONS", "")
FEED_TRIP_UPDATES = os.getenv("FEED_TRIP_UPDATES", "")
AGENCY_ID = os.getenv("AGENCY_ID", "unknown_agency")
CITY = os.getenv("CITY", "unknown_city")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
LOG_PATH = os.getenv("LOG_PATH", "/var/log/ptla/app.log")


# Logging
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logger = logging.getLogger("ptla")
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_PATH)
_handler.setLevel(logging.INFO)
logger.addHandler(_handler)


# Prometheus registry and metrics
registry = CollectorRegistry()

events_ingested = Counter(
    "ptla_events_ingested_total",
    "Total GTFS-RT events ingested",
    ["type"],
    registry=registry,
)

scrape_status = Gauge(
    "ptla_scrape_status",
    "Feed scrape status 1=success, 0=failure",
    ["feed"],
    registry=registry,
)

scrape_latency = Histogram(
    "ptla_scrape_latency_seconds",
    "Feed scrape latency seconds",
    ["feed"],
    registry=registry,
    buckets=(0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

vehicle_count_by_route = Gauge(
    "ptla_vehicle_count_by_route",
    "Number of vehicles by route",
    ["agency", "city", "route_id"],
    registry=registry,
)

headway_seconds = Gauge(
    "ptla_headway_seconds",
    "Estimated headway seconds by route",
    ["agency", "city", "route_id"],
    registry=registry,
)

arrival_delay_seconds = Gauge(
    "ptla_arrival_delay_seconds",
    "Arrival delay seconds by route/trip/stop",
    ["agency", "city", "route_id", "trip_id", "stop_id"],
    registry=registry,
)

on_time_ratio = Gauge(
    "ptla_on_time_ratio",
    "On-time ratio (|delay| <= 60s) by route",
    ["agency", "city", "route_id"],
    registry=registry,
)


app = FastAPI(title="Public Transport Live Arrivals (PTLA)")


state_lock = threading.Lock()
last_vehicle_positions_fetch: Optional[float] = None
last_trip_updates_fetch: Optional[float] = None


def now_ts() -> float:
    return time.time()


def _fetch_feed(url: str, feed_name: str) -> Optional[gtfs_realtime_pb2.FeedMessage]:
    if not url:
        scrape_status.labels(feed=feed_name).set(0)
        return None
    start = now_ts()
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)
        scrape_status.labels(feed=feed_name).set(1)
        scrape_latency.labels(feed=feed_name).observe(now_ts() - start)
        return feed
    except Exception:
        scrape_status.labels(feed=feed_name).set(0)
        scrape_latency.labels(feed=feed_name).observe(now_ts() - start)
        return None


def _estimate_headway_seconds(timestamps: List[int]) -> float:
    if len(timestamps) < 2:
        return 0.0
    ts_sorted = sorted(timestamps)
    diffs = [b - a for a, b in zip(ts_sorted[:-1], ts_sorted[1:]) if b >= a]
    if not diffs:
        return 0.0
    diffs_sorted = sorted(diffs)
    mid = len(diffs_sorted) // 2
    if len(diffs_sorted) % 2 == 1:
        return float(diffs_sorted[mid])
    return float((diffs_sorted[mid - 1] + diffs_sorted[mid]) / 2.0)


def _log_snapshot(route_stats: Dict[str, Dict[str, float]]):
    doc = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "ptla_snapshot",
        "agency": AGENCY_ID,
        "city": CITY,
        "routes": [
            {
                "route_id": route_id,
                "vehicle_count": stats.get("vehicle_count", 0),
                "on_time_ratio": stats.get("on_time_ratio", 0.0),
                "headway_seconds": stats.get("headway_seconds", 0.0),
            }
            for route_id, stats in sorted(route_stats.items())
        ],
    }
    try:
        logger.info(json.dumps(doc))
    except Exception:
        # Avoid crashing on logging issues
        pass


def poll_loop():
    global last_vehicle_positions_fetch, last_trip_updates_fetch

    while True:
        route_vehicle_timestamps: Dict[str, List[int]] = {}
        route_vehicle_counts: Dict[str, int] = {}
        route_on_time_counts: Dict[str, Tuple[int, int]] = {}

        # Vehicle Positions
        vp_feed = _fetch_feed(FEED_VEHICLE_POSITIONS, "vehicle_positions")
        if vp_feed is not None:
            with state_lock:
                last_vehicle_positions_fetch = now_ts()
            vp_entities = vp_feed.entity
            events_ingested.labels(type="vehicle_positions").inc(len(vp_entities))
            for ent in vp_entities:
                if not ent.HasField("vehicle"):
                    continue
                vehicle = ent.vehicle
                route_id = vehicle.trip.route_id if vehicle.trip and vehicle.trip.route_id else ""
                ts = 0
                if vehicle.timestamp:
                    ts = int(vehicle.timestamp)
                elif vp_feed.header and vp_feed.header.timestamp:
                    ts = int(vp_feed.header.timestamp)
                route_vehicle_timestamps.setdefault(route_id, []).append(ts)
                route_vehicle_counts[route_id] = route_vehicle_counts.get(route_id, 0) + 1

        # Trip Updates
        tu_feed = _fetch_feed(FEED_TRIP_UPDATES, "trip_updates")
        if tu_feed is not None:
            with state_lock:
                last_trip_updates_fetch = now_ts()
            tu_entities = tu_feed.entity
            events_ingested.labels(type="trip_updates").inc(len(tu_entities))
            for ent in tu_entities:
                if not ent.HasField("trip_update"):
                    continue
                tu = ent.trip_update
                route_id = tu.trip.route_id if tu.trip and tu.trip.route_id else ""
                trip_id = tu.trip.trip_id if tu.trip and tu.trip.trip_id else ent.id or ""
                for stu in tu.stop_time_update:
                    stop_id = stu.stop_id or ""
                    delay = None
                    if stu.arrival and stu.arrival.delay is not None:
                        delay = int(stu.arrival.delay)
                    elif stu.departure and stu.departure.delay is not None:
                        delay = int(stu.departure.delay)
                    if delay is None:
                        continue
                    arrival_delay_seconds.labels(
                        agency=AGENCY_ID, city=CITY, route_id=route_id, trip_id=trip_id, stop_id=stop_id
                    ).set(delay)
                    # On-time accounting
                    total_ok, total_all = route_on_time_counts.get(route_id, (0, 0))
                    total_all += 1
                    if abs(delay) <= 60:
                        total_ok += 1
                    route_on_time_counts[route_id] = (total_ok, total_all)

        # Update per-route gauges
        route_stats: Dict[str, Dict[str, float]] = {}

        for route_id, count in route_vehicle_counts.items():
            vehicle_count_by_route.labels(agency=AGENCY_ID, city=CITY, route_id=route_id).set(count)
            route_stats.setdefault(route_id, {})["vehicle_count"] = float(count)

        for route_id, ts_list in route_vehicle_timestamps.items():
            hw = _estimate_headway_seconds(ts_list)
            headway_seconds.labels(agency=AGENCY_ID, city=CITY, route_id=route_id).set(hw)
            route_stats.setdefault(route_id, {})["headway_seconds"] = float(hw)

        for route_id, (ok_count, total_count) in route_on_time_counts.items():
            ratio = (ok_count / total_count) if total_count > 0 else 0.0
            on_time_ratio.labels(agency=AGENCY_ID, city=CITY, route_id=route_id).set(ratio)
            route_stats.setdefault(route_id, {})["on_time_ratio"] = float(ratio)

        _log_snapshot(route_stats)

        time.sleep(POLL_SECONDS)


@app.get("/")
def root():
    return {
        "name": "Public Transport Live Arrivals (PTLA)",
        "metrics": "/metrics",
        "health": "/health",
        "agency": AGENCY_ID,
        "city": CITY,
    }


@app.get("/health")
def health():
    with state_lock:
        vp = last_vehicle_positions_fetch
        tu = last_trip_updates_fetch
    return {
        "status": "ok",
        "last_vehicle_positions_fetch": vp,
        "last_trip_updates_fetch": tu,
    }


@app.get("/metrics")
def metrics():
    data = generate_latest(registry)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


def _start_background_poller():
    t = threading.Thread(target=poll_loop, name="poll_loop", daemon=True)
    t.start()


_start_background_poller()



