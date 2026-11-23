import argparse
import glob

import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--gps", default="data/real/gps_*.csv")
ap.add_argument("--gtfs", default=None)
ap.add_argument("--tz", type=float, default=5.5)
a = ap.parse_args()
files = sorted(glob.glob(a.gps))
g = pd.concat([pd.read_csv(f) for f in files], ignore_index=True).drop_duplicates(["vehicle_id", "timestamp"])
t = pd.to_datetime(g.timestamp + a.tz * 3600, unit="s")
g["date"], g["hour"], g["wd"] = t.dt.strftime("%Y-%m-%d"), t.dt.hour, t.dt.day_name().str[:3]
print(f"{len(files)} files, {len(g):,} pings, {g.vehicle_id.nunique():,} vehicles, {g.route_id.nunique():,} routes")
print(f"from {t.min()} to {t.max()}  (Delhi time)")
print("\npings per day:")
print(g.groupby(["date", "wd"]).size().to_string())
print("\npings per hour of day (all days):")
print(g.groupby("hour").size().to_string())
if a.gtfs:
    known = set(pd.read_csv(f"{a.gtfs}/routes.txt", dtype=str).route_id)
    ok = g.route_id.astype(str).isin(known)
    print(f"\n{ok.mean() * 100:.1f}% of pings have a route_id that exists in routes.txt")