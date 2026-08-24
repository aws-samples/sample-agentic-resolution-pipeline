"""Data models for firmware management."""

from pydantic import BaseModel, Field
from typing import Optional


class FirmwareVersion(BaseModel):
    device_type: str
    version: str
    release_date: str
    checksum_sha256: str
    download_url: str
    release_notes: Optional[str] = None
    min_battery_percent: float = Field(default=30.0)
    is_critical: bool = False


class UpdateRequest(BaseModel):
    device_id: str
    device_type: str
    current_version: str


class UpdateStatus(BaseModel):
    device_id: str
    needs_update: bool
    current_version: str
    latest_version: Optional[str] = None
    is_critical: bool = False
