"""Synthetic GTFS + GPS generator.

SYNTHETIC DATA ONLY. A toy 6-route grid network with made-up congestion, signals, dwell,
GPS noise and outliers. It exists so the pipeline, API and UI can be run and tested
end to end without the real Transport Stack datasets. Numbers produced from it say
nothing about real accuracy.

    python -m busgps.synth --out data/synth
"""
import argparse
import os

import numpy as np
import pandas as pd

from .geo import Frame

LAT0, LON0 = 28.57, 77.18
L = 9000.0
LANE = 12.5
ROADS = {"H0": ("h", 0), "H1": ("h", 3000), "H2": ("h", 6000),
         "V0": ("v", 0), "V1": ("v", 3000), "V2": ("v", 6000)}
STOP_S = 100.0 + 500.0 * np.arange(18)
SIGNALS = np.array([3000.0, 6000.0])
SCHED_SPEED = 25 / 3.6
INCIDENT_DATE, INCIDENT_ROUTE = "2026-11-05", "V1"


def xy_at(kind, c, d, sp):
    sp = np.asarray(sp, float)
    phys = sp if d == 0 else L - sp
    lane = -LANE if d == 0 else LANE
    return (phys, c + lane + 0 * phys) if kind == "h" else (c + lane + 0 * phys, phys)


def hms(sec):
    sec = int(round(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def time_factor(h, weekday, d):
    if weekday:
        am = (1.3 if d == 0 else 0.8) * 0.42 * np.exp(-((h - 9.0) / 1.2) ** 2)
        pm = (1.3 if d == 1 else 0.8) * 0.45 * np.exp(-((h - 18.3) / 1.5) ** 2)
        return np.clip(1 - am - pm, 0.25, 1)
    return 1 - 0.15 * np.exp(-((h - 12) / 3) ** 2)


def schedule(weekday):
    deps, t = [], 6 * 3600
    while t <= 22 * 3600:
        deps.append(t)
        h = t / 3600
        t += (20 if weekday and (7 <= h < 10 or 17 <= h < 20) else 30) * 60
    return deps


def write_gtfs(out, frame):
    stops, routes, trips, st, shp = [], [], [], [], []
    for r, (kind, c) in ROADS.items():
        routes.append(dict(route_id=r, route_short_name=r, route_long_name=f"Synthetic corridor {r}", route_type=3))
        for d in (0, 1):
            sid = f"S_{r}_{d}"
            pts = np.arange(0, L + 1, 500.0)
            x, y = xy_at(kind, c, d, pts)
            lat, lon = frame.to_ll(x, y)
            for i in range(len(pts)):
                shp.append(dict(shape_id=sid, shape_pt_lat=lat[i], shape_pt_lon=lon[i], shape_pt_sequence=i))
            sx, sy = xy_at(kind, c, d, STOP_S)
            slat, slon = frame.to_ll(sx, sy)
            for k in range(len(STOP_S)):
                stops.append(dict(stop_id=f"{r}_{d}_{k:02d}", stop_name=f"{r} {'outbound' if d == 0 else 'inbound'} stop {k + 1}",
                                  stop_lat=slat[k], stop_lon=slon[k]))
            for svc, wd in (("WD", True), ("WE", False)):
                for i, dep in enumerate(schedule(wd)):
                    tid = f"{r}_{d}_{svc}_{i}"
                    trips.append(dict(route_id=r, service_id=svc, trip_id=tid, direction_id=d, shape_id=sid))
                    for k, s in enumerate(STOP_S):
                        tt = hms(dep + s / SCHED_SPEED)
                        st.append(dict(trip_id=tid, arrival_time=tt, departure_time=tt,
                                       stop_id=f"{r}_{d}_{k:02d}", stop_sequence=k + 1))
    for name, rows in (("stops", stops), ("routes", routes), ("trips", trips), ("stop_times", st), ("shapes", shp)):
        pd.DataFrame(rows).to_csv(os.path.join(out, f"{name}.txt"), index=False)


def run_trip(r, d, date, wd, dep, rng, ping):
    kind, c = ROADS[r]
    edges = np.arange(0, L + 1, 50.0)
    mids = (edges[:-1] + edges[1:]) / 2
    h = (dep + mids / 7.0) / 3600
    v = 8.5 * time_factor(h, wd, d) * rng.lognormal(0, 0.12) * rng.lognormal(0, 0.10, len(mids))
    phys = mids if d == 0 else L - mids
    v = v * np.where((phys > 2500) & (phys < 3500), 0.7, 1.0) * np.where((phys > 5500) & (phys < 6500) & wd, 0.85, 1.0)
    if date == INCIDENT_DATE and r == INCIDENT_ROUTE:
        v = v * np.where((8 <= h) & (h < 10) & (phys > 4000) & (phys < 5000), 0.35, 1.0)
    T0 = np.r_[0, np.cumsum(50.0 / v)]
    peak = float(1 - time_factor(dep / 3600 + 0.3, wd, d))
    ev_s = np.r_[STOP_S, SIGNALS]
    order = np.argsort(ev_s)
    ev_s = ev_s[order]
    delay = np.r_[8 + 20 * peak + rng.exponential(6, len(STOP_S)),
                  rng.uniform(0, 50, len(SIGNALS)) * (rng.random(len(SIGNALS)) < 0.6)][order]
    cd = np.r_[0, np.cumsum(delay)]
    shift = cd[np.searchsorted(ev_s, edges, side="left")]
    t_edges = dep + T0 + shift
    ei = (ev_s / 50).astype(int)
    t_dep = dep + T0[ei] + cd[1:]
    t = np.r_[t_edges, t_dep]
    s = np.r_[edges, ev_s]
    o = np.argsort(t, kind="stable")
    t, s = t[o], s[o]
    tp = np.arange(np.ceil(dep + rng.uniform(0, ping)), t[-1], ping)
    return tp, np.interp(tp, t, s), (kind, c)


def simulate(out, seed=7, start="2026-10-19", days=21, ping=15):
    os.makedirs(out, exist_ok=True)
    rng = np.random.default_rng(seed)
    frame = Frame(LAT0, LON0)
    write_gtfs(out, frame)
    frames = []
    for day in pd.date_range(start, periods=days):
        date = day.strftime("%Y-%m-%d")
        wd = day.weekday() < 5
        midnight_utc = int(day.value // 10**9) - 19800  # IST midnight as unix UTC
        for r in ROADS:
            for d in (0, 1):
                for i, dep0 in enumerate(schedule(wd)):
                    dep = max(dep0 + rng.normal(0, 100), 5 * 3600)
                    tp, sp, (kind, c) = run_trip(r, d, date, wd, dep, rng, ping)
                    x, y = xy_at(kind, c, d, sp)
                    x = x + rng.normal(0, 4, len(x))
                    y = y + rng.normal(0, 4, len(y))
                    bad = rng.random(len(x)) < 0.004
                    x = x + bad * rng.choice([-1, 1], len(x)) * rng.uniform(150, 400, len(x))
                    keep = rng.random(len(x)) > 0.02
                    lat, lon = frame.to_ll(x, y)
                    ts = (midnight_utc + tp + rng.integers(-1, 2, len(tp))).astype(np.int64)
                    veh = f"{r}-{d}-{i % 6}"
                    f = pd.DataFrame(dict(vehicle_id=veh, route_id=r, latitude=lat, longitude=lon, timestamp=ts))[keep]
                    frames.append(f)
                    dup = f.sample(frac=0.01, random_state=int(rng.integers(1e9)))
                    frames.append(dup)
    gps = pd.concat(frames, ignore_index=True)
    gps.to_csv(os.path.join(out, "gps.csv.gz"), index=False, float_format="%.6f")
    with open(os.path.join(out, "SYNTHETIC.txt"), "w") as fh:
        fh.write("Synthetic data generated by busgps.synth. Not real. Do not report accuracy from it.\n")
    return len(gps)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/synth")
    ap.add_argument("--days", type=int, default=21)
    a = ap.parse_args()
    n = simulate(a.out, days=a.days)
    print(f"wrote GTFS + {n:,} GPS rows to {a.out}")
