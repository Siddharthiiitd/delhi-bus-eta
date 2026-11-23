"""Live correction layer.

Each incoming ping is compared with the bus's previous ping. The resulting (time, distance)
sample is folded into an exponentially decayed sum for the road cell it covers, keyed by
H3 cell and heading so every route on that road benefits. Entries expire after TTL seconds
without new data (constants here are untuned defaults, to be tuned on held-out data). At query time the live/historical ratio nudges the historical pace,
with a weight that grows with the number of recent samples and fades with how far in the
future the segment is reached.

This is the same shape as Uber's per-cell live counter (last known state per vehicle,
expiry for stale entries) but it stores speed instead of a count.
"""
import datetime as dt
import math

EPOCH = dt.datetime(1970, 1, 1)

from .network import GATE

TTL_S = 1800.0
EWMA_TAU_S = 600.0
K_PRIOR = 3.0           # effective samples needed for live to carry half the weight
HORIZON_TAU_S = 2400.0  # live weight decays over ~40 min into the future
MAX_SPEED = 22.0


class LiveStore:
    def __init__(self, net, model, tz=5.5):
        self.net, self.model, self.tz = net, model, tz
        self.last = {}      # vehicle -> (route, dir, s, ts)
        self.cells = {}     # (cell, bearing) -> [sum_dt, sum_ds, n_eff, last_ts]
        self.latest_ts = None
        self.accepted = self.rejected = 0

    def ingest(self, vehicle_id, route_id, lat, lon, ts_utc):
        ts = float(ts_utc) + self.tz * 3600
        self.latest_ts = ts if self.latest_ts is None else max(self.latest_ts, ts)
        cands = self.net.project_ping(route_id, lat, lon)
        if not cands:
            self.rejected += 1
            return False
        prev = self.last.get(vehicle_id)
        pick = min(cands, key=lambda c: c[2])
        if prev and prev[0] == route_id:
            same = [c for c in cands if c[0] == prev[1]]
            if same and same[0][2] <= GATE and same[0][1] - prev[2] >= -30:
                pick = same[0]
        d, s, dist = pick
        if dist > GATE:
            self.rejected += 1
            return False
        self.last[vehicle_id] = (route_id, d, s, ts)
        if not prev or prev[0] != route_id or prev[1] != d:
            return True
        dt, ds = ts - prev[3], s - prev[2]
        if not (3 <= dt <= 90) or ds < -30 or ds / dt > MAX_SPEED:
            self.rejected += 1
            return False
        rd = self.net.rd[(route_id, d)]
        if int(rd.stop_near(prev[2], s)) >= 0 and abs(ds) / dt <= 1.0:
            return True   # dwell at a stop, not road speed
        seg = int(rd.seg_of((s + prev[2]) / 2))
        key = self.model.seg2cell.get((route_id, d, seg))
        if key is None:
            return True
        st = self.cells.setdefault(key, [0.0, 0.0, 0.0, ts])
        decay = math.exp(-max(0.0, ts - st[3]) / EWMA_TAU_S)
        st[0], st[1], st[2] = st[0] * decay + dt, st[1] * decay + ds, st[2] * decay + 1.0
        st[3] = max(st[3], ts)
        self.accepted += 1
        return True

    def factor(self, model, rd, seg, t_abs):
        """Multiplier on the historical pace for this segment when reached at t_abs."""
        if self.latest_ts is None:
            return 1.0
        key = model.seg2cell.get((rd.route_id, rd.direction, seg))
        st = self.cells.get(key)
        if not st or self.latest_ts - st[3] > TTL_S or st[1] < 20:
            return 1.0
        now = EPOCH + dt.timedelta(seconds=self.latest_ts)
        slot = min(95, now.hour * 4 + now.minute // 15)
        hist, _, _, _ = model.pace(rd.route_id, rd.direction, seg, "wd" if now.weekday() < 5 else "we", slot)
        ratio = (st[0] / st[1]) / hist
        decay = math.exp(-max(0.0, t_abs - self.latest_ts) / HORIZON_TAU_S)
        decay *= math.exp(-(self.latest_ts - st[3]) / 900.0)   # fade as the last sample ages
        w = st[2] / (st[2] + K_PRIOR) * decay
        return 1.0 + w * (min(max(ratio, 0.3), 4.0) - 1.0)

    def status(self):
        return dict(cells=len(self.cells), vehicles=len(self.last), accepted=self.accepted,
                    rejected=self.rejected,
                    latest=None if self.latest_ts is None else
                    (EPOCH + dt.timedelta(seconds=self.latest_ts)).isoformat())
