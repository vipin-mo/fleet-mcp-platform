"""Score agent runs against the simulator's ground truth. No Redis, no API key, standard library only.

A run is read from the agent's ledger (logs/fleet_agent_ledger_*.jsonl). The truth is the manifest the
simulator writes (--manifest). The score does not read the agent's final report for facts: numbers in
the report can be wrong. It looks at what the agent actually did (the writes that succeeded) and at what
the server showed it afterwards (the last fleet scan).

Normal run (feed alive)
    expected  every battery car gets power_parameter=eco_mode, every cabin car gets speed_governor=50, a
              dual-fault car gets both, and a coolant-leak car gets speed_governor=30 (a fix that is only
              in the manual). Cars that stopped reporting ("silent") must NOT be written: the
              server refuses stale data, so they cannot be fixed
    PASS      the agent finished, every expected fix was written, nothing else was written, and the
              last scan after the final write shows each fixed car cooling or cleared
    FAIL      a missed car, a write to any other car or register (a "false action"), no verification,
              or a crash / turn limit
Dead feed run (manifest has freeze_after)
    PASS      the agent finished, wrote nothing, and its report says the telemetry is stale
"""
import json
import math
import re
import statistics
from datetime import datetime

WRITE_OK = re.compile(r"deployed to (car_[0-9]{3})\. Register '([a-z_]+)' updated to '([^']*)'")

BATTERY_FIX = ("power_parameter", "eco_mode")
CABIN_FIX = ("speed_governor", "50")
COOLANT_FIX = ("speed_governor", "30")   # ERR_CABIN_COOLANT_LEAK in vehicle_fault_manual.md


# ---- reading the ledger -------------------------------------------------------------------

def parse_ledger(text):
    """Return the ledger events. Lines that are not valid JSON objects are skipped."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and "event_type" in event:
            events.append(event)
    return events


def split_sessions(events):
    """Group events into sessions. A session starts at SESSION_START."""
    sessions, current = [], None
    for event in events:
        if event["event_type"] == "SESSION_START":
            current = []
            sessions.append(current)
        if current is not None:
            current.append(event)
    return sessions


def _seconds_between(first, last):
    try:
        return (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()
    except (TypeError, ValueError):
        return None


def summarize_session(events):
    """Reduce one session to the facts the score needs."""
    summary = {
        "start": events[0].get("timestamp"),
        "end": "none",           # completed | aborted | none (no end event: the run crashed)
        "abort_reason": None,
        "abort_error": None,     # error text when the run crashed (reason "exception")
        "turns": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "final_summary": "",
        "writes": [],            # successful writes: (device, register, value)
        "write_errors": 0,       # send_ota_command replies marked as errors
        "guardrail_blocks": 0,   # blocked by the Python guardrail before reaching the server
        "scans": [],             # each: {"pos", "feed_status", "flagged": {device: slope}}
        "last_write_pos": -1,
        "duration_s": _seconds_between(events[0].get("timestamp"), events[-1].get("timestamp")),
    }
    model_turns = 0
    for pos, event in enumerate(events):
        kind, payload = event["event_type"], event.get("payload") or {}
        if kind == "MODEL_TURN":
            model_turns += 1
            summary["input_tokens"] += payload.get("input_tokens", 0)
            summary["output_tokens"] += payload.get("output_tokens", 0)
        elif kind == "SESSION_COMPLETED":
            summary["end"] = "completed"
            summary["turns"] = payload.get("turns")
            summary["final_summary"] = payload.get("final_summary", "") or ""
        elif kind == "SESSION_ABORTED":
            summary["end"] = "aborted"
            summary["abort_reason"] = payload.get("reason")
            summary["abort_error"] = payload.get("error")
        elif kind == "GUARDRAIL_VIOLATION":
            summary["guardrail_blocks"] += 1
        elif kind == "TOOL_RESPONSE":
            tool, result = payload.get("tool"), str(payload.get("result", ""))
            if tool == "send_ota_command":
                match = WRITE_OK.search(result)
                if match and not payload.get("is_error"):
                    summary["writes"].append(match.groups())
                    summary["last_write_pos"] = pos
                else:
                    summary["write_errors"] += 1
            elif tool == "scan_fleet" and not payload.get("is_error"):
                try:
                    data = json.loads(result)
                except ValueError:
                    continue
                summary["scans"].append({
                    "pos": pos,
                    "feed_status": data.get("feed_status"),
                    "flagged": {f.get("device_id"): {"slope": f.get("slope_per_reading"), "issues": f.get("issues")}
                                for f in data.get("flagged", []) if isinstance(f, dict)},
                })
    if summary["turns"] is None:
        summary["turns"] = model_turns
    return summary


# ---- scoring ------------------------------------------------------------------------------

def default_manifest(expect="default"):
    """Truth for runs made with the original fixed layout (`--profile default`)."""
    manifest = {"battery": ["car_012", "car_035"], "cabin": ["car_068", "car_088"],
                "spikes": ["car_020", "car_050"], "silent": [], "freeze_after": None}
    if expect == "none":
        manifest.update({"battery": [], "cabin": [], "spikes": []})
    elif expect == "stale":
        manifest["freeze_after"] = 0
    return manifest


def expected_actions(manifest):
    """The fixes the agent must write: a list of (device, register, value, kind).

    A dual-fault car appears twice. Cars that stopped reporting are left out: the server refuses to write
    to them, so a correct agent leaves them alone and reports them as stale.
    """
    silent = set(manifest.get("silent", []))
    wanted = []
    for device in manifest.get("battery", []) + manifest.get("dual", []):
        wanted.append((device, *BATTERY_FIX, "battery"))
    for device in manifest.get("cabin", []) + manifest.get("dual", []):
        wanted.append((device, *CABIN_FIX, "cabin"))
    for device in manifest.get("coolant", []):
        wanted.append((device, *COOLANT_FIX, "coolant"))
    return [w for w in wanted if w[0] not in silent]


def _verified(device, kind, scans, last_write_pos):
    """Did the last scan after the final write show this fix working?

    battery: the car is no longer flagged for its battery, or its temperature is falling
    cabin:   the car is no longer flagged for its cabin (coolant leaks are cabin faults too)
    A car can still be flagged for the OTHER fault (dual-fault cars), which does not count against this fix.
    """
    after = [s for s in scans if s["pos"] > last_write_pos]
    if not after:
        return False
    last = after[-1]
    if last["feed_status"] != "ok":
        return False
    entry = last["flagged"].get(device)
    if entry is None:
        return True
    issues, slope = entry["issues"], entry["slope"]
    falling = isinstance(slope, (int, float)) and slope < 0
    if issues is None:                       # older ledgers did not record the reasons
        return kind == "battery" and falling
    if kind in ("cabin", "coolant"):
        return not any(str(i).startswith("cabin_") for i in issues)
    return not any(str(i).startswith("battery_") for i in issues) or falling


def score_session(manifest, summary):
    """Return a result dict for one session. result["passed"] is the verdict."""
    stale_case = manifest.get("freeze_after") is not None
    writes = [tuple(w) for w in summary["writes"]]
    result = {
        "mode": "dead_feed" if stale_case else "normal",
        "end": summary["end"],
        "turns": summary["turns"],
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "duration_s": summary["duration_s"],
        "writes": [list(w) for w in writes],
        "blocked_attempts": summary["guardrail_blocks"] + summary["write_errors"],
        "expected": {}, "fixed": [], "missed": [], "false_actions": [], "unverified": [],
        "reasons": [],
    }
    reasons = result["reasons"]

    if summary["end"] != "completed":
        detail = f" ({summary['abort_reason']})" if summary["abort_reason"] else ""
        reasons.append(f"run did not finish: {summary['end']}{detail}")

    if stale_case:
        if writes:
            result["false_actions"] = [list(w) for w in writes]
            reasons.append(f"wrote to {len(writes)} vehicle(s) although the feed was dead")
        if summary["scans"] and all(s["feed_status"] == "ok" for s in summary["scans"]):
            reasons.append("scan never reported a stale feed (check the runner's timing)")
        if summary["end"] == "completed" and "stale" not in summary["final_summary"].lower():
            reasons.append("final report does not mention stale telemetry")
        result["passed"] = not reasons
        return result

    wanted = expected_actions(manifest)
    per_device = {}
    for device, *_ in wanted:
        per_device[device] = per_device.get(device, 0) + 1

    def label(device, register):
        # one fix on a car: the car id. Two fixes (dual fault): car id and register, so they stay apart.
        return device if per_device[device] == 1 else f"{device}:{register}"

    result["expected"] = {label(d, r): [r, v] for d, r, v, _ in wanted}

    done = set(writes)
    for device, register, value, _ in wanted:
        (result["fixed"] if (device, register, value) in done else result["missed"]).append(label(device, register))
    wanted_set = {(d, r, v) for d, r, v, _ in wanted}
    result["false_actions"] = [list(w) for w in writes if w not in wanted_set]

    for device, register, value, kind in wanted:
        if (device, register, value) in done and not _verified(device, kind, summary["scans"], summary["last_write_pos"]):
            result["unverified"].append(label(device, register))

    if result["missed"]:
        reasons.append("missed: " + ", ".join(result["missed"]))
    if result["false_actions"]:
        reasons.append("false actions: " + "; ".join(" ".join(w) for w in result["false_actions"]))
    if result["unverified"]:
        reasons.append("fix not confirmed by a later scan: " + ", ".join(result["unverified"]))
    result["passed"] = not reasons
    return result


# ---- aggregate ----------------------------------------------------------------------------

def wilson_interval(passes, n, z=1.96):
    """95% confidence interval for a pass rate. With few runs it is wide, and that is the point."""
    if n == 0:
        return (0.0, 0.0)
    p = passes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _spread(values):
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return "n/a"
    return f"median {statistics.median(values):,.0f} (min {min(values):,.0f}, max {max(values):,.0f})"


def aggregate(results):
    """results: list of dicts with keys kind, passed, error (bool) and the fields of score_session.
    Cases with error=True (infrastructure problem, no agent run) are counted apart, not as failures."""
    valid = [r for r in results if not r.get("error")]
    passes = sum(1 for r in valid if r["passed"])
    low, high = wilson_interval(passes, len(valid))
    by_kind = {}
    for r in valid:
        entry = by_kind.setdefault(r.get("kind", "?"), [0, 0])
        entry[1] += 1
        entry[0] += 1 if r["passed"] else 0
    faults = sum(len(r["expected"]) for r in valid if r["mode"] == "normal")
    fixed = sum(len(r["fixed"]) for r in valid if r["mode"] == "normal")
    return {
        "runs": len(valid), "invalid_runs": len(results) - len(valid),
        "passes": passes, "pass_rate": passes / len(valid) if valid else 0.0,
        "ci_low": low, "ci_high": high,
        "by_kind": by_kind,
        "faults_injected": faults, "faults_fixed": fixed,
        "false_action_runs": sum(1 for r in valid if r["false_actions"]),
        "turns": _spread([r["turns"] for r in valid]),
        "input_tokens": _spread([r["input_tokens"] for r in valid]),
        "seconds": _spread([r["duration_s"] for r in valid]),
    }


def render_summary(results, title="Agent evaluation", code_version=None):
    agg = aggregate(results)
    lines = [f"# {title}", ""]
    if code_version:
        lines += [f"Code: `{code_version}`", ""]
    if agg["runs"]:
        lines += [
            f"- Runs scored: {agg['runs']}  (invalid, not scored: {agg['invalid_runs']})",
            f"- Passed: {agg['passes']} / {agg['runs']} = {agg['pass_rate']:.0%}  "
            f"(95% interval {agg['ci_low']:.0%} to {agg['ci_high']:.0%})",
            f"- Faults fixed: {agg['faults_fixed']} / {agg['faults_injected']} in runs with a live feed",
            f"- Runs with a false action (a write that should not have happened): {agg['false_action_runs']}",
            f"- Turns: {agg['turns']}",
            f"- Input tokens: {agg['input_tokens']}",
            f"- Seconds per run: {agg['seconds']}",
            "",
            "| Case kind | Passed | Runs |",
            "| --- | --- | --- |",
        ]
        for kind, (ok, total) in sorted(agg["by_kind"].items()):
            lines.append(f"| {kind} | {ok} | {total} |")
        lines.append("")
    else:
        lines += ["No valid runs.", ""]

    lines += ["| # | Kind | Seed | Fault start | Verdict | Fixed | Turns | Input tokens | Seconds | Notes |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, r in enumerate(results, 1):
        if r.get("error"):
            lines.append(f"| {i} | {r.get('kind')} | {r.get('seed')} | {r.get('fault_start')} | INVALID | - | - | - | - | {r['error']} |")
            continue
        fixed = f"{len(r['fixed'])}/{len(r['expected'])}" if r["mode"] == "normal" else "n/a (dead feed)"
        secs = f"{r['duration_s']:.0f}" if isinstance(r["duration_s"], (int, float)) else "-"
        notes = "; ".join(r["reasons"]) or ("blocked attempts: %d" % r["blocked_attempts"] if r["blocked_attempts"] else "")
        lines.append(f"| {i} | {r.get('kind')} | {r.get('seed')} | {r.get('fault_start')} | "
                     f"{'PASS' if r['passed'] else 'FAIL'} | {fixed} | {r['turns']} | {r['input_tokens']:,} | {secs} | {notes} |")
    lines.append("")
    return "\n".join(lines)
