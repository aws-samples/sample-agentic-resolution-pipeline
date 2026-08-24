"""
Firmware version check — validates device firmware on telemetry ingestion.

Calls the firmware-service to determine if a device needs an OTA update.
If the device is on an outdated firmware with a critical security patch,
the telemetry record is flagged and an update notification is queued.

BUG (related to Bug #3 - Semver Comparison):
This module calls firmware-service's /firmware/check-update endpoint.
Firmware-service uses string comparison for versions, so a device on
v2.10.1 is incorrectly told it needs an "update" to v2.9.0.

When telemetry-ingest receives this incorrect response, it:
  - Flags the telemetry record with firmware_outdated=True (incorrect)
  - Emits a CloudWatch metric "FirmwareOutdated" (inflates the count)
  - Queues an unnecessary OTA update notification to the device

Fix requires:
  1. firmware-service/app/version_manager.py: Fix compare_versions to use semantic versioning
  2. This file: No code change needed, but existing incorrect flags in DynamoDB
     need a backfill script to clear firmware_outdated on records that were
     incorrectly flagged (or accept the data will self-correct going forward)
"""

import structlog
import httpx
import os

logger = structlog.get_logger()

FIRMWARE_SERVICE_URL = os.getenv("FIRMWARE_SERVICE_URL", "http://firmware-service:8082")


async def check_device_firmware(device_id: str, device_type: str, current_version: str) -> dict:
    """
    Check if a device's firmware is up to date.
    Calls firmware-service synchronously during ingestion.
    """
    if not current_version or not device_type:
        return {"checked": False, "reason": "missing_version_or_type"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{FIRMWARE_SERVICE_URL}/firmware/check-update",
                json={
                    "device_id": device_id,
                    "device_type": device_type,
                    "current_version": current_version,
                },
            )
            resp.raise_for_status()
            result = resp.json()

            if result.get("needs_update") and result.get("is_critical"):
                logger.warning(
                    "critical_firmware_update_needed",
                    device_id=device_id,
                    current=current_version,
                    latest=result.get("latest_version"),
                )

            return {
                "checked": True,
                "needs_update": result.get("needs_update", False),
                "is_critical": result.get("is_critical", False),
                "latest_version": result.get("latest_version"),
            }

    except Exception as e:
        logger.debug("firmware_check_failed", device_id=device_id, error=str(e))
        return {"checked": False, "reason": str(e)}
