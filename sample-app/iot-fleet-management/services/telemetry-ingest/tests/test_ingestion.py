"""Tests for telemetry ingestion logic."""

import pytest
from app.ingestion import process_telemetry
from app.models import TelemetryPayload


def test_basic_telemetry():
    payload = TelemetryPayload(
        device_id="device-001",
        fleet_id="fleet-alpha",
        timestamp="2024-03-15T10:30:00Z",
        temperature_c=25.5,
        battery_percent=80.0,
        speed_kmh=60.0,
    )
    record = process_telemetry(payload)
    assert record.device_id == "device-001"
    assert record.temperature_c == 25.5


def test_timestamp_with_timezone_offset():
    """BUG: This test demonstrates the timestamp drift issue.
    A device in IST (+05:30) reports 14:30 local = 09:00 UTC.
    The stored timestamp should be normalized to UTC."""
    payload = TelemetryPayload(
        device_id="device-002",
        fleet_id="fleet-beta",
        timestamp="2024-03-15T14:30:00+05:30",
        temperature_c=30.0,
    )
    record = process_telemetry(payload)
    # BUG: This assertion shows the bug — timestamp is stored as-is
    # with the offset, not converted to UTC
    assert "+05:30" in record.device_timestamp  # Bug: should be UTC


def test_invalid_timestamp_rejected():
    payload = TelemetryPayload(
        device_id="device-003",
        fleet_id="fleet-gamma",
        timestamp="not-a-timestamp",
        temperature_c=20.0,
    )
    with pytest.raises(ValueError, match="Invalid timestamp"):
        process_telemetry(payload)


def test_temperature_exceeds_maximum():
    payload = TelemetryPayload(
        device_id="device-004",
        fleet_id="fleet-alpha",
        timestamp="2024-03-15T10:00:00Z",
        temperature_c=200.0,
    )
    with pytest.raises(ValueError, match="exceeds sensor maximum"):
        process_telemetry(payload)


def test_speed_exceeds_maximum():
    payload = TelemetryPayload(
        device_id="device-005",
        fleet_id="fleet-alpha",
        timestamp="2024-03-15T10:00:00Z",
        speed_kmh=400.0,
    )
    with pytest.raises(ValueError, match="exceeds fleet maximum"):
        process_telemetry(payload)
