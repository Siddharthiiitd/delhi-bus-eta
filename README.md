# Delhi Bus ETA & Journey Planner

A real-time bus travel-time estimation and journey-planning prototype for Delhi, built for **Transport Stack Hackathon 2026 – Phase 1, Problem 1**.

The system combines static GTFS transit data with live GTFS-Realtime (GTFS-RT) vehicle positions. It estimates travel time over route segments, uses a fallback hierarchy when observations are sparse, applies a live adjustment to estimates, and exposes bus journey options through a REST API and a lightweight web interface.

> **Data note:** The real-data model described here was trained using a short, self-recorded GTFS-RT dataset collected on **23 November 2026**. The team did not have a supplied multi-week historical GPS archive. Raw recordings and trained model artifacts are local inputs/outputs and are not guaranteed to be present in a fresh clone.

## Features

- Static GTFS ingestion: stops, routes, trips, stop times and route shapes.
- GTFS-RT vehicle-position recording and live ingestion.
- GPS cleaning and association with GTFS route shapes.
- Approximately 250 m route-segment travel-time estimates.
- Separate stop-dwell handling within a configured 40 m stop gate.
- Sparse-data fallback hierarchy with confidence information.
- Live adjustment using recent vehicle observations.
- Direct and one-transfer bus journey search.
- FastAPI REST API with interactive OpenAPI documentation.
- Lightweight web page served from the API.
- Pipeline quality summary and observed-trip validation script.

## Architecture

1. **Data ingestion:** Static GTFS supplies the transit network and schedules. GTFS-RT supplies live vehicle positions. A recorder stores live snapshots as CSV.
2. **Cleaning and route association:** Duplicate/null rows, unknown routes, off-route positions and implausible movement samples are filtered.
3. **Travel-time estimation:** Consecutive pings from the same vehicle provide movement samples for route segments of about 250 m. Stop dwell is maintained separately.
4. **Fallback:** Estimates use the most specific sufficiently supported level, then progressively broader evidence, finally falling back to timetable values.
5. **Live adjustment:** Recent live movement information adjusts segment estimates; stale observations expire.
6. **Journey planning:** The planner searches direct and one-transfer bus journeys and returns options with travel-time ranges and confidence.

## Requirements

- Python 3.11 recommended (used in the development environment).
- Static GTFS data for Delhi.
- Recorded GTFS-RT CSV snapshots for real-data training.
- Windows PowerShell commands are shown below; equivalent commands can be used on Linux/macOS.

Python dependencies are listed in `requirements.txt`:

```text
numpy
pandas
scipy
h3>=4
fastapi
uvicorn
httpx
gtfs-realtime-bindings
```

## Setup

Clone the repository:

```powershell
git clone https://github.com/Siddharthiiitd/delhi-bus-eta.git
cd delhi-bus-eta
```

Create and activate a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Data layout

Place the static GTFS files in:

```text
data/gtfs/
```

The pipeline uses the relevant GTFS tables, including stops, routes, trips, stop times and shapes.

Place locally recorded GTFS-RT CSV snapshots in:

```text
data/snap/
```

The training command below expects files matching `gps_*.csv`. The raw data directory is not included as a reproducible public dataset in this repository. Obtain the permitted feed/data separately and do not commit credentials or private raw recordings.

### Recording live data

The team used a separate recorder process to collect live GTFS-RT vehicle-position snapshots to CSV. The exact recorder invocation and feed configuration are not documented in the repository files inspected for this README, so they are intentionally not guessed here. See the recorder implementation/configuration in the project source or the team's run notes.

## Train the real-data model

From the repository root, after placing GTFS data and recorded GPS CSV files in the expected directories:

```powershell
python -m busgps.pipeline --gtfs data/gtfs --gps "data/snap/gps_*.csv" --out artifacts/delhi_real --min-days 1 --min-n 4
```

This creates the trained model and quality metadata under `artifacts/delhi_real/`.

The final development artifact was built from the team's one-day recording. Its quality summary reported:

- Raw rows: **1,551,026**
- Clean rows used for training: **1,089,623**
- Moving samples: **892,663**
- Dwell visits: **20,375**
- Duplicate or null rows: **48,881**
- Unknown-route rows: **23,089**
- Off-route rows: **389,433**

These are statistics from the team's development run, not guaranteed outputs for a fresh dataset.

## Start the API and web interface

In the API terminal, set the artifact directory and start the FastAPI application:

```powershell
$env:ARTIFACTS = "artifacts/delhi_real"
python -m uvicorn busgps.api:app --host 127.0.0.1 --port 8000
```

Set `ARTIFACTS` to the trained real-data artifact directory. If it is omitted, the application may load its default artifact directory instead.

Open:

- Web interface: http://127.0.0.1:8000/
- Interactive API documentation: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health
- Model metadata: http://127.0.0.1:8000/meta

The web directory contains `web/index.html`; no separate Node.js package manifest or npm build step was present in the inspected repository.

## API reference

### `GET /journeys`

Returns up to the requested number of bus journey options between an origin and destination.

| Parameter | Required | Description |
|---|---:|---|
| `from` | Yes | Origin stop name or coordinate pair |
| `to` | Yes | Destination stop name or coordinate pair |
| `depart` | No | Departure date/time |
| `n` | No | Number of journey options (default 3) |
| `live_on` | No | Enable/disable live adjustment (default true) |

Example (PowerShell):

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/journeys?from=GGS%20University%20ISBT%20Kashmere%20Gate&to=Nehru%20Place&depart=2026-11-24T08:30:00&n=3&live_on=true"
```

The response includes journey options with total time, estimated range, confidence, live-adjustment status and journey legs (such as route, boarding/alighting stops, waiting time and ride time).

### Other endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | API health check |
| `/meta` | GET | Loaded model metadata and quality summary |
| `/stops` | GET | Search stops by name (`q`, `limit`) |
| `/segments` | GET | Query segment estimates by time and map bounds |
| `/live/pings` | POST | Submit live vehicle-position observations |
| `/live/status` | GET | Inspect live subsystem counters and latest update |
| `/live/reset` | POST | Reset in-memory live state |
| `/` | GET | Serve the web interface |
| `/docs` | GET | Interactive OpenAPI / Swagger documentation |

## Synthetic demo versus real-data run

The repository includes `run_demo.sh`. **This script runs the synthetic-data demo**, not the real-data training run described in the project evaluation. It installs dependencies, generates synthetic data under `data/synth`, trains `artifacts/synth`, validates it and starts the API using that synthetic artifact.

On a Unix-like shell with `sh` available, it can be run from the repository root:

```bash
sh run_demo.sh
```
### Run the synthetic demo on Windows PowerShell

Activate your virtual environment and run these commands from the repository root:

```powershell
python -m pip install -r requirements.txt

python -m busgps.synth --out data/synth

python -m busgps.pipeline --gtfs data/synth --gps data/synth/gps.csv.gz --out artifacts/synth --train-until 2026-11-05

python -m busgps.validate --gps data/synth/gps.csv.gz --artifacts artifacts/synth

$env:ARTIFACTS = "artifacts/synth"
python -m uvicorn busgps.api:app --host 127.0.0.1 --port 8000
```

### Run the synthetic demo on Windows PowerShell

From the repository root, activate your Python 3.11 virtual environment and run the following commands one at a time:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m busgps.synth --out data/synth
python -m busgps.pipeline --gtfs data/synth --gps data/synth/gps.csv.gz --out artifacts/synth --train-until 2026-11-05
python -m busgps.validate --gps data/synth/gps.csv.gz --artifacts artifacts/synth
$env:ARTIFACTS = "artifacts/synth"
python -m uvicorn busgps.api:app --host 127.0.0.1 --port 8000
```

When the API starts, open:

- Web interface: http://127.0.0.1:8000/
- Interactive API documentation: http://127.0.0.1:8000/docs
- Model metadata: http://127.0.0.1:8000/meta

If port `8000` is already in use, choose another port (for example, `8001`) in the Uvicorn command and use that port in the browser URLs.

The validation script prints a warning that synthetic results demonstrate end-to-end pipeline execution only; they are **not evidence of prediction accuracy on Delhi buses**.

Do not use this workflow when demonstrating the real-data artifact. For the real-data application, follow the training and API startup commands above and set `ARTIFACTS=artifacts/delhi_real`.

## Validation summary

The observed-trip validation used a same-evening temporal holdout on 23 November 2026. It contained **4,240 observed stop-to-stop passes** after the training cutoff.

| Metric | Learned model | Timetable baseline |
|---|---:|---:|
| MAE | 4.15 min | 2.30 min |
| MAPE | 147.5% | 92.8% |

The timetable baseline performed better on this evaluation. The result is based on a same-day holdout and does not establish multi-day or multi-week generalisation.

A separate comparison against Google Maps was conducted for five origin–destination journeys at the same departure times:

| Journey | Planner | Google Maps | Difference |
|---|---:|---:|---:|
| Moti Nagar → Karol Bagh | 37 min | 32 min | +5 min |
| Ashok Vihar → CP | 53 min | 47 min | +6 min |
| Shastri Nagar → Govindpuri | 102 min | 89 min | +13 min |
| Lajpat Nagar → Shastri Nagar | 92 min | 71 min | +21 min |
| Laxmi Nagar → Rajinder Nagar | 59 min | 46 min | +13 min |

Mean absolute difference: **11.6 minutes**. This five-trip comparison is an external reference comparison, not ground-truth bus travel-time validation.

## Limitations

- Training history covers one day of self-recorded GTFS-RT data, not several weeks of historical GPS traces.
- The observed-trip holdout is from later on the same day.
- On the reported holdout, the timetable baseline outperformed the learned model.
- Sparse data causes substantial use of the timetable fallback (77.4% of weekday estimates and 100% of weekend estimates in the final artifact's coverage summary).
- Holidays, weather and special events are not explicitly modelled.
- Live correction was functionally tested, but an accuracy improvement has not yet been established.
- The planner currently supports direct and one-transfer bus journeys; metro and other transport modes are outside scope.

## Reproducibility and artifact notes

A fresh clone requires the following local inputs or generated outputs:

- Static GTFS dataset under `data/gtfs/`
- Recorded GTFS-RT CSV files under `data/snap/`
- Trained model artifacts under `artifacts/delhi_real/`

Do not commit API keys, feed credentials, private raw GPS recordings or large generated model files unless the hackathon submission explicitly requires them and sharing is permitted.

## Team

- Team name: SAFARAI
- Members: Siddharth Verma & Pranav Aggarwal

## Acknowledgements

Built for Transport Stack Hackathon 2026, Phase 1, Problem 1.
