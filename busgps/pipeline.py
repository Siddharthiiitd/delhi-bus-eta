"""Raw GPS + static GTFS -> trained SpeedModel.

    python -m busgps.pipeline --gtfs data/synth --gps data/synth/gps.csv.gz --out artifacts/synth \
        --train-until 2026-11-05

Steps: dedupe -> project onto route shapes (both directions, nearest wins, smoothed per vehicle)
-> reject off-route pings -> consecutive-ping samples -> separate stop dwell from road time ->
hierarchical pace tables -> dwell and headway tables -> coverage audit.
"""
import argparse
import datetime as dt
import glob
import json
import os
import pickle

import numpy as np
import pandas as pd

try:                       # H3 is optional: its compiled library may be blocked on locked-down machines
    import h3
except Exception:
    h3 = None

from .geo import bearing_bucket, point_at, project
from .model import SpeedModel
from .network import GATE, SEG, load_gtfs

MIN_N, MIN_DAYS = 6, 3     # bus passes and distinct days needed to trust a table cell
H3_RES = 9
MAX_SPEED = 22.0           # m/s (about 80 km/h), faster is treated as a GPS jump


def load_gps(path, cols=None):
    files = sorted(glob.glob(path)) or [path]       # a file, or a glob such as "data/real/gps_*.csv"
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    if cols:
        df = df.rename(columns=cols)
    df = df[["vehicle_id", "route_id", "latitude", "longitude", "timestamp"]].copy()
    df["vehicle_id"], df["route_id"] = df.vehicle_id.astype(str), df.route_id.astype(str)
    return df


def prepare(gps, net, tz=5.5, q=None):
    """Clean pings and attach (direction, distance along route, local time fields)."""
    q = {} if q is None else q
    q["raw_rows"] = int(len(gps))
    g = gps.dropna().drop_duplicates(["vehicle_id", "timestamp"])
    q["duplicate_or_null_rows"] = int(len(gps) - len(g))
    known = g.route_id.isin(net.routes)
    q["unknown_route_rows"] = int((~known).sum())
    g = g[known]
    parts = []
    for route, grp in g.groupby("route_id"):
        x, y = net.frame.to_xy(grp.latitude.values, grp.longitude.values)
        P = np.c_[x, y]
        part = pd.DataFrame({"vehicle_id": grp.vehicle_id.values, "route_id": route, "timestamp": grp.timestamp.values})
        for d in (0, 1):
            if (route, d) in net.rd:
                rd = net.rd[(route, d)]
                s, dist = project(P, rd.shape_xy, rd.cum)
            else:
                s, dist = np.zeros(len(P)), np.full(len(P), np.inf)
            part[f"s{d}"], part[f"d{d}"] = s, dist
        parts.append(part)
    df = pd.concat(parts, ignore_index=True)
    df = df.sort_values(["vehicle_id", "route_id", "timestamp"], kind="stable").reset_index(drop=True)
    df["dir_raw"] = np.where(df.d0 <= df.d1, 0, 1)
    df["dir"] = df.groupby(["vehicle_id", "route_id"])["dir_raw"].transform(
        lambda x: x.rolling(9, center=True, min_periods=1).median().round().astype(int))
    df["s"] = np.where(df.dir == 0, df.s0, df.s1)
    df["dist"] = np.where(df.dir == 0, df.d0, df.d1)
    off = df.dist > GATE
    q["off_route_rows"] = int(off.sum())
    df = df[~off].copy()
    df["t"] = df.timestamp.astype(float) + tz * 3600
    ts = pd.to_datetime(df.t, unit="s")
    df["date"] = ts.dt.strftime("%Y-%m-%d")
    df["daytype"] = np.where(ts.dt.weekday < 5, "wd", "we")
    return df[["vehicle_id", "route_id", "dir", "s", "t", "date", "daytype"]].reset_index(drop=True)


def make_pairs(df, net, q=None):
    """Consecutive ping pairs for the same vehicle, route and direction."""
    q = {} if q is None else q
    df = df.sort_values(["vehicle_id", "route_id", "dir", "t"], kind="stable").reset_index(drop=True)
    k = ["vehicle_id", "route_id", "dir"]
    same = (df[k] == df[k].shift()).all(axis=1)
    pr = df.shift()
    dt_, ds = df.t - pr.t, df.s - pr.s
    time_ok = (dt_ >= 3) & (dt_ <= 90)
    speed_ok = (ds >= -30) & (ds / dt_ <= MAX_SPEED)
    q["pairs_candidate"] = int(same.sum())
    q["pairs_dropped_time_gap"] = int((same & ~time_ok).sum())
    q["pairs_dropped_speed_outlier"] = int((same & time_ok & ~speed_ok).sum())
    ok = same & time_ok & speed_ok
    P = pd.DataFrame({"vehicle_id": df.vehicle_id, "route_id": df.route_id, "dir": df.dir,
                      "s0": pr.s, "s1": df.s, "t0": pr.t, "dt": dt_, "ds": ds,
                      "date": df.date, "daytype": df.daytype})[ok].copy()
    tm = P.t0 + P.dt / 2
    P["slot"] = ((tm % 86400) // 900).astype(int)
    parts = []
    for (r, d), grp in P.groupby(["route_id", "dir"]):
        rd = net.rd[(r, int(d))]
        sm = np.clip((grp.s0.values + grp.s1.values) / 2, 0, rd.length)
        j = rd.stop_near(grp.s0.values, grp.s1.values)
        slow = (np.abs(grp.ds.values) / grp.dt.values) <= 1.0
        parts.append(grp.assign(seg=rd.seg_of(sm), stop=np.where(slow, j, -1)))
    return pd.concat(parts, ignore_index=True)


def seg_map(net):
    """(route,dir,seg) -> (H3 cell, heading bucket). Pools routes that share a road."""
    out = {}
    for (r, d), rd in net.rd.items():
        for seg in range(rd.nseg):
            a, b = seg * SEG, min((seg + 1) * SEG, rd.length)
            x0, y0 = point_at(rd.shape_xy, rd.cum, a)
            x1, y1 = point_at(rd.shape_xy, rd.cum, b)
            lat, lon = net.frame.to_ll((x0 + x1) / 2, (y0 + y1) / 2)
            if h3 is not None:
                cell = h3.latlng_to_cell(float(lat), float(lon), H3_RES)
            else:                                            # fallback: 200 m square grid in the local frame
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                cell = f"g{int(cx // 200)}_{int(cy // 200)}"
            out[(r, d, seg)] = (cell, bearing_bucket(x0, y0, x1, y1))
    return out


def agg(S, keys):
    """One row per key. Pace is pooled seconds/metre; the spread comes from bus passes."""
    g = S.groupby(["day", "veh"] + keys).agg(dt=("dt", "sum"), ds=("ds", "sum")).reset_index()
    g = g[g.ds >= 30].copy()
    g["pace"] = g.dt / g.ds
    o = g.groupby(keys).agg(dt=("dt", "sum"), ds=("ds", "sum"), n=("pace", "size"), days=("day", "nunique"))
    o["q25"] = g.groupby(keys).pace.quantile(0.25)
    o["q75"] = g.groupby(keys).pace.quantile(0.75)
    o = o[(o.n >= MIN_N) & (o.days >= MIN_DAYS)]
    o["pace"] = o.dt / o.ds
    return o


def expand(S, offs=range(-2, 3)):
    return pd.concat([S.assign(slot=(S.slot + o).clip(0, 95)) for o in offs], ignore_index=True)


def to_dict(o, decode):
    return {decode(k): (float(r.pace), float(r.q25), float(r.q75), int(r.n)) for k, r in zip(o.index, o.itertuples())}


def build_model(pairs, net, q, meta):
    rds = sorted(net.rd)
    rd_code = {k: i for i, k in enumerate(rds)}
    mv = pairs[pairs.stop < 0]
    S = pd.DataFrame({
        "day": pd.factorize(mv.date)[0], "veh": pd.factorize(mv.vehicle_id)[0],
        "rd": [rd_code[(r, int(d))] for r, d in zip(mv.route_id, mv.dir)],
        "seg": mv.seg.values, "dty": (mv.daytype == "we").astype(int).values,
        "slot": mv.slot.values, "dt": mv.dt.values, "ds": mv.ds.values})
    q["moving_samples"] = int(len(S))
    dty = lambda c: "we" if c else "wd"

    # level 1 and 2: segment x slot, and segment x +-2 slots
    t1 = to_dict(agg(S, ["rd", "seg", "dty", "slot"]),
                 lambda k: (rds[k[0]][0], rds[k[0]][1], int(k[1]), dty(k[2]), int(k[3])))
    S2 = expand(S)
    t2 = to_dict(agg(S2, ["rd", "seg", "dty", "slot"]),
                 lambda k: (rds[k[0]][0], rds[k[0]][1], int(k[1]), dty(k[2]), int(k[3])))

    # level 3: H3 cell x heading, routes pooled
    s2c = seg_map(net)
    cells = sorted({v[0] for v in s2c.values()})
    cell_code = {c: i for i, c in enumerate(cells)}
    segdf = pd.DataFrame([(rd_code[(r, d)], sg, cell_code[c], b) for (r, d, sg), (c, b) in s2c.items()],
                         columns=["rd", "seg", "cell", "brg"])
    S3 = S2.merge(segdf, on=["rd", "seg"], how="inner")
    t3 = to_dict(agg(S3, ["cell", "brg", "dty", "slot"]),
                 lambda k: (cells[k[0]], int(k[1]), dty(k[2]), int(k[3])))

    # level 4: route x hour
    S4 = S.assign(hour=S.slot // 4)
    t4 = to_dict(agg(S4, ["rd", "dty", "hour"]), lambda k: (rds[k[0]][0], rds[k[0]][1], dty(k[1]), int(k[2])))

    # dwell visits
    dw = pairs[pairs.stop >= 0].sort_values(["vehicle_id", "route_id", "dir", "t0"]).copy()
    key = dw.vehicle_id + "|" + dw.route_id + "|" + dw.dir.astype(str) + "|" + dw.stop.astype(str)
    newrun = (key != key.shift()) | ((dw.t0 - (dw.t0.shift() + dw.dt.shift())) > 45)
    dw["run"] = newrun.cumsum()
    v = dw.groupby("run").agg(route=("route_id", "first"), d=("dir", "first"), stop=("stop", "first"),
                              dty=("daytype", "first"), slot=("slot", "first"), dwell=("dt", "sum"))
    v = v[(v.dwell >= 3) & (v.dwell <= 300)]
    v["hour"] = v.slot // 4
    q["dwell_visits"] = int(len(v))
    g = v.groupby(["route", "d", "stop", "dty", "hour"]).dwell.agg(["median", "size"])
    dwell = {(r, int(d), int(s), t, int(h)): float(m) for (r, d, s, t, h), m, n in
             zip(g.index, g["median"], g["size"]) if n >= 5}
    dwell_glob = {(t, int(h)): float(m) for (t, h), m in v.groupby(["dty", "hour"]).dwell.median().items()}

    # observed headways at the route midpoint -> expected wait of a random arrival, E[H^2]/(2E[H])
    wait = {}
    for (r, d), grp in pairs.groupby(["route_id", "dir"]):
        rd = net.rd[(r, int(d))]
        sm = rd.length / 2
        g = grp[(grp.s0 < sm) & (grp.s1 >= sm) & (grp.ds > 0)]
        tc = g.t0 + (sm - g.s0) / g.ds * g.dt
        tmp = pd.DataFrame({"t": tc.values, "date": g.date.values, "dty": g.daytype.values}).sort_values("t")
        tmp["h"] = tmp.groupby("date").t.diff()
        tmp = tmp[(tmp.h > 30) & (tmp.h < 5400)]
        tmp["hour"] = ((tmp.t % 86400) // 3600).astype(int)
        for (t, hr), gg in tmp.groupby(["dty", "hour"]):
            if len(gg) >= 8:
                h = gg.h.values
                wait[(r, int(d), t, int(hr))] = float((h ** 2).mean() / (2 * h.mean()) / 60)

    # quietest hour (fastest network pace) per day type, used for the "usual quiet-hour" comparison
    byh = S4.groupby(["dty", "hour"]).agg(dt=("dt", "sum"), ds=("ds", "sum"), n=("dt", "size"))
    byh = byh[(byh.n > 200) & (byh.index.get_level_values("hour").isin(range(6, 22)))]
    byh["pace"] = byh.dt / byh.ds
    ref = {dty(int(t)): int(byh.loc[t].pace.idxmin()) for t in byh.index.get_level_values(0).unique()}
    meta["ref_hour"] = ref
    m = SpeedModel(t1, t2, t3, t4, s2c, dwell, dwell_glob, wait, meta).attach(net)
    q["table_cells"] = {"level1": len(t1), "level2": len(t2), "level3": len(t3), "level4": len(t4)}
    return m


def coverage(model, net):
    """Share of (segment, slot) cells answered at each level, 05:00-23:00."""
    out = {}
    for dtype in ("wd", "we"):
        cnt = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for (r, d), rd in net.rd.items():
            for seg in range(rd.nseg):
                for slot in range(20, 92):
                    cnt[model.pace(r, d, seg, dtype, slot)[1]] += 1
        tot = sum(cnt.values())
        out[dtype] = {f"level{k}": round(100 * v / tot, 1) for k, v in cnt.items()}
    return out


def cutoff_t(train_until):
    """'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (Delhi time) -> seconds on the same clock as df.t."""
    return (pd.Timestamp(train_until) - pd.Timestamp("1970-01-01")).total_seconds() if train_until else None


def run(gtfs, gps_path, out, train_until=None, tz=5.5, cols=None, min_n=None, min_days=None):
    global MIN_N, MIN_DAYS
    if min_n is not None:
        MIN_N = min_n
    if min_days is not None:
        MIN_DAYS = min_days
    os.makedirs(out, exist_ok=True)
    net = load_gtfs(gtfs)
    gps = load_gps(gps_path, cols)
    q = {}
    df = prepare(gps, net, tz, q)
    if train_until:
        df = df[df.t < cutoff_t(train_until)]
    q["clean_rows_used_for_training"] = int(len(df))
    pairs = make_pairs(df, net, q)
    synthetic = os.path.exists(os.path.join(gtfs, "SYNTHETIC.txt"))
    meta = dict(source="synthetic" if synthetic else "real", train_from=df.date.min(), train_until=train_until or df.date.max(), train_until_t=cutoff_t(train_until),
                built=dt.datetime.now().isoformat(timespec="seconds"),
                params=dict(seg_m=SEG, gate_m=GATE, min_passes=MIN_N, min_days=MIN_DAYS, h3_res=H3_RES))
    model = build_model(pairs, net, q, meta)
    q["coverage_pct"] = coverage(model, net)
    meta["quality"] = q
    with open(os.path.join(out, "model.pkl"), "wb") as fh:
        pickle.dump(model, fh)
    with open(os.path.join(out, "net.pkl"), "wb") as fh:
        pickle.dump(net, fh)
    with open(os.path.join(out, "quality.json"), "w") as fh:
        json.dump(meta, fh, indent=2, default=str)
    return model, net


def run_timetable_only(gtfs, out):
    """No GPS history: every estimate falls back to the timetable (level 5). Live pings can
    still be fed to the API and will correct estimates relative to the timetable."""
    os.makedirs(out, exist_ok=True)
    net = load_gtfs(gtfs)
    meta = dict(source="real", mode="timetable_only", built=dt.datetime.now().isoformat(timespec="seconds"),
                note="No GPS history was used. Travel times are timetable-based; live pings adjust them.")
    model = SpeedModel({}, {}, {}, {}, seg_map(net), {}, {}, {}, meta).attach(net)
    with open(os.path.join(out, "model.pkl"), "wb") as fh:
        pickle.dump(model, fh)
    with open(os.path.join(out, "net.pkl"), "wb") as fh:
        pickle.dump(net, fh)
    return model, net


def load_artifacts(path):
    with open(os.path.join(path, "net.pkl"), "rb") as fh:
        net = pickle.load(fh)
    with open(os.path.join(path, "model.pkl"), "rb") as fh:
        model = pickle.load(fh)
    return model.attach(net), net


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", required=True)
    ap.add_argument("--gps", default=None, help="omit for timetable-only mode")
    ap.add_argument("--out", required=True)
    ap.add_argument("--train-until", default=None, help="exclusive YYYY-MM-DD; later days are held out")
    ap.add_argument("--tz", type=float, default=5.5, help="hours to add to UTC timestamps (Delhi 5.5)")
    ap.add_argument("--min-days", type=int, default=None)
    ap.add_argument("--min-n", type=int, default=None)
    ap.add_argument("--cols", default=None, help='JSON map of your column names, e.g. \'{"lat":"latitude","vid":"vehicle_id"}\'')
    a = ap.parse_args()
    if not a.gps:
        run_timetable_only(a.gtfs, a.out)
        print("timetable-only model written to", a.out)
    else:
        m, _ = run(a.gtfs, a.gps, a.out, a.train_until, a.tz, json.loads(a.cols) if a.cols else None, a.min_n, a.min_days)
        print(json.dumps(m.meta["quality"], indent=2, default=str))
