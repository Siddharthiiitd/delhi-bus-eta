"""Travel-time model: hierarchical segment pace table with a fallback chain,
dwell and headway tables, and optional live correction."""
import calendar
from collections import defaultdict

from .network import SEG

LEVELS = {1: "segment x time slot", 2: "segment x +-30 min", 3: "road cell, routes pooled",
          4: "route x hour", 5: "timetable"}
UNKNOWN_SPREAD = (0.75, 1.40)   # assumed, unvalidated spread used only at level 5
DEFAULT_DWELL_S = 20.0
DEFAULT_WAIT_MIN = 8.0


def daytype_of(dt):
    return "wd" if dt.weekday() < 5 else "we"


def epoch_local(dt):
    return calendar.timegm(dt.timetuple())


class SpeedModel:
    def __init__(self, t1, t2, t3, t4, seg2cell, dwell, dwell_glob, wait, meta):
        self.t1, self.t2, self.t3, self.t4 = t1, t2, t3, t4
        self.seg2cell, self.dwell, self.dwell_glob, self.wait_tab = seg2cell, dwell, dwell_glob, wait
        self.meta = meta
        self.net = None   # attached after loading
        self.live = None

    def attach(self, net, live=None):
        self.net, self.live = net, live
        return self

    # ---- pace lookup with fallback chain -------------------------------------------
    def pace(self, route, d, seg, dtype, slot):
        """Returns (seconds per metre, level, q25, q75)."""
        k = (route, d, seg, dtype, slot)
        r = self.t1.get(k)
        if r:
            return r[0], 1, r[1], r[2]
        r = self.t2.get(k)
        if r:
            return r[0], 2, r[1], r[2]
        cb = self.seg2cell.get((route, d, seg))
        if cb:
            r = self.t3.get((cb[0], cb[1], dtype, slot))
            if r:
                return r[0], 3, r[1], r[2]
        r = self.t4.get((route, d, dtype, slot // 4))
        if r:
            return r[0], 4, r[1], r[2]
        p = 1.0 / self.net.rd[(route, d)].sched_speed
        return p, 5, p * UNKNOWN_SPREAD[0], p * UNKNOWN_SPREAD[1]

    def dwell_s(self, route, d, k, dtype, hour):
        r = self.dwell.get((route, d, k, dtype, hour))
        if r:
            return r
        return self.dwell_glob.get((dtype, hour), DEFAULT_DWELL_S)

    def wait_min(self, rd, t0):
        """Expected wait in minutes. Observed headways first, then timetable, then a default."""
        dtype, hour = daytype_of(t0), t0.hour
        w = self.wait_tab.get((rd.route_id, rd.direction, dtype, hour))
        if w:
            return w, "observed headways"
        hw = rd.sched_hw.get(hour)
        if hw:
            return hw / 2, "timetable headway"
        return DEFAULT_WAIT_MIN, "assumed"

    # ---- ride time between two stop indices -------------------------------------------
    def ride(self, rd, ia, ib, t0, use_live=True):
        s0, s1 = float(rd.stop_s[ia]), float(rd.stop_s[ib])
        dtype = daytype_of(t0)
        base = epoch_local(t0)
        cursor = t0.hour * 3600 + t0.minute * 60 + t0.second
        total = lo = hi = 0.0
        by_level = defaultdict(float)
        live_used = False
        for seg in range(int(rd.seg_of(s0)), int(rd.seg_of(s1)) + 1):
            a, b = max(s0, seg * SEG), min(s1, (seg + 1) * SEG)
            ln = b - a
            if ln <= 0:
                continue
            slot = min(95, int(cursor // 900))
            p, lvl, q25, q75 = self.pace(rd.route_id, rd.direction, seg, dtype, slot)
            f = 1.0
            if use_live and self.live is not None:
                f = self.live.factor(self, rd, seg, base + (cursor - (t0.hour * 3600 + t0.minute * 60 + t0.second)))
                live_used = live_used or f != 1.0
            sec = p * f * ln
            total += sec
            lo += q25 * f * ln
            hi += q75 * f * ln
            by_level[lvl] += sec
            cursor += sec
        dw = sum(self.dwell_s(rd.route_id, rd.direction, k, dtype, t0.hour) for k in range(ia, ib))
        total, lo, hi = total + dw, lo + dw, hi + dw
        move = sum(by_level.values()) or 1.0
        share = {k: v / move for k, v in by_level.items()}
        if share.get(5, 0) >= 0.4:
            conf = "low"
        elif share.get(1, 0) + share.get(2, 0) >= 0.8:
            conf = "high"
        else:
            conf = "medium"
        return dict(seconds=total, lo=lo, hi=hi, dwell_s=dw, level_share=share, confidence=conf, live=live_used)
