"""Record the Delhi GTFS-RT VehiclePositions feed to daily CSV files the pipeline can read.

    export TRANSPORT_STACK_API_KEY=...        # never commit the key
    python -m busgps.poll_rt --once --gtfs data/gtfs          # inspect the feed, check route_id matches
    python -m busgps.poll_rt --out data/real --every 10       # record until Ctrl-C

Writes data/real/gps_YYYY-MM-DD.csv with vehicle_id,route_id,latitude,longitude,timestamp
(unix seconds, UTC). Leave it running on a machine that stays on; history only exists for the
time you record. The pipeline accepts a glob: --gps "data/real/gps_*.csv".
"""
import argparse
import csv
import datetime as dt
import os
import sys
import time

import requests
from google.transit import gtfs_realtime_pb2

URL = "https://dts-backend.transportstack.in/api/dataset/otd/get-file"
PARAMS = {"agency": "delhi-buses", "category": "realtime_gtfs", "filename": "VehiclePositions.pb"}
FIELDS = ["vehicle_id", "route_id", "latitude", "longitude", "timestamp"]


def parse(content):
    """Bytes of a VehiclePositions.pb -> list of row dicts."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    hdr_ts = feed.header.timestamp
    rows = []
    for e in feed.entity:
        if not e.HasField("vehicle"):
            continue
        v = e.vehicle
        if not v.HasField("position"):
            continue
        vid = v.vehicle.id or v.vehicle.label or e.id
        rows.append(dict(vehicle_id=vid, route_id=v.trip.route_id, latitude=v.position.latitude,
                         longitude=v.position.longitude, timestamp=int(v.timestamp or hdr_ts)))
    return rows


def fetch(key):
    r = requests.get(URL, params=PARAMS, headers={"x-api-key": key}, timeout=30)
    r.raise_for_status()
    return parse(r.content)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/real")
    ap.add_argument("--every", type=float, default=10.0, help="seconds between requests")
    ap.add_argument("--once", action="store_true", help="fetch once, print a summary, write nothing")
    ap.add_argument("--api", help="also forward every new ping to a running API, e.g. http://127.0.0.1:8000")
    ap.add_argument("--no-save", action="store_true", help="forward to the API only, write no CSV")
    ap.add_argument("--gtfs", help="static GTFS folder, to check how many route_ids match routes.txt")
    a = ap.parse_args()
    key = os.environ.get("TRANSPORT_STACK_API_KEY")
    if not key:
        sys.exit("Set TRANSPORT_STACK_API_KEY first.")

    if a.once:
        rows = fetch(key)
        ids = {r["route_id"] for r in rows}
        print(f"{len(rows)} vehicles, {len(ids)} distinct route_ids, "
              f"{sum(1 for r in rows if not r['route_id'])} with empty route_id")
        print("sample:", rows[:3])
        age = time.time() - max((r["timestamp"] for r in rows), default=0)
        print(f"newest position is {age:.0f}s old")
        if a.gtfs:
            import pandas as pd
            known = set(pd.read_csv(os.path.join(a.gtfs, "routes.txt"), dtype=str).route_id)
            hit = ids & known
            print(f"{len(hit)} of {len(ids)} feed route_ids exist in routes.txt; examples missing: {list(ids - known)[:5]}")
        return

    os.makedirs(a.out, exist_ok=True)
    seen = {}
    n = 0
    while True:
        t0 = time.time()
        try:
            rows = fetch(key)
        except Exception as ex:                      # keep recording through network blips
            print("fetch failed:", ex, file=sys.stderr)
            time.sleep(a.every)
            continue
        new = [r for r in rows if seen.get(r["vehicle_id"]) != r["timestamp"]]
        for r in new:
            seen[r["vehicle_id"]] = r["timestamp"]
        if new and a.api:
            try:
                requests.post(a.api.rstrip("/") + "/live/pings", json=new, timeout=20)
            except Exception as ex:
                print("API forward failed:", ex, file=sys.stderr)
        if new and not a.no_save:
            day = dt.datetime.fromtimestamp(max(r["timestamp"] for r in new), dt.timezone(dt.timedelta(hours=5.5))).strftime("%Y-%m-%d")
            path = os.path.join(a.out, f"gps_{day}.csv")
            fresh = not os.path.exists(path)
            with open(path, "a", newline="") as fh:
                w = csv.DictWriter(fh, FIELDS)
                if fresh:
                    w.writeheader()
                w.writerows(new)
            n += len(new)
        print(f"{dt.datetime.now():%H:%M:%S} {len(rows)} vehicles, {len(new)} new, {n} total", flush=True)
        time.sleep(max(0.0, a.every - (time.time() - t0)))


if __name__ == "__main__":
    main()
