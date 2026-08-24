"""
Telemetry ingestion logic — validates and normalizes device payloads.

BUG: Timestamp handling does not normalize device-reported timestamps to UTC.
Devices in different timezones report local time (e.g., "2024-03-15T14:30:00+05:30")
but this service stores the timestamp string as-is without converting to UTC.
This causes:
  - Out-of-order writes when sorting by device_timestamp across timezones
  - Alert-engine sliding windows evaluate wrong time ranges
  - Geofence "time in zone" calculations are off by timezone offset
"""

from datetime import datetime, timezone

from app.models import TelemetryPayload, TelemetryRecord


def process_telemetry(payload: TelemetryPayload) -> TelemetryRecord:
    """Validate and normalize a telemetry payload into a storage record."""
    _validate_payload(payload)
    device_ts = _parse_device_timestamp(payload.timestamp)
    return _build_record(payload, device_ts)


def _validate_payload(payload: TelemetryPayload) -> None:
    """Business-rule validation beyond Pydantic schema checks."""
    if payload.temperature_c is not None and payload.temperature_c > 150:
        raise ValueError(f"Temperature {payload.temperature_c}°C exceeds sensor maximum (150°C)")

    if payload.speed_kmh is not None and payload.speed_kmh > 300:
        raise ValueError(f"Speed {payload.speed_kmh} km/h exceeds fleet maximum (300 km/h)")

    if payload.engine_rpm is not None and payload.engine_rpm > 12000:
        raise ValueError(f"Engine RPM {payload.engine_rpm} exceeds sensor maximum (12000)")


def _parse_device_timestamp(timestamp_str: str) -> str:
    """
    Parse the device-reported timestamp.

    BUG: This function accepts timestamps with timezone offsets (e.g.,
    "2024-03-15T14:30:00+05:30") but does NOT convert them to UTC before
    storing. It just stores the raw string. Downstream services that sort
    or window by this field assume UTC, causing out-of-order processing
    when devices report in local timezones.

    The correct behavior would be to parse the timezone-aware timestamp
    and convert it to UTC before storing.
    """
    try:
        parsed = datetime.fromisoformat(timestamp_str)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid timestamp format: {timestamp_str}") from e

    if parsed.year < 2020 or parsed.year > 2030:
        raise ValueError(f"Timestamp {timestamp_str} outside valid range (2020-2030)")

    # BUG: Should convert to UTC here but doesn't.
    # Correct fix: return parsed.astimezone(timezone.utc).isoformat()
    return timestamp_str


def _build_record(payload: TelemetryPayload, device_ts: str) -> TelemetryRecord:
    """Construct a TelemetryRecord from the validated payload."""
    record = TelemetryRecord(
        device_id=payload.device_id,
        fleet_id=payload.fleet_id,
        device_timestamp=device_ts,
        ingested_at=datetime.now(timezone.utc),
        temperature_c=payload.temperature_c,
        battery_percent=payload.battery_percent,
        speed_kmh=payload.speed_kmh,
        engine_rpm=payload.engine_rpm,
        fuel_level_percent=payload.fuel_level_percent,
        odometer_km=payload.odometer_km,
        custom_metrics=payload.custom_metrics,
    )

    if payload.gps:
        record.latitude = payload.gps.latitude
        record.longitude = payload.gps.longitude
        record.altitude_m = payload.gps.altitude_m
        record.heading = payload.gps.heading

    return record
