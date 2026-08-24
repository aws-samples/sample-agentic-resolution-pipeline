"""
Firmware Service — OTA update orchestration and version management.

Manages firmware versions for the fleet, determines which devices need
updates, and orchestrates rollout in controlled batches.
"""

from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
import structlog
import boto3
import os

from app.models import FirmwareVersion, UpdateRequest, UpdateStatus
from app.version_manager import check_update_needed, get_latest_version, compare_versions
from app.health import router as health_router

logger = structlog.get_logger()

TABLE_NAME = os.getenv("FIRMWARE_TABLE", "iot-fleet-firmware")
REGION = os.getenv("AWS_REGION", "us-east-1")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.dynamodb = boto3.resource("dynamodb", region_name=REGION)
    app.state.table = app.state.dynamodb.Table(TABLE_NAME)
    logger.info("firmware_service_started", table=TABLE_NAME)
    yield


app = FastAPI(
    title="Firmware Service",
    description="OTA firmware update orchestration for IoT fleet",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health_router)


@app.get("/firmware/latest/{device_type}")
async def get_latest(device_type: str):
    """Get the latest firmware version for a device type."""
    version = get_latest_version(device_type)
    if not version:
        raise HTTPException(status_code=404, detail=f"No firmware found for {device_type}")
    return version


@app.post("/firmware/check-update")
async def check_update(request: UpdateRequest):
    """Check if a device needs a firmware update."""
    result = check_update_needed(
        device_type=request.device_type,
        current_version=request.current_version,
    )
    logger.info("update_check",
                device_id=request.device_id,
                current=request.current_version,
                needs_update=result["needs_update"])
    return result


@app.post("/firmware/rollback/{device_type}")
async def rollback(device_type: str, target_version: str):
    """Rollback a device type to a previous firmware version."""
    latest = get_latest_version(device_type)
    if not latest:
        raise HTTPException(status_code=404, detail=f"No firmware found for {device_type}")

    comparison = compare_versions(target_version, latest["version"])
    if comparison >= 0:
        raise HTTPException(status_code=400, detail="Rollback target must be older than current")

    logger.info("firmware_rollback", device_type=device_type, target=target_version)
    return {"status": "rollback_initiated", "target_version": target_version}
