import json
from pathlib import Path

import pytest

from guardrails import check_feed_allows_writes, parse_feed_status, validate_ota_payload

CASES = json.loads((Path(__file__).resolve().parent.parent / "testdata" / "ota_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_cases(case):
    ok, message = validate_ota_payload(
        {"device_id": case["device_id"], "parameter": case["parameter"], "value": case["value"]}
    )
    assert ok is case["valid"], message


def test_there_are_enough_shared_cases():
    assert len(CASES) >= 25


@pytest.mark.parametrize("value", [50, 50.0, None, True, ["50"], {"v": 50}])
def test_non_string_values_are_rejected_without_crashing(value):
    ok, message = validate_ota_payload({"device_id": "car_012", "parameter": "speed_governor", "value": value})
    assert ok is False
    assert "string" in message


def test_missing_keys_and_non_dict_input_are_rejected():
    assert validate_ota_payload({})[0] is False
    assert validate_ota_payload({"device_id": "car_012"})[0] is False
    assert validate_ota_payload(None)[0] is False
    assert validate_ota_payload("send_ota_command")[0] is False


# --- stale-feed guard ---

def test_parse_feed_status_reads_scan_reply():
    assert parse_feed_status(json.dumps({"feed_status": "ok", "flagged": []})) == "ok"
    assert parse_feed_status(json.dumps({"feed_status": "stale"})) == "stale"
    assert parse_feed_status(json.dumps({"feed_status": "empty"})) == "empty"


@pytest.mark.parametrize("text", ["", "not json", "[]", "null", "42", json.dumps({}), json.dumps({"feed_status": 3}), None])
def test_parse_feed_status_returns_none_for_anything_else(text):
    assert parse_feed_status(text) is None


def test_writes_allowed_only_when_feed_ok():
    assert check_feed_allows_writes("ok")[0] is True


def test_writes_blocked_before_any_scan_or_after_an_unreadable_scan():
    ok, message = check_feed_allows_writes(None)
    assert ok is False
    assert "scan_fleet" in message
    assert check_feed_allows_writes(parse_feed_status("not json"))[0] is False


@pytest.mark.parametrize("status", ["stale", "empty", "weird", ""])
def test_writes_blocked_when_feed_is_not_ok(status):
    ok, message = check_feed_allows_writes(status)
    assert ok is False
    assert "STALE TELEMETRY" in message
