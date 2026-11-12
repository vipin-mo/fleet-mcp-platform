# How fast does the detector notice a battery fault?

Measured on the real Go detector (`mcp-server-go/server/anomaly.go`) with series made by the simulator
(`scripts/gen_detector_series.py` writes `testdata/detector_series.json`). The detector sees the newest
60 readings at every tick, as the server does. One reading is one second, so ticks are seconds.

Reproduce: `go test ./server -run TestDetectionLatencyReport -v` (from `mcp-server-go/`).

## Time to flag a ramp

Each rate was run with 10 different seeds. "Flagged" means `sustained_anomaly` or `rising_trend`, the two
classifications the agent acts on. The fault begins at second 60.

| Heating rate (°C per second) | Runs | Missed | Seconds to flag: fastest / median / slowest | Rise above normal when flagged (°C, median) |
| --- | --- | --- | --- | --- |
| 0.10 | 10 | 0 | 12 / 14 / 17 | 1.3 |
| 0.15 | 10 | 0 | 8 / 9 / 11 | 1.4 |
| 0.20 | 10 | 0 | 6 / 7 / 9 | 1.3 |
| 0.25 | 10 | 0 | 4 / 6 / 7 | 1.7 |
| 0.30 | 10 | 0 | 2 / 5 / 5 | 1.5 |
| 0.35 | 10 | 0 | 2 / 4 / 5 | 1.5 |
| 0.40 | 10 | 0 | 2 / 2 / 3 | 1.3 |
| 0.60 | 10 | 0 | 1 / 2 / 2 | 1.6 |

What this says:

- The detector flags a fault when the battery is about 1.3 to 1.7 °C above its normal level, whatever the
  heating rate. That is the 0.5 °C minimum spread (the "floor") times the 2.5 limit. So the time to flag is
  roughly 1.4 divided by the heating rate: a 0.1 °C/s fault takes about 14 s, the default 0.6 °C/s about 2 s.
- No ramp was missed, and none was flagged before the fault began.
- The 0.5 °C floor is tuned to the simulator's sensor noise (0.15 °C). A real battery with larger natural
  swings would need a different floor, and this table would change.

## False alarms

- 20 healthy cars, 200 readings each (4,000 readings): none flagged, none called a spike.
- 10 cars with a one-reading blip every 20 s (70 blips, each watched for the 5 readings it stays in the
  detector's window): none flagged.

## A bug this study found

The first version of this test flagged 5 of the 70 blips as a `rising_trend`. The cause: the rule that tells a
blip from the start of a ramp used a slope that included the newest reading, and when that reading is the
blip, 4 of the 10 pairwise slopes are the blip itself, so ordinary sensor noise could push the median slope over
the limit. An agent that scanned at that second would have put a healthy car in `eco_mode`. The rule now looks at
the readings before the newest one. The fix makes some ramps flag up to one second later (measured before and after: 0.30 °C/s median 4 s
to 5 s, 0.60 °C/s median 1 s to 2 s; the 0.10, 0.15, 0.25 and 0.40 rows did not move). Tests that fail on the old rule:
`TestNoisySensorBeforeASpikeIsStillASpike` and `TestDetectorOnSimulatedSeries`.

The 20-fleet agent evaluation in `artifacts/eval_20261006/` ran before this fix. Blips were rarely in the
detector's window at the moment of a scan, which is why the agent never acted on one. The evaluation was
repeated on the fixed detector on 2026-10-24 with the same 20 seeds: 20 of 20 passed, 43 of 43 faults fixed
(`artifacts/eval_rerun_llm_20261024/`).
