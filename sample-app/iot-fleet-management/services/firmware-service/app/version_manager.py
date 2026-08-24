"""
Firmware version management — determines update eligibility.

BUG: Version comparison uses Python string comparison instead of semantic
versioning. This causes "2.10.0" < "2.9.0" (because "1" < "9" in ASCII),
which means:
  - Devices on v2.10+ are incorrectly told they need an "update" to v2.9
  - Rollback validation fails (thinks v2.10 is older than v2.9)
  - Critical security patches on v2.10+ are not properly enforced
"""

import os
import structlog

logger = structlog.get_logger()

# Bucket name must be set via environment variable to avoid bucket squatting.
# In production, use a unique name like: iot-firmware-{account_id}-{region}
_FIRMWARE_BUCKET = os.environ.get("FIRMWARE_BUCKET_NAME", "iot-firmware-CHANGE-ME")

# Simulated firmware registry (in production, this would be DynamoDB)
FIRMWARE_REGISTRY = {
    "sensor-v1": {
        "version": "2.10.1",
        "release_date": "2024-03-01",
        "checksum_sha256": "abc123def456",
        "download_url": f"s3://{_FIRMWARE_BUCKET}/sensor-v1/2.10.1.bin",
        "release_notes": "Fixed battery drain in sleep mode",
        "is_critical": True,
    },
    "gateway-v2": {
        "version": "1.5.0",
        "release_date": "2024-02-15",
        "checksum_sha256": "789ghi012jkl",
        "download_url": f"s3://{_FIRMWARE_BUCKET}/gateway-v2/1.5.0.bin",
        "release_notes": "Added MQTT v5 support",
        "is_critical": False,
    },
    "tracker-v3": {
        "version": "3.2.0",
        "release_date": "2024-03-10",
        "checksum_sha256": "mno345pqr678",
        "download_url": f"s3://{_FIRMWARE_BUCKET}/tracker-v3/3.2.0.bin",
        "release_notes": "Improved GPS accuracy in urban canyons",
        "is_critical": False,
    },
}


def get_latest_version(device_type: str) -> dict | None:
    """Get the latest firmware version for a device type."""
    return FIRMWARE_REGISTRY.get(device_type)


def check_update_needed(device_type: str, current_version: str) -> dict:
    """
    Check if a device needs a firmware update.

    BUG: Uses string comparison for version check. "2.10.1" < "2.9.0" is True
    in string comparison because "1" (in "10") < "9" character-by-character.
    This incorrectly tells devices on v2.10+ that they need to "update" to v2.9.
    """
    latest = FIRMWARE_REGISTRY.get(device_type)
    if not latest:
        return {"needs_update": False, "reason": "unknown_device_type"}

    latest_version = latest["version"]
    comparison = compare_versions(current_version, latest_version)

    needs_update = comparison < 0

    return {
        "needs_update": needs_update,
        "current_version": current_version,
        "latest_version": latest_version,
        "is_critical": latest.get("is_critical", False) if needs_update else False,
    }


def compare_versions(version_a: str, version_b: str) -> int:
    """
    Compare two version strings.
    Returns: -1 if a < b, 0 if equal, 1 if a > b.

    BUG: Uses direct string comparison instead of parsing semantic version
    components as integers. This means:
      "2.10.0" < "2.9.0"  (WRONG — should be greater)
      "1.5.0" < "1.12.0"  (WRONG — should be less... wait, it says greater)

    The correct implementation should split on "." and compare each component
    as an integer: tuple(int(x) for x in version.split("."))
    """
    # BUG: String comparison — "2.10" < "2.9" because "1" < "9"
    if version_a < version_b:
        return -1
    elif version_a > version_b:
        return 1
    return 0
