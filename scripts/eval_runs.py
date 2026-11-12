"""Run the agent many times against different simulated fleets and score every run.

Needs Redis running (make up), the Go binary built (make build) and ANTHROPIC_API_KEY in .env.
Each case: start the simulator with a new seed and layout, wait for the faults to develop, run the
agent once, stop the simulator, score the run against the simulator's ground truth.

    python scripts/eval_runs.py --runs 20                 # about 20 minutes, a few dollars of API use
    python scripts/eval_runs.py --runs 12 --suite hard    # harder cases: dual faults, silent faulty cars, slow ramps
    python scripts/eval_runs.py --runs 6 --suite manual   # a fault whose fix is only in the manual (rules cannot pass)
    python scripts/eval_runs.py --runs 20 --agent baseline   # the rule-based agent: same cases, no API key, no cost
    python scripts/eval_runs.py --runs 3 --seed-base 500  # quick check
    python scripts/eval_runs.py --score-ledger logs/fleet_agent_ledger_2026-10-06.jsonl \\
        --expect default --session-start 2026-10-06T18:37     # score runs you already made

Results go to eval_results/<timestamp>/ (summary.md, results.jsonl, and one folder per case with the
simulator log, agent output, ledger slice and ground truth). Copy summary.md into the README.
"""
import argparse
import functools
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_scoring import (aggregate, default_manifest, parse_ledger, render_summary,  # noqa: E402
                          score_session, split_sessions, summarize_session)

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "mcp-server-go" / "mcp-server-go-binary"
AGENT_SCRIPTS = {"llm": ROOT / "agent-client-python" / "main.py",
                 "baseline": ROOT / "agent-client-python" / "baseline_agent.py"}

# One round of the suite. Mostly random fault layouts, plus the controls that catch false alarms.
SUITE_PATTERN = ["random", "random", "none", "random", "spikes_only",
                 "random", "silent", "random", "freeze", "random"]
# Harder cases (--suite hard). Each kind is one fault type the standard suite never uses.
HARD_PATTERN = ["dual", "silent_fault", "slow_ramp"]
# --suite manual: coolant-leak cars whose fix (speed_governor=30) is written only in the manual, next to
# ordinary cabin cars that need 50. The rule-based agent has no rule for it, so this is where the model can win.
MANUAL_PATTERN = ["manual_only"]
SLOW_RAMP_HEAT = 0.15             # degrees C per second: the detector needs 8 to 11 s to see it (docs/DETECTION_LATENCY.md)
SLOW_RAMP_LEAD_SECONDS = 25       # long enough that even the slowest measured detection has happened
FAULT_STARTS = [20, 30, 40, 50]   # the detector needs about 15 readings of history before faults
STALE_GAP_SECONDS = 20            # how long the feed is dead before the agent starts (limit is 15 s)
SILENT_CARS = 5
SILENT_MIN_AGE_SECONDS = 25       # silent cars must be clearly older than the 15 s limit when the agent scans
FREEZE_AFTER_FAULT_START = 15     # ticks of faults written before the feed dies


def build_suite(runs, seed_base, suite="standard"):
    """Return the list of cases. The same arguments always give the same suite."""
    pattern = {"hard": HARD_PATTERN, "manual": MANUAL_PATTERN}.get(suite, SUITE_PATTERN)
    cases = []
    for i in range(runs):
        kind = pattern[i % len(pattern)]
        fault_start = FAULT_STARTS[i % len(FAULT_STARTS)]
        profile = {"silent": "random", "freeze": "random", "dual": "dual", "silent_fault": "dual",
                   "slow_ramp": "battery_only"}.get(kind, kind)
        cases.append({
            "index": i + 1, "kind": kind, "seed": seed_base + i * 11, "fault_start": fault_start, "profile": profile,
            "silent_cars": SILENT_CARS if kind == "silent" else 0,
            "silent_faulty": 1 if kind == "silent_fault" else 0,
            "heat_per_tick": SLOW_RAMP_HEAT if kind == "slow_ramp" else None,
            "freeze_after": fault_start + FREEZE_AFTER_FAULT_START if kind == "freeze" else None,
        })
    return cases


def wait_seconds(case, lead):
    """How long to let the simulator run before the agent starts."""
    if case["freeze_after"] is not None:
        return case["freeze_after"] + STALE_GAP_SECONDS
    if case["silent_cars"] or case.get("silent_faulty"):
        # silent cars must be older than the 15 s freshness limit when the agent scans
        return case["fault_start"] + max(lead, SILENT_MIN_AGE_SECONDS)
    if case.get("heat_per_tick"):
        return case["fault_start"] + max(lead, SLOW_RAMP_LEAD_SECONDS)
    return case["fault_start"] + lead


def simulator_command(case, manifest_path):
    cmd = [sys.executable, "-u", str(ROOT / "fleet-simulator" / "simulator.py"),
           "--seed", str(case["seed"]), "--fault-start-tick", str(case["fault_start"]),
           "--profile", case["profile"], "--manifest", str(manifest_path)]
    if case["silent_cars"]:
        cmd += ["--silent-cars", str(case["silent_cars"])]
    if case.get("silent_faulty"):
        cmd += ["--silent-faulty", str(case["silent_faulty"])]
    if case.get("heat_per_tick"):
        cmd += ["--heat-per-tick", str(case["heat_per_tick"])]
    if case["freeze_after"] is not None:
        cmd += ["--freeze-after", str(case["freeze_after"])]
    return cmd


def code_version(run=subprocess.run):
    """The commit the run used, so a result can be tied to the code that produced it.

    "<sha> + uncommitted changes" when tracked files differ from that commit; "unknown" outside git."""
    try:
        sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                    capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha} + uncommitted changes" if dirty else sha


def ledger_path():
    return ROOT / "logs" / f"fleet_agent_ledger_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"


def preflight():
    """Return an error message if the environment is not ready, else None."""
    if not BINARY.exists():
        return f"Go binary not found at {BINARY}. Run 'make build'."
    try:
        import redis
        redis.Redis().ping()
    except Exception as exc:   # noqa: BLE001 - any failure means "not ready"
        return f"Cannot reach Redis on localhost:6379 ({exc}). Run 'make up'."
    return None


def start_simulator(case, case_dir):
    manifest = case_dir / "manifest.json"
    log = open(case_dir / "simulator.log", "w", encoding="utf-8")
    proc = subprocess.Popen(simulator_command(case, manifest), cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("simulator exited early; see simulator.log")
        if manifest.exists() and "Faulty cars" in (case_dir / "simulator.log").read_text(encoding="utf-8"):
            return proc, manifest
        time.sleep(0.2)
    proc.terminate()
    raise RuntimeError("simulator did not start within 20 s")


def stop_simulator(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_agent(case_dir, timeout, name="llm"):
    """Run the agent once. `name` is "llm" (Claude, needs an API key) or "baseline" (rules, free).
    Returns (return code, ledger text written during the run)."""
    path = ledger_path()
    offset = path.stat().st_size if path.exists() else 0
    with open(case_dir / "agent.log", "w", encoding="utf-8") as out:
        try:
            proc = subprocess.run([sys.executable, str(AGENT_SCRIPTS[name])],
                                  cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = -9
    text = ""
    if path.exists():
        with open(path, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", errors="replace")
    (case_dir / "ledger.jsonl").write_text(text, encoding="utf-8")
    return code, text


def score_case(case, manifest, ledger_text, return_code):
    """Turn one agent run into a result dict. Pure: no processes, no files."""
    sessions = split_sessions(parse_ledger(ledger_text))
    base = {"index": case["index"], "kind": case["kind"], "seed": case["seed"], "fault_start": case["fault_start"]}
    if not sessions:
        failed = [e for e in parse_ledger(ledger_text) if e["event_type"] == "STARTUP_FAILED"]
        reason = f": {failed[-1]['payload'].get('error', '').splitlines()[0]}" if failed else ""
        return {**base, "error": f"agent wrote no session to the ledger (exit code {return_code}){reason}; see agent.log"}
    summary = summarize_session(sessions[-1])
    if summary["abort_reason"] == "exception":
        # The run crashed (bad API key, API outage, server crash). That says nothing about the agent's
        # decisions, so it is not scored. The error text is kept so the cause is visible in the summary.
        error = (summary["abort_error"] or "unknown error").replace("\n", " ")
        return {**base, "error": f"agent crashed: {error[:300]}; see agent.log"}
    result = score_session(manifest, summary)
    result.update(base)
    result["manifest"] = {k: manifest.get(k) for k in
                          ("battery", "cabin", "dual", "coolant", "spikes", "silent", "silent_faulty", "heat_per_tick", "freeze_after")}
    return result


def run_case(case, case_dir, lead, agent_timeout, sleep=time.sleep,
             start=start_simulator, stop=stop_simulator, agent=run_agent):
    case_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc, manifest_path = start(case, case_dir)
    except RuntimeError as exc:
        return {"index": case["index"], "kind": case["kind"], "seed": case["seed"],
                "fault_start": case["fault_start"], "error": str(exc)}
    try:
        sleep(wait_seconds(case, lead))
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        code, text = agent(case_dir, agent_timeout)
    finally:
        stop(proc)
    return score_case(case, manifest, text, code)


def write_results(out_dir, results, title, version=None):
    with open(out_dir / "results.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    summary = render_summary(results, title, version)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    return summary


def score_existing(args):
    """Score sessions already in a ledger file. Every selected session gets the same expectation."""
    text = Path(args.score_ledger).read_text(encoding="utf-8")
    manifest = default_manifest(args.expect)
    results = []
    for n, events in enumerate(split_sessions(parse_ledger(text)), 1):
        start = events[0].get("timestamp", "")
        if args.session_start and not any(start.startswith(p) for p in args.session_start):
            continue
        result = score_session(manifest, summarize_session(events))
        result.update({"index": n, "kind": args.expect, "seed": "?", "fault_start": "?"})
        results.append(result)
    if not results:
        print("No matching sessions.")
        return 1
    print(render_summary(results, f"Scored from {args.score_ledger} (expectation: {args.expect})"))
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--seed-base", type=int, default=1000)
    p.add_argument("--lead", type=int, default=15,
                   help="seconds to let faults develop before the agent starts (default 15)")
    p.add_argument("--agent-timeout", type=int, default=240)
    p.add_argument("--max-invalid-streak", type=int, default=3,
                   help="stop the suite after this many invalid cases in a row (a setup problem repeats "
                        "every time; there is no point in waiting 20 minutes to learn that)")
    p.add_argument("--suite", choices=["standard", "hard", "manual"], default="standard",
                   help="standard: random faults and controls. hard: dual-fault cars, faulty cars that go "
                        "silent, slow ramps. manual: a fault whose fix is only in the manual")
    p.add_argument("--agent", choices=["llm", "baseline"], default="llm",
                   help="llm: the Claude agent (API key, costs money). baseline: the rule-based agent "
                        "(no key, free), run on the same cases")
    p.add_argument("--only", help="comma-separated case kinds to keep from the suite "
                                  "(standard: random, none, spikes_only, silent, freeze; hard: dual, silent_fault, slow_ramp; manual: manual_only)")
    p.add_argument("--out", default=str(ROOT / "eval_results"))
    p.add_argument("--score-ledger", help="score sessions from an existing ledger instead of running")
    p.add_argument("--expect", choices=["default", "none", "stale"], default="default",
                   help="with --score-ledger: truth for those sessions (default fixed cars / healthy fleet / dead feed)")
    p.add_argument("--session-start", action="append",
                   help="with --score-ledger: only sessions whose start timestamp begins with this (repeatable)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.score_ledger:
        return score_existing(args)

    problem = preflight()
    if problem:
        print(problem)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = build_suite(args.runs, args.seed_base, args.suite)
    if args.only:
        wanted = {k.strip() for k in args.only.split(",")}
        cases = [c for c in cases if c["kind"] in wanted]
        if not cases:
            print(f"No case of kind {sorted(wanted)} in the first {args.runs} cases of the suite.")
            return 1
    version = code_version()
    print(f"{len(cases)} cases, results in {out_dir}, code {version}")

    results = []
    invalid_streak = 0
    for position, case in enumerate(cases, 1):
        label = f"[{position}/{len(cases)}] {case['kind']} seed={case['seed']} fault_start={case['fault_start']}"
        print(label, flush=True)
        result = run_case(case, out_dir / f"case_{case['index']:02d}_{case['kind']}", args.lead, args.agent_timeout,
                          agent=functools.partial(run_agent, name=args.agent))
        result["agent"] = args.agent
        result["code_version"] = version
        results.append(result)
        verdict = "INVALID: " + result["error"] if result.get("error") else ("PASS" if result["passed"] else "FAIL: " + "; ".join(result["reasons"]))
        print("   ->", verdict, flush=True)
        write_results(out_dir, results, f"Agent evaluation: {args.agent}, {args.suite} suite (in progress)", version)
        invalid_streak = invalid_streak + 1 if result.get("error") else 0
        if invalid_streak >= args.max_invalid_streak:
            print(f"\nStopping: {invalid_streak} invalid cases in a row. Fix the problem below, then rerun.")
            print(f"First error: {result['error']}")
            break

    print()
    print(write_results(out_dir, results, f"Agent evaluation: {args.agent}, {args.suite} suite", version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
