"""Replay recorded GPS into the live endpoint, to demo the live layer without a live feed.

    python -m busgps.replay --gps data/synth/gps.csv.gz --date 2026-11-05 --start 07:30 --end 08:30
"""
import argparse

import httpx
import pandas as pd

from .pipeline import load_gps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gps", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--start", default="07:30")
    ap.add_argument("--end", default="08:30")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--tz", type=float, default=5.5)
    a = ap.parse_args()
    g = load_gps(a.gps)
    local = pd.to_datetime(g.timestamp + a.tz * 3600, unit="s")
    t0, t1 = pd.Timestamp(f"{a.date} {a.start}"), pd.Timestamp(f"{a.date} {a.end}")
    g = g[(local >= t0) & (local < t1)].sort_values("timestamp")
    httpx.post(f"{a.api}/live/reset")
    rows = g.to_dict("records")
    for i in range(0, len(rows), 1000):
        chunk = [dict(vehicle_id=r["vehicle_id"], route_id=r["route_id"], latitude=r["latitude"],
                      longitude=r["longitude"], timestamp=r["timestamp"]) for r in rows[i:i + 1000]]
        httpx.post(f"{a.api}/live/pings", json=chunk, timeout=60)
    print(httpx.get(f"{a.api}/live/status").json())


if __name__ == "__main__":
    main()
