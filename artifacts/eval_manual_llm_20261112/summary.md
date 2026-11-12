# Agent evaluation: llm, manual suite

Code: `3a44bc0`

- Runs scored: 6  (invalid, not scored: 0)
- Passed: 6 / 6 = 100%  (95% interval 61% to 100%)
- Faults fixed: 30 / 30 in runs with a live feed
- Runs with a false action (a write that should not have happened): 0
- Turns: median 6 (min 6, max 6)
- Input tokens: median 28,882 (min 26,249, max 29,827)
- Seconds per run: median 28 (min 24, max 29)

| Case kind | Passed | Runs |
| --- | --- | --- |
| manual_only | 6 | 6 |

| # | Kind | Seed | Fault start | Verdict | Fixed | Turns | Input tokens | Seconds | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | manual_only | 3000 | 20 | PASS | 6/6 | 6 | 29,827 | 29 |  |
| 2 | manual_only | 3011 | 30 | PASS | 4/4 | 6 | 27,006 | 27 |  |
| 3 | manual_only | 3022 | 40 | PASS | 5/5 | 6 | 28,600 | 24 |  |
| 4 | manual_only | 3033 | 50 | PASS | 4/4 | 6 | 26,249 | 25 |  |
| 5 | manual_only | 3044 | 20 | PASS | 5/5 | 6 | 29,164 | 29 |  |
| 6 | manual_only | 3055 | 30 | PASS | 6/6 | 6 | 29,826 | 28 |  |
