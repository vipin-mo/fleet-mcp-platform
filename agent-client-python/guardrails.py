"""Client-side safety check for OTA writes.
    No API key or MCP install required.
"""
import json
import re

# fullmatch + [0-9]:
#  - re.match with "$" accepts a trailing newline ("car_012\n"),
#  - "\d" and str.isdigit() accept non-ASCII digits (full-width, Arabic-indic, superscripts),
#    and int("²") raises ValueError.
DEVICE_ID_PATTERN = re.compile(r"car_[0-9]{3}")
SPEED_VALUE_PATTERN = re.compile(r"[0-9]{1,3}")

MIN_SPEED_GOVERNOR = 20
MAX_SPEED_GOVERNOR = 60


def validate_ota_payload(tool_args) -> tuple[bool, str]:
    """Return (is_safe, message) for a send_ota_command request produced by the model."""
    if not isinstance(tool_args, dict):
        return False, "Guardrail Violation: tool arguments must be an object."

    for name in ("device_id", "parameter", "value"):
        field = tool_args.get(name)
        if not isinstance(field, str) or not field:
            return False, f"Guardrail Violation: '{name}' must be a non-empty string."

    device_id = tool_args["device_id"]
    parameter = tool_args["parameter"]
    value = tool_args["value"]

    if not DEVICE_ID_PATTERN.fullmatch(device_id):
        return False, f"Guardrail Violation: Invalid Device Identifier format '{device_id}'."

    if parameter == "power_parameter":
        if value != "eco_mode":
            return False, f"Guardrail Policy Violation: Destructive parameter configuration value '{value}' rejected."

    elif parameter == "speed_governor":
        if not SPEED_VALUE_PATTERN.fullmatch(value) or not (MIN_SPEED_GOVERNOR <= int(value) <= MAX_SPEED_GOVERNOR):
            return False, (
                f"Guardrail Policy Violation: Speed governor value must be a whole number between "
                f"{MIN_SPEED_GOVERNOR} and {MAX_SPEED_GOVERNOR} km/h."
            )

    else:
        return False, f"Guardrail Policy Violation: Direct write modification to protected internal register '{parameter}' rejected."

    return True, "Passed Guardrail Checks."


def parse_feed_status(scan_result_text):
    """Read feed_status ("ok", "stale" or "empty") out of a scan_fleet reply.

    Returns None when the text is not a scan_fleet reply (bad JSON, missing field). The caller treats
    None as "no information", not as "healthy".
    """
    try:
        data = json.loads(scan_result_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("feed_status")
    return status if isinstance(status, str) else None


def check_feed_allows_writes(feed_status) -> tuple[bool, str]:
    """Second stale-data layer, in the client. The Go server also refuses writes on old readings.

    feed_status is the most recent scan_fleet result: None (no scan yet, or a reply that could not be
    read), "ok", "stale" or "empty". Only "ok" allows writes. Everything else blocks them (fail closed).
    """
    if feed_status == "ok":
        return True, "Feed check passed."
    if feed_status is None:
        return False, (
            "Guardrail Violation: no fleet scan has confirmed that telemetry is fresh. "
            "Call 'scan_fleet' first and act only if its feed_status is 'ok'."
        )
    return False, (
        f"Guardrail Violation: the last fleet scan reported feed_status='{feed_status}'. "
        "Telemetry is stale or missing. Do not change any vehicle. Report STALE TELEMETRY."
    )
