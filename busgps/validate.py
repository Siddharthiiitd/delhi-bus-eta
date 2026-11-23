"""Held-out validation: model vs timetable against observed bus times, plus an optional
Google Maps comparison.

    python -m busgps.validate --gtfs data/synth --gps data/synth/gps.csv.gz --artifacts artifacts/synth

Observed time = time between a bus first reaching stop i and first reaching stop j (dwell at i
and in between included), interpolated from held-out GPS. The model never saw those days.
"""
import argparse
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from .model import daytype_of
from .pipeline import load_artifacts, load_gps, prepare

EPOCH = dt.datetime(1970, 1, 1)


def observed_pairs(df, net, rng, per_run=4):
    df = df.sort_values(["vehicle_id", "route_id", "dir", "t"], kind="stable").reset_index(drop=True)
    k = ["vehicle_id", "route_id", "dir"]
    newrun = ~(df[k] == df[k].shift()).all(axis=1) | ((df.t - df.t.shift()) > 90) | ((df.s - df.s.shift()) < -200)
    df["run"] = newrun.cumsum()
    med = df.groupby("run").s.transform(lambda x: x.rolling(5, center=True, min_periods=1).median())
    df = df[(df.s - med).abs() <= 100]
    rows = []
    for run, g in df.groupby("run"):
        if len(g) < 8:
            continue
        rd = net.rd[(g.route_id.iloc[0], int(g.dir.iloc[0]))]
        s, t = g.s.values, g.t.values
        smax = np.maximum.accumulate(s)
        u, ix = np.unique(smax, return_index=True)
        ok = np.where((rd.stop_s >= u[0] + 30) & (rd.stop_s <= u[-1] - 30))[0]
        if len(ok) < 3:
            continue
        tk = dict(zip(ok, np.interp(rd.stop_s[ok], u, t[ix])))
        for _ in range(per_run):
            i = int(rng.choice(ok))
            j = i + int(rng.integers(2, 13))
            if j in tk:
                rows.append(dict(route=rd.route_id, dir=rd.direction, i=i, j=j, t_i=tk[i], obs=tk[j] - tk[i],
                                 date=g.date.iloc[0]))
    return pd.DataFrame(rows)


def metrics(d, a, b):
    e = d[a] - d[b]
    return dict(n=int(len(d)), mae_min=round(float(e.abs().mean() / 60), 2),
                mape_pct=round(float((e.abs() / d[b]).mean() * 100), 1), bias_min=round(float(e.mean() / 60), 2))


def evaluate(gtfs_gps, artifacts, seed=0):
    model, net = load_artifacts(artifacts)
    df = prepare(gtfs_gps, net)
    cut = model.meta.get("train_until_t")
    test = df[df.t >= cut] if cut is not None else df[df.date >= model.meta["train_until"]]
    pr = observed_pairs(test, net, np.random.default_rng(seed))
    pm, ps, cf, hr, dtp = [], [], [], [], []
    for r in pr.itertuples():
        rd = net.rd[(r.route, r.dir)]
        t0 = EPOCH + dt.timedelta(seconds=float(r.t_i))
        res = model.ride(rd, r.i, r.j, t0, use_live=False)
        pm.append(res["seconds"])
        ps.append((rd.stop_s[r.j] - rd.stop_s[r.i]) / rd.sched_speed)
        cf.append(res["confidence"])
        hr.append(t0.hour)
        dtp.append(daytype_of(t0))
    pr["model"], pr["timetable"], pr["conf"], pr["hour"], pr["daytype"] = pm, ps, cf, hr, dtp
    pr["peak"] = np.where((pr.daytype == "wd") & pr.hour.isin([8, 9, 17, 18, 19]), "peak", "off-peak")
    out = {"source": model.meta.get("source"), "test_from": model.meta["train_until"], "pairs": int(len(pr))}
    groups = {"all": pr}
    for col in ("peak", "daytype", "conf"):
        for v, g in pr.groupby(col):
            groups[f"{col}={v}"] = g
    out["by_group"] = {name: {"model": metrics(g, "model", "obs"), "timetable": metrics(g, "timetable", "obs")}
                       for name, g in groups.items()}
    return out, pr


def to_markdown(res):
    lines = []
    if res["source"] == "synthetic":
        lines += ["> **Synthetic data.** These numbers only show that the pipeline runs end to end. They are not evidence of",
                  "> accuracy on Delhi buses. Re-run with the real held-out data before reporting anything.", ""]
    lines += [f"Held-out days from {res['test_from']}, {res['pairs']} observed stop-to-stop passes.", "",
              "| Group | n | Model MAE (min) | Model MAPE | Timetable MAE (min) | Timetable MAPE |", "|---|---|---|---|---|---|"]
    for k, v in res["by_group"].items():
        m, t = v["model"], v["timetable"]
        lines.append(f"| {k} | {m['n']} | {m['mae_min']} | {m['mape_pct']}% | {t['mae_min']} | {t['mape_pct']}% |")
    return "\n".join(lines) + "\n"


def gmaps_compare(path, artifacts, out_path):
    """CSV columns: from_lat,from_lon,to_lat,to_lon,depart,gmaps_minutes[,observed_minutes]."""
    from .planner import Planner
    model, net = load_artifacts(artifacts)
    pl = Planner(net, model)
    rows = []
    for r in pd.read_csv(path).itertuples():
        opts = pl.plan((r.from_lat, r.from_lon), (r.to_lat, r.to_lon), dt.datetime.fromisoformat(r.depart), 1)
        est = opts[0]["total_minutes"] if opts else np.nan
        row = dict(depart=r.depart, model_min=round(est, 1), gmaps_min=r.gmaps_minutes)
        if hasattr(r, "observed_minutes"):
            row.update(observed_min=r.observed_minutes, model_err=round(est - r.observed_minutes, 1),
                       gmaps_err=round(r.gmaps_minutes - r.observed_minutes, 1))
        rows.append(row)
    df = pd.DataFrame(rows)
    with open(out_path, "w") as fh:
        fh.write(df.to_markdown(index=False) if hasattr(df, "to_markdown") else df.to_string(index=False))
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--gps")
    ap.add_argument("--gtfs")
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--gmaps", help="CSV of origin-destination pairs with Google Maps times")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.gps:
        res, pr = evaluate(load_gps(a.gps), a.artifacts)
        json.dump(res, open(os.path.join(a.out, "validation.json"), "w"), indent=2)
        open(os.path.join(a.out, "validation.md"), "w").write(to_markdown(res))
        pr.to_csv(os.path.join(a.out, "validation_pairs.csv"), index=False)
        print(to_markdown(res))
    if a.gmaps:
        print(gmaps_compare(a.gmaps, a.artifacts, os.path.join(a.out, "gmaps_comparison.md")))
