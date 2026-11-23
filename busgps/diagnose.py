"""Print how one route was read from the GTFS, to spot bad shapes or stop positions.

    python -m busgps.diagnose --gtfs data\\gtfs --route "OMS(+)UP"
"""
import argparse

import numpy as np

from .network import load_gtfs

ap = argparse.ArgumentParser()
ap.add_argument("--gtfs", required=True)
ap.add_argument("--route", required=True, help="route_id or short name")
a = ap.parse_args()
net = load_gtfs(a.gtfs)
print(f"{len(net.rd)} route-directions loaded, {len(getattr(net, 'shape_fallbacks', []))} used the stop-polyline fallback")
for (rid, d), rd in net.rd.items():
    if a.route in (rid, rd.name):
        hop = np.sqrt((np.diff(net.stops.loc[rd.stop_ids, ["x", "y"]].values, axis=0) ** 2).sum(1)).sum()
        print(f"route_id={rid} name={rd.name} dir={d} stops={len(rd.stop_ids)} shape_len={rd.length:.0f} m "
              f"stop_span={rd.stop_s[-1] - rd.stop_s[0]:.0f} m stop_to_stop_straight={hop:.0f} m "
              f"sched_speed={rd.sched_speed * 3.6:.1f} km/h used_fallback={(rid, d) in getattr(net, 'shape_fallbacks', [])}")
