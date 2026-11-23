> **Synthetic data.** These numbers only show that the pipeline runs end to end. They are not evidence of
> accuracy on Delhi buses. Re-run with the real held-out data before reporting anything.

Held-out days from 2026-11-05, 4124 observed stop-to-stop passes.

| Group | n | Model MAE (min) | Model MAPE | Timetable MAE (min) | Timetable MAPE |
|---|---|---|---|---|---|
| all | 4124 | 1.18 | 14.5% | 2.21 | 20.2% |
| peak=off-peak | 3286 | 1.19 | 15.8% | 1.35 | 14.9% |
| peak=peak | 838 | 1.15 | 9.4% | 5.57 | 41.1% |
| daytype=wd | 2219 | 1.16 | 13.4% | 2.96 | 25.1% |
| daytype=we | 1905 | 1.22 | 15.8% | 1.32 | 14.6% |
| conf=high | 4113 | 1.19 | 14.5% | 2.21 | 20.3% |
| conf=low | 2 | 0.59 | 14.4% | 1.33 | 12.7% |
| conf=medium | 9 | 0.8 | 13.0% | 1.28 | 10.0% |
