#!/bin/sh
# End-to-end demo on SYNTHETIC data. Takes a couple of minutes.
set -e
pip install -r requirements.txt
python -m busgps.synth --out data/synth
python -m busgps.pipeline --gtfs data/synth --gps data/synth/gps.csv.gz --out artifacts/synth --train-until 2026-11-05
python -m busgps.validate --gps data/synth/gps.csv.gz --artifacts artifacts/synth
echo "Starting API on http://127.0.0.1:8000 (open it in a browser)"
ARTIFACTS=artifacts/synth uvicorn busgps.api:app --port 8000
