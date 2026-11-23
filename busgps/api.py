"""HTTP API and static web client.

    ARTIFACTS=artifacts/synth uvicorn busgps.api:app --port 8000

GET  /journeys?from=<lat,lon|stop name>&to=<lat,lon|stop name>&depart=2026-11-06T08:30&n=3
GET  /segments?daytype=wd&hour=8&minute=30&south=&west=&north=&east=
GET  /stops?q=name
POST /live/pings   [{vehicle_id, route_id, latitude, longitude, timestamp}, ...]
GET  /live/status, /meta, /health
"""
import datetime as dt
import os
import re
from typing import List

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .geo import point_at
from .live import LiveStore
from .network import SEG
from .pipeline import load_artifacts
from .planner import Planner

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
LATLON = re.compile(r"^\s*(-?\d+(\.\d+)?)\s*,\s*(-?\d+(\.\d+)?)\s*$")


class Ping(BaseModel):
    vehicle_id: str
    route_id: str
    latitude: float
    longitude: float
    timestamp: float


def create_app(artifacts):
    model, net = load_artifacts(artifacts)
    live = LiveStore(net, model)
    model.attach(net, live)
    planner = Planner(net, model)
    app = FastAPI(title="Delhi bus travel-time model and journey planner")

    def place(txt):
        m = LATLON.match(txt)
        if m:
            return float(m.group(1)), float(m.group(3))
        hit = net.stops[net.stops.name.str.contains(re.escape(txt), case=False, regex=True)]
        if hit.empty:
            raise HTTPException(400, f"No stop matches '{txt}'. Use 'lat,lon' or part of a stop name.")
        return float(hit.iloc[0].lat), float(hit.iloc[0].lon)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/meta")
    def meta():
        m = dict(model.meta)
        m["routes"] = len(net.routes)
        m["stops"] = len(net.stops)
        m["center"] = [net.frame.lat0, net.frame.lon0]
        return m

    @app.get("/stops")
    def stops(q: str = "", limit: int = 20):
        s = net.stops[net.stops.name.str.contains(re.escape(q), case=False)] if q else net.stops
        return [dict(id=i, name=r["name"], lat=r.lat, lon=r.lon) for i, r in s.head(limit).iterrows()]

    @app.get("/journeys")
    def journeys(frm: str = Query(..., alias="from"), to: str = Query(...), depart: str = "", n: int = 3, live_on: bool = True):
        o, d = place(frm), place(to)
        try:
            when = dt.datetime.fromisoformat(depart) if depart else dt.datetime.now().replace(second=0, microsecond=0)
        except ValueError:
            raise HTTPException(400, "depart must look like 2026-11-06T08:30")
        model.live = live if live_on else None
        opts = planner.plan(o, d, when, n)
        model.live = live
        return dict(origin=o, destination=d, depart=when.isoformat(timespec="minutes"), options=opts,
                    data_source=model.meta.get("source"), live=live.status())

    @app.get("/segments")
    def segments(daytype: str = "wd", hour: int = 8, minute: int = 0, south: float = -90, west: float = -180,
                 north: float = 90, east: float = 180, limit: int = 4000):
        slot = hour * 4 + minute // 15
        out = []
        for (r, d), rd in net.rd.items():
            for seg in range(rd.nseg):
                a, b = seg * SEG, min((seg + 1) * SEG, rd.length)
                xs, ys = point_at(rd.shape_xy, rd.cum, [a, (a + b) / 2, b])
                lat, lon = net.frame.to_ll(xs, ys)
                if not (south <= lat[1] <= north and west <= lon[1] <= east):
                    continue
                p, lvl, _, _ = model.pace(r, d, seg, daytype, slot)
                out.append(dict(route=rd.name, dir=d, kmh=round(3.6 / p, 1), level=lvl,
                                line=[[float(la), float(lo)] for la, lo in zip(lat, lon)]))
                if len(out) >= limit:
                    return out
        return out

    @app.post("/live/pings")
    def live_pings(pings: List[Ping]):
        ok = sum(live.ingest(p.vehicle_id, p.route_id, p.latitude, p.longitude, p.timestamp) for p in pings)
        return dict(accepted=ok, received=len(pings))

    @app.get("/live/status")
    def live_status():
        return live.status()

    @app.post("/live/reset")
    def live_reset():
        live.cells.clear()
        live.last.clear()
        live.latest_ts = None
        live.accepted = live.rejected = 0
        return live.status()

    @app.get("/")
    def index():
        return FileResponse(os.path.join(WEB, "index.html"))

    return app


app = create_app(os.environ.get("ARTIFACTS", "artifacts/synth")) if os.path.exists(
    os.path.join(os.environ.get("ARTIFACTS", "artifacts/synth"), "model.pkl")) else None
