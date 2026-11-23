"""Static GTFS loader. Builds route-direction objects with shape geometry, stop positions
along the shape, and timetable-derived speed and headways."""
import math
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .geo import Frame, cumlen, project

SEG = 250.0     # segment length in metres
GATE = 40.0     # max distance from the route shape for a GPS point to be accepted
STOP_R = 40.0   # radius around a stop where slow movement counts as dwell


def parse_t(x):
    h, m, s = str(x).split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


@dataclass
class RouteDir:
    route_id: str
    direction: int
    shape_xy: np.ndarray
    cum: np.ndarray
    length: float
    stop_ids: list
    stop_s: np.ndarray
    sched_speed: float          # m/s implied by the timetable
    sched_hw: dict              # hour -> scheduled headway in minutes
    name: str = ""

    @property
    def nseg(self):
        return max(1, int(math.ceil(self.length / SEG)))

    def seg_of(self, s):
        return np.minimum((np.asarray(s) // SEG).astype(int), self.nseg - 1)

    def stop_near(self, s1, s2):
        """Index of the stop both s1 and s2 are within STOP_R of, else -1 (vectorised)."""
        ss = self.stop_s
        m = (np.asarray(s1) + np.asarray(s2)) / 2
        i = np.searchsorted(ss, m)
        i0, i1 = np.clip(i - 1, 0, len(ss) - 1), np.clip(i, 0, len(ss) - 1)
        j = np.where(abs(ss[i0] - m) <= abs(ss[i1] - m), i0, i1)
        ok = (abs(ss[j] - s1) <= STOP_R) & (abs(ss[j] - s2) <= STOP_R)
        return np.where(ok, j, -1)


def stop_positions(pts, shape_xy, cum, tie_m=0.0, max_gap=4000.0, near_m=80.0):
    """Distance along the shape for each stop, taken in order so the sequence never goes backwards.
    Plain nearest-point projection puts stops on the wrong pass of routes that loop or double back."""
    a, b = shape_xy[:-1], shape_xy[1:]
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-9)
    out = np.empty(len(pts))
    start, prev = 0, 0.0
    for i, p in enumerate(pts):
        def search(lo, hi):
            ap = p - a[lo:hi]
            t = np.clip((ap * ab[lo:hi]).sum(1) / L2[lo:hi], 0, 1)
            d = np.sqrt(((p - (a[lo:hi] + t[:, None] * ab[lo:hi])) ** 2).sum(1))
            k = int(np.where(d <= d.min() + tie_m)[0][0])
            return lo + k, float(t[k]), float(d[k])
        hi = int(np.searchsorted(cum, prev + max_gap, side="right"))
        hi = min(max(hi, start + 1), len(ab))
        j, t, d = search(start, hi)
        if d > near_m and hi < len(ab):
            j, t, d = search(start, len(ab))
        sj = max(float(cum[j] + t * np.sqrt(L2[j])), prev)
        out[i], prev, start = sj, sj, j
    return out


class Network:
    def __init__(self, stops, rds, frame, route_names):
        self.stops, self.rd, self.frame, self.route_names = stops, rds, frame, route_names
        self.routes = {}
        for (r, d) in rds:
            self.routes.setdefault(r, []).append(d)
        for r in self.routes:
            self.routes[r].sort()

    def project_ping(self, route_id, lat, lon):
        """All candidate (direction, s, distance) for one ping."""
        x, y = self.frame.to_xy([lat], [lon])
        p = np.c_[x, y]
        out = []
        for d in self.routes.get(route_id, []):
            rd = self.rd[(route_id, d)]
            s, dist = project(p, rd.shape_xy, rd.cum)
            out.append((d, float(s[0]), float(dist[0])))
        return out


def load_gtfs(path):
    rdcsv = lambda f: pd.read_csv(os.path.join(path, f), dtype=str)
    stops, routes, trips = rdcsv("stops.txt"), rdcsv("routes.txt"), rdcsv("trips.txt")
    st, shapes = rdcsv("stop_times.txt"), rdcsv("shapes.txt")

    stops["lat"] = stops.stop_lat.astype(float)
    stops["lon"] = stops.stop_lon.astype(float)
    frame = Frame(stops.lat.mean(), stops.lon.mean())
    stops["x"], stops["y"] = frame.to_xy(stops.lat.values, stops.lon.values)
    stops["name"] = stops.stop_name
    stops = stops.set_index("stop_id")

    shp = {}
    for sid, g in shapes.groupby("shape_id"):
        g = g.assign(q=g.shape_pt_sequence.astype(int)).sort_values("q")
        x, y = frame.to_xy(g.shape_pt_lat.astype(float).values, g.shape_pt_lon.astype(float).values)
        shp[sid] = np.c_[x, y]

    if "direction_id" not in trips:
        trips["direction_id"] = "0"
    trips["direction_id"] = trips.direction_id.fillna("0").astype(int)
    if "service_id" not in trips:
        trips["service_id"] = "all"

    st["seq"] = st.stop_sequence.astype(int)
    st = st.sort_values(["trip_id", "seq"])
    first_dep = st.groupby("trip_id").departure_time.first().map(parse_t)
    st_idx = st.set_index("trip_id")

    names = dict(zip(routes.route_id, routes.get("route_short_name", routes.route_id)))
    rds = {}
    fallbacks = []
    for (rid, d), g in trips.groupby(["route_id", "direction_id"]):
        shape_id = g.shape_id.mode().iloc[0]
        if shape_id not in shp:
            continue
        gg = g[g.shape_id == shape_id]
        xy = shp[shape_id]
        cum = cumlen(xy)
        rep = st_idx.loc[gg.trip_id.iloc[0]]
        sids = list(rep.stop_id)
        sx, sy = stops.loc[sids, "x"].values, stops.loc[sids, "y"].values
        spts = np.c_[sx, sy]
        s = stop_positions(spts, xy, cum)
        # Sanity check: a shape that does not follow its stops would collapse the stop distances.
        hop = float(np.sqrt((np.diff(spts, axis=0) ** 2).sum(1)).sum()) if len(spts) > 1 else 0.0
        _, off = project(spts, xy, cum)
        if len(spts) > 2 and (s[-1] - s[0] < 0.6 * hop or float(np.median(off)) > 150):
            xy = spts                                   # fall back to the stop-to-stop polyline
            cum = cumlen(xy)
            s = cum.copy()
            fallbacks.append((rid, int(d)))
        t0, t1 = parse_t(rep.departure_time.iloc[0]), parse_t(rep.arrival_time.iloc[-1])
        speed = (s[-1] - s[0]) / (t1 - t0) if t1 > t0 and s[-1] > s[0] else 5.0
        svc = gg.service_id.mode().iloc[0]
        deps = first_dep.loc[gg[gg.service_id == svc].trip_id]
        counts = (deps // 3600).value_counts()
        hw = {int(h): 60.0 / c for h, c in counts.items()}
        rds[(rid, int(d))] = RouteDir(rid, int(d), xy, cum, float(cum[-1]), sids, s, float(speed), hw,
                                     str(names.get(rid, rid)))
    net = Network(stops, rds, frame, names)
    net.shape_fallbacks = fallbacks
    return net
