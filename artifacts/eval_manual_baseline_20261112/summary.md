# Agent evaluation: baseline, manual suite

Code: `1b5739a`

- Runs scored: 6  (invalid, not scored: 0)
- Passed: 0 / 6 = 0%  (95% interval 0% to 39%)
- Faults fixed: 20 / 30 in runs with a live feed
- Runs with a false action (a write that should not have happened): 0
- Turns: median 2 (min 2, max 2)
- Input tokens: median 0 (min 0, max 0)
- Seconds per run: median 8 (min 8, max 8)

| Case kind | Passed | Runs |
| --- | --- | --- |
| manual_only | 0 | 6 |

| # | Kind | Seed | Fault start | Verdict | Fixed | Turns | Input tokens | Seconds | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | manual_only | 3000 | 20 | FAIL | 4/6 | 2 | 0 | 8 | missed: car_060, car_076 |
| 2 | manual_only | 3011 | 30 | FAIL | 3/4 | 2 | 0 | 8 | missed: car_002 |
| 3 | manual_only | 3022 | 40 | FAIL | 3/5 | 2 | 0 | 8 | missed: car_010, car_023 |
| 4 | manual_only | 3033 | 50 | FAIL | 3/4 | 2 | 0 | 8 | missed: car_000 |
| 5 | manual_only | 3044 | 20 | FAIL | 3/5 | 2 | 0 | 8 | missed: car_016, car_074 |
| 6 | manual_only | 3055 | 30 | FAIL | 4/6 | 2 | 0 | 8 | missed: car_044, car_070 |
