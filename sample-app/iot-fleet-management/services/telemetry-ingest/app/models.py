"""Data models for telemetry payloads and records."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field
import uuid


class GPSCoordinates(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: Optional[float] = None
    heading: Optional[float] = Field(default=None, ge=0, le=360)
    speed_kmh: Optional[float] = Field(default=None, ge=0)


class TelemetryPayload(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    fleet_id: str = Field(min_length=1, max_length=64)
    timestamp: str = Field(description="ISO 8601 timestamp from device")
    temperature_c: Optional[float] = None
    battery_percent: Optional[float] = Field(default=None, ge=0, le=100)
    speed_kmh: Optional[float] = Field(default=None, ge=0)
    gps: Optional[GPSCoordinates] = None
    engine_rpm: Optional[int] = Field(default=None, ge=0)
    fuel_level_percent: Optional[float] = Field(default=None, ge=0, le=100)
    odometer_km: Optional[float] = Field(default=None, ge=0)
    custom_metrics: Optional[dict] = None


class TelemetryRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str
    fleet_id: str
    device_timestamp: str
    ingested_at: datetime
    temperature_c: Optional[float] = None
    battery_percent: Optional[float] = None
    speed_kmh: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude_m: Optional[float] = None
    heading: Optional[float] = None
    engine_rpm: Optional[int] = None
    fuel_level_percent: Optional[float] = None
    odometer_km: Optional[float] = None
    custom_metrics: Optional[dict] = None

    def to_dynamodb(self) -> dict:
        """Convert to DynamoDB-compatible item (Decimals for numbers)."""
        item = {
            "PK": f"DEVICE#{self.device_id}",
            "SK": f"TS#{self.ingested_at.isoformat()}",
            "record_id": self.record_id,
            "device_id": self.device_id,
            "fleet_id": self.fleet_id,
            "device_timestamp": self.device_timestamp,
            "ingested_at": self.ingested_at.isoformat(),
        }

        for field in ["temperature_c", "battery_percent", "speed_kmh",
                      "latitude", "longitude", "altitude_m", "heading",
                      "engine_rpm", "fuel_level_percent", "odometer_km"]:
            value = getattr(self, field)
            if value is not None:
                item[field] = Decimal(str(value))

        if self.custom_metrics:
            item["custom_metrics"] = self.custom_metrics

        return item
