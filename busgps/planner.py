"""Journey planner: walk -> bus (-> transfer -> bus) -> walk, with time-dependent bus legs.

Candidates are generated on stop sequences (direct and one transfer), ranked cheaply, then
evaluated with the trained model: the clock advances leg by leg, so the model is queried at
the time the bus actually reaches each stretch of road."""
import datetime as dt

import numpy as np
try:                       # scipy is optional; fall back to a brute-force numpy search
    from scipy.spatial import cKDTree
except Exception:
    cKDTree = None

from .geo import point_at
from .network import SEG

WALK_MS = 1.25          # m/s
WALK_DETOUR = 1.3       # street distance vs straight line
MAX_WALK_M = 800.0
TRANSFER_M = 250.0
TRANSFER_PENALTY_MIN = 5.0     # ranking penalty per transfer (display still shows the real time)
MIN_TRANSFER_GAIN_MIN = 3.0    # a transfer must beat the best direct trip by this much to be shown


class _BruteTree:
    def __init__(self, xy):
        self.xy = np.asarray(xy, float)

    def query_ball_point(self, p, r):
        d2 = ((self.xy - np.asarray(p, float)) ** 2).sum(1)
        return np.where(d2 <= r * r)[0].tolist()


def walk_s(m):
    return m * WALK_DETOUR / WALK_MS


class Planner:
    def __init__(self, net, model):
        self.net, self.model = net, model
        self.ids = sorted({s for rd in net.rd.values() for s in rd.stop_ids})
        self.xy = net.stops.loc[self.ids, ["x", "y"]].values
        self.tree = cKDTree(self.xy) if cKDTree is not None else _BruteTree(self.xy)
        self.pos = {s: i for i, s in enumerate(self.ids)}
        self.stop_rds = {}
        for key, rd in net.rd.items():
            for i, s in enumerate(rd.stop_ids):
                self.stop_rds.setdefault(s, []).append((key, i))

    def near(self, x, y, r=MAX_WALK_M, k=15):
        idx = self.tree.query_ball_point([x, y], r)
        d = [(float(np.hypot(*(self.xy[i] - (x, y)))), self.ids[i]) for i in idx]
        return sorted(d)[:k]

    # ---- candidate generation ---------------------------------------------------------
    def candidates(self, o, d):
        O, D = self.near(*o), self.near(*d)
        dest = {}
        for dist, sid in D:
            for key, ib in self.stop_rds.get(sid, []):
                dest.setdefault(key, []).append((ib, sid, dist))
        cands = []
        for od, osid in O:
            for key, ia in self.stop_rds.get(osid, []):
                rd = self.net.rd[key]
                for ib, bsid, dd in dest.get(key, []):
                    if ib > ia:
                        cheap = walk_s(od + dd) + (rd.stop_s[ib] - rd.stop_s[ia]) / 6 + 300
                        cands.append((cheap, dict(w1=od, legs=[(key, ia, ib)], xw=[], w2=dd)))
                for ix in range(ia + 1, min(len(rd.stop_ids), ia + 60)):
                    xi = self.pos[rd.stop_ids[ix]]
                    for yi in self.tree.query_ball_point(self.xy[xi], TRANSFER_M):
                        ysid = self.ids[yi]
                        tw = float(np.hypot(*(self.xy[yi] - self.xy[xi])))
                        for key2, iy in self.stop_rds.get(ysid, []):
                            if key2[0] == key[0] or key2 not in dest:
                                continue
                            rd2 = self.net.rd[key2]
                            for ib, bsid, dd in dest[key2]:
                                if ib > iy:
                                    cheap = (walk_s(od + dd + tw) + (rd.stop_s[ix] - rd.stop_s[ia]) / 6
                                             + (rd2.stop_s[ib] - rd2.stop_s[iy]) / 6 + 600)
                                    cands.append((cheap, dict(w1=od, legs=[(key, ia, ix), (key2, iy, ib)], xw=[tw], w2=dd)))
            if len(cands) > 6000:
                cands = sorted(cands, key=lambda c: c[0])[:300]
        return sorted(cands, key=lambda c: c[0])[:60]

    # ---- evaluation ---------------------------------------------------------------------
    def evaluate(self, spec, depart, use_live=True):
        t = depart + dt.timedelta(seconds=walk_s(spec["w1"]))
        legs = [dict(type="walk", minutes=walk_s(spec["w1"]) / 60, meters=round(spec["w1"]))]
        lo = hi = walk_s(spec["w1"])
        conf = []
        live = False
        for i, (key, ia, ib) in enumerate(spec["legs"]):
            rd = self.net.rd[key]
            wmin, wsrc = self.model.wait_min(rd, t)
            t += dt.timedelta(minutes=wmin)
            r = self.model.ride(rd, ia, ib, t, use_live)
            a, b = rd.stop_ids[ia], rd.stop_ids[ib]
            xs, ys = point_at(rd.shape_xy, rd.cum, np.arange(rd.stop_s[ia], rd.stop_s[ib] + SEG, SEG).clip(max=rd.stop_s[ib]))
            lat, lon = self.net.frame.to_ll(xs, ys)
            legs.append(dict(type="bus", route=rd.name, route_id=rd.route_id, direction=rd.direction,
                             board=self.net.stops.loc[a, "name"], alight=self.net.stops.loc[b, "name"],
                             stops=ib - ia, wait_minutes=wmin, wait_source=wsrc,
                             ride_minutes=r["seconds"] / 60, ride_range=[r["lo"] / 60, r["hi"] / 60],
                             confidence=r["confidence"], live_adjusted=r["live"],
                             path=[[float(p), float(q)] for p, q in zip(lat, lon)]))
            lo += wmin * 60 + r["lo"]
            hi += wmin * 60 + r["hi"]
            conf.append(r["confidence"])
            live = live or r["live"]
            t += dt.timedelta(seconds=r["seconds"])
            if i < len(spec["xw"]):
                w = walk_s(spec["xw"][i])
                legs.append(dict(type="walk", minutes=w / 60, meters=round(spec["xw"][i]), note="transfer"))
                t += dt.timedelta(seconds=w)
                lo += w
                hi += w
        w2 = walk_s(spec["w2"])
        legs.append(dict(type="walk", minutes=w2 / 60, meters=round(spec["w2"])))
        t += dt.timedelta(seconds=w2)
        lo += w2
        hi += w2
        total = (t - depart).total_seconds()
        order = {"low": 0, "medium": 1, "high": 2}
        return dict(total_minutes=total / 60, range_minutes=[lo / 60, hi / 60], arrive=t.strftime("%H:%M"),
                    confidence=min(conf, key=order.get), live_adjusted=live, legs=legs,
                    signature=tuple((k[0], k[1]) for k, _, _ in spec["legs"]))

    def plan(self, o_ll, d_ll, depart, n=3):
        o = [float(v) for v in self.net.frame.to_xy(*o_ll)]
        d = [float(v) for v in self.net.frame.to_xy(*d_ll)]
        cands = self.candidates(o, d)
        evals, seen = [], {}
        for _, spec in cands[:40]:
            e = self.evaluate(spec, depart)
            e["_spec"] = spec
            if e["signature"] not in seen or e["total_minutes"] < seen[e["signature"]]["total_minutes"]:
                seen[e["signature"]] = e
        # A transfer costs effort and risk the clock does not capture, so rank with a penalty, and only
        # show a transfer option when it clearly beats the best direct trip.
        ordered = sorted(seen.values(), key=lambda e: e["total_minutes"] + TRANSFER_PENALTY_MIN * (len(e["legs"]) and sum(1 for l in e["legs"] if l["type"] == "bus") - 1))
        direct = [e for e in ordered if sum(1 for l in e["legs"] if l["type"] == "bus") == 1]
        if direct:
            best = direct[0]["total_minutes"]
            ordered = [e for e in ordered if e in direct or e["total_minutes"] <= best - MIN_TRANSFER_GAIN_MIN]
        evals = ordered[:n]
        ref = self.model.meta.get("ref_hour", {}).get("wd" if depart.weekday() < 5 else "we")
        for e in evals:
            if ref is not None:
                quiet = depart.replace(hour=ref, minute=0, second=0)
                e["typical_quiet_minutes"] = self.evaluate(e["_spec"], quiet, use_live=False)["total_minutes"]
                e["quiet_hour"] = f"{ref:02d}:00"
            e.pop("_spec")
            e["signature"] = list(e["signature"])
        return evals
