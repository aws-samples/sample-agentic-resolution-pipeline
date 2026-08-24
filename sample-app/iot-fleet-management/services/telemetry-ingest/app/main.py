"""
Telemetry Ingest Service — receives device payloads, validates, stores in DynamoDB.

Devices send periodic telemetry (temperature, speed, battery, GPS coordinates).
This service normalizes and persists the data for downstream consumption by
the alert-engine and geofence-service.
"""

from fastapi import FastAPI, HTTPException, Request
from contextlib import asynccontextmanager
import structlog
import boto3
import os

from app.models import TelemetryPayload, TelemetryRecord
from app.ingestion import process_telemetry
from app.health import router as health_router

logger = structlog.get_logger()

TABLE_NAME = os.getenv("TELEMETRY_TABLE", "iot-fleet-telemetry")
REGION = os.getenv("AWS_REGION", "us-east-1")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.dynamodb = boto3.resource("dynamodb", region_name=REGION)
    app.state.table = app.state.dynamodb.Table(TABLE_NAME)
    app.state.cloudwatch = boto3.client("cloudwatch", region_name=REGION)
    logger.info("telemetry_ingest_started", table=TABLE_NAME)
    yield
    logger.info("telemetry_ingest_shutdown")


app = FastAPI(
    title="Telemetry Ingest Service",
    description="Receives and stores IoT device telemetry data",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health_router)


@app.post("/telemetry", status_code=201)
async def ingest_telemetry(payload: TelemetryPayload, request: Request):
    """Ingest a telemetry payload from a device."""
    try:
        record = process_telemetry(payload)
        request.app.state.table.put_item(Item=record.to_dynamodb())

        request.app.state.cloudwatch.put_metric_data(
            Namespace="IoTFleet/Telemetry",
            MetricData=[{
                "MetricName": "TelemetryIngested",
                "Value": 1,
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "DeviceId", "Value": payload.device_id},
                    {"Name": "FleetId", "Value": payload.fleet_id},
                ],
            }],
        )

        logger.info("telemetry_ingested",
                    device_id=payload.device_id,
                    fleet_id=payload.fleet_id,
                    timestamp=str(record.ingested_at))
        return {"status": "accepted", "record_id": record.record_id}

    except ValueError as e:
        logger.warning("telemetry_validation_failed",
                       device_id=payload.device_id, error=str(e))
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("telemetry_ingestion_failed",
                     device_id=payload.device_id, error=str(e))
        raise HTTPException(status_code=500, detail="Internal error")


@app.post("/telemetry/batch", status_code=201)
async def ingest_batch(payloads: list[TelemetryPayload], request: Request):
    """Ingest a batch of telemetry payloads."""
    results = []
    errors = []

    for payload in payloads:
        try:
            record = process_telemetry(payload)
            request.app.state.table.put_item(Item=record.to_dynamodb())
            results.append({"device_id": payload.device_id, "status": "accepted"})
        except Exception as e:
            errors.append({"device_id": payload.device_id, "error": str(e)})

    logger.info("batch_ingested", accepted=len(results), failed=len(errors))
    return {"accepted": len(results), "failed": len(errors), "errors": errors}
