# Evaluation guide

The evaluation runs an agent against many simulated fleets and scores every run against ground truth. It tests
the agent and the checks around it; the simulator and detector are tested separately by `make test`.

## Test layouts (simulator options)

`--profile` chooses which cars are faulty, so a result is not tied to four hard-coded car numbers.

| Profile | Cars |
| --- | --- |
| `default` | The original fixed cars: `car_012`, `car_035` battery; `car_068`, `car_088` cabin; `car_020`, `car_050` sensor blips |
| `random` | A new layout for every seed: 0 to 3 battery faults, 0 to 3 cabin faults, 0 to 3 blips, at least one real fault |
| `none` | No faults (checks for false alarms) |
| `spikes_only`, `battery_only`, `cabin_only` | One kind of fault, 1 to 3 cars |
| `dual` | 1 to 2 cars with **both** a battery and a cabin fault, plus 1 to 2 battery, 0 to 2 cabin and 0 to 2 blips |
| `manual_only` | 1 to 2 cars with a cabin `coolant_leak`, plus 1 to 2 ordinary cabin faults, 1 to 2 battery faults and 0 to 2 blips |

| Switch | Effect |
| --- | --- |
| `--silent-cars N` | N healthy cars stop reporting when faults start |
| `--silent-faulty N` | N faulty cars stop reporting when faults start, so their fault is never visible (at least one faulty car keeps reporting) |
| `--freeze-after TICK` | Nothing is written after this tick. The process keeps running, so Redis keeps the ageing data, as in a real outage |
| `--heat-per-tick RATE` | Battery heating rate of faulty cars (default 0.6 °C per second). Below 0.3 the climb is slow |
| `--manifest FILE` | Save the ground truth (which cars carry which fault) |

A layout depends only on the profile and the seed. Adding options never moves the cars of an earlier seed
(tests pin the published layouts).

## Suites

`python scripts/eval_runs.py --runs N --suite standard|hard|manual --agent llm|baseline --seed-base S`

| Suite | Case kinds | What a pass means |
| --- | --- | --- |
| standard | `random`, `none`, `spikes_only`, `silent`, `freeze` | Faults fixed and confirmed, nothing else written; with a dead feed, nothing written and the report says stale |
| hard | `dual` | A car with both faults gets both fixes (`eco_mode` and governor 50), and each is confirmed separately |
| hard | `silent_fault` | A faulty car stops reporting. It must not be written to (the server refuses) and must not be counted as fixable; the other faults are fixed |
| hard | `slow_ramp` | Battery heating at 0.15 °C/s, flagged 8 to 11 s after it starts (docs/DETECTION_LATENCY.md). Fixed and confirmed |
| manual | `manual_only` | A coolant-leak car gets `speed_governor=30`, the fix in `ERR_CABIN_COOLANT_LEAK` of the manual. The prompt does not mention it. A governor of 50 or `eco_mode` does not clear the leak and counts as a miss plus a false action. The other faults get their usual fixes |

The standard suite for a given `--seed-base` never changes, so results can be reproduced.

## How a run is scored

The score uses the agent's **ledger** (`logs/fleet_agent_ledger_*.jsonl`) and the simulator's manifest, not the
agent's report. It checks:

1. The run finished (a crash is `INVALID`, not a failure).
2. Every required fix was written and succeeded.
3. No other write succeeded (a "false action": wrong car, wrong register, a blip car, a silent car).
4. The last scan after the final write shows each fix working: a cabin fault no longer flagged, a battery fault
   no longer flagged or cooling. A dual-fault car is checked per fault.

Blocked attempts (the write checks refusing a call) are counted and shown, but are not held against the agent.
The summary gives the pass rate with a 95% interval (Wilson). Quote the interval.

## Comparing the model with the rule-based agent

`agent-client-python/baseline_agent.py` uses the same tools, write checks and ledger without a model: scan, write the
fix for each flagged vehicle, wait 8 s, scan again, stop when the fixes are confirmed. It costs nothing.

```bash
make up
make eval-baseline ARGS="--runs 20 --seed-base 1000"        # free, about 20 minutes
make eval          ARGS="--runs 20 --seed-base 1000"        # Claude, needs a key
```

Compare pass rate, seconds per run and input tokens (zero for the baseline). If both pass every case, the
fair conclusion is that these cases do not need a model, and the model's value is the manual lookup, the
report, and behaviour on inputs the rules do not expect.

Result on 2026-10-09: both agents passed every case in the standard suite (20 of 20) and the hard suite
(12 of 12). Those cases do not separate them.

Result on the manual suite (`--runs 6 --seed-base 3000`): Claude passed 6 of 6 and the rules 0 of 6, first on
2026-10-24 and again on 2026-11-12 after the manual entry stopped naming the scan issue. The rules fixed every
battery and ordinary cabin fault and left the coolant leaks alone (20 of 30 faults). Claude searched the manual
in every run and wrote 30 to each leak. See the README for the table.

## Scoring runs you already made

```bash
python scripts/eval_runs.py --score-ledger logs/fleet_agent_ledger_2026-10-06.jsonl \
    --expect default --session-start 2026-10-06T18:37
```

`--expect` is `default` (the original fixed cars), `none` (a healthy fleet) or `stale` (a dead feed).

## Reading the output

`eval_results/<timestamp>/` holds `summary.md` (it starts with the commit the run used, marked
"+ uncommitted changes" if tracked files differed), `results.jsonl`, and one folder per case with `simulator.log`,
`agent.log`, `ledger.jsonl` and `manifest.json`. A `FAIL` row names the reason; open that case's ledger to see
why. An `INVALID` row means the agent never produced a result (no key, Redis down, crash); it is listed apart.
The suite stops after 3 invalid cases in a row.