# Delhi bus travel-time model and journey planner

Transport Stack Hackathon 2026, Problem 1 (Phase 1 prototype).

A travel-time model learned from bus GPS, and a journey planner on top of it. A user gives an
origin, a destination and a departure time. The API returns bus journeys (walk, board, ride,
transfer, walk) with a total time, a typical range, a confidence level, and how the same trip
usually looks at a quiet hour.

## Status: read this first

| Part | State |
|---|---|
| GPS cleaning, map-matching to GTFS shapes, segment-speed model, dwell and headway tables | Built, runs end to end |
| Planner (direct and one-transfer, time-dependent), REST API, web map | Built, runs end to end |
| Live correction layer (per-road-cell EWMA with expiry) | Built and tested by replaying recorded pings. Constants are untuned defaults |
| Held-out validation script, Google Maps comparison script | Built. Google Maps comparison needs times you collect by hand |
| **Run on the real Transport Stack data** | **Not yet. Everything in this repo has so far been run on synthetic data only** |
| GTFS-RT protobuf consumer | Not built. `/live/pings` takes JSON, so a small poller can feed it |

The demo data is synthetic (a toy grid of 6 routes with made-up congestion, signals, GPS noise and
one incident). The validation table in `reports/validation.md` comes from that data. It shows the
pipeline works and that peak and off-peak differences are learned. It says nothing about how accurate
the model is on Delhi's buses.

## Quick start (synthetic demo)

```
pip install -r requirements.txt
sh run_demo.sh
# open http://127.0.0.1:8000
```

Click the map for the start and the destination, pick a time, press Find journeys. The coloured lines
show learned bus speed for the chosen time of day.

To see the live layer, with the API running:

```
python -m busgps.replay --gps data/synth/gps.csv.gz --date 2026-11-05 --start 07:30 --end 08:30
```

That feeds one hour of recorded pings (including a slowdown on route V1) and journeys that cross the
affected stretch are marked "live adjusted".

## Timetable-only mode (no GPS history)

If you have the static GTFS but no historical GPS, the model can still run. Every estimate falls back to the
timetable, confidence shows "low", and live pings (if you forward them) adjust the times relative to the timetable.

```
python -m busgps.pipeline --gtfs data/gtfs --out artifacts/delhi          # no --gps
ARTIFACTS=artifacts/delhi uvicorn busgps.api:app --port 8000
# in another terminal, forward the real feed into the live layer:
export TRANSPORT_STACK_API_KEY=...
python -m busgps.poll_rt --api http://127.0.0.1:8000 --out data/real
```

In this mode nothing is learned from history, so there are no peak/off-peak patterns and no held-out validation.
Say so in any write-up.

## Running on the real data

1. Get the static GTFS folder and the historical GPS from the Delhi Transport Stack platform.
2. The GPS file needs `vehicle_id, route_id, latitude, longitude, timestamp` (unix seconds, UTC).
   If your columns are named differently, pass `--cols '{"lat":"latitude", ...}'` (old name -> new name).
3. Train, holding out the last week for testing:

```
python -m busgps.pipeline --gtfs path/to/gtfs --gps path/to/gps.csv --out artifacts/delhi --train-until YYYY-MM-DD
python -m busgps.validate --gps path/to/gps.csv --artifacts artifacts/delhi
ARTIFACTS=artifacts/delhi uvicorn busgps.api:app --port 8000
```

4. For the Google Maps comparison, write a CSV with `from_lat,from_lon,to_lat,to_lon,depart,gmaps_minutes`
   (and `observed_minutes` if you have it), then run `python -m busgps.validate --artifacts artifacts/delhi --gmaps pairs.csv`.

The historical file format is still "to be confirmed" in the brief, and the full Delhi GPS file will be far
larger than the synthetic one. The pipeline holds it in memory with pandas, so expect to chunk by day or by
route if the machine is small.

## API

```
GET  /journeys?from=<lat,lon | stop name>&to=<lat,lon | stop name>&depart=2026-11-06T08:30&n=3
GET  /segments?daytype=wd&hour=8&minute=30&south=&west=&north=&east=
GET  /stops?q=name        GET /meta        GET /health
POST /live/pings          [{vehicle_id, route_id, latitude, longitude, timestamp}, ...]
GET  /live/status         POST /live/reset
```

Each journey option has `total_minutes`, `range_minutes`, `arrive`, `confidence`, `typical_quiet_minutes`,
`live_adjusted` and a list of `legs` (walk legs, and bus legs with route, stops, wait, ride time and path).
Interactive docs are at `/docs` when the server is running.

## Method in short

1. **Clean.** Drop duplicate pings, project every ping onto the route shapes (both directions, nearest wins,
   direction smoothed over 9 pings per vehicle), drop pings more than 40 m off the route, drop consecutive-ping pairs
   with gaps over 90 s or implied speed over 80 km/h.
2. **Samples.** Each pair of consecutive pings gives (seconds, metres along the route) for a 250 m segment of that route.
   Slow movement within 40 m of a stop is counted as stop dwell, not road time. Waiting at signals stays in road time.
3. **Tables.** Pace (seconds per metre) per segment, day type (weekday/weekend) and 15-minute slot, plus
   the spread across bus passes. Thin cells fall back through a chain: segment x slot, segment x +-30 min, H3 cell x
   heading (pools routes that share a road), route x hour, timetable. Every ride reports which level it used, and
   confidence is derived from that.
4. **Waiting.** Expected wait comes from observed headways (E[H^2] / 2E[H]), falling back to timetable headways.
5. **Planner.** Direct and one-transfer candidates are generated on stop sequences, then evaluated leg by leg with a
   running clock, so each stretch is priced at the time the bus reaches it.
6. **Live.** Each new ping is compared with the vehicle's previous ping, folded into an exponentially decayed
   speed estimate for the H3 cell it covers, and expired when stale. At query time the live/historical ratio adjusts
   the historical pace, with a weight that grows with sample count and fades with time into the future.

## Known limitations

- Not yet validated on real data, and not yet compared with Google Maps.
- Day type is only weekday/weekend. No holidays, weather or events.
- The "range" assumes delays on neighbouring stretches move together, so it is wide. Level 5 (timetable) uses
  an assumed spread that has not been validated.
- Headway wait is measured at the route midpoint, so it is approximate for stops far from it.
- Direction is inferred from the nearest shape. Where the two directions run on nearly the same line, this can mix up.
- The planner handles direct trips and one transfer, no multimodal trips.
- Live constants (decay, prior weight) are defaults, not tuned.

## Layout

```
busgps/geo.py        projection helpers
busgps/network.py    static GTFS loader
busgps/pipeline.py   cleaning, samples, tables, coverage audit (writes artifacts/*/quality.json)
busgps/model.py      pace lookup with fallback chain, ride and wait estimates
busgps/live.py       live correction
busgps/planner.py    journey search and evaluation
busgps/api.py        FastAPI app and static client
busgps/validate.py   held-out validation and Google Maps comparison
busgps/synth.py      synthetic data generator (demo only)
busgps/replay.py     replay recorded pings into the live endpoint
web/index.html       map client
```
