"""Health check endpoint for ALB target group."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "healthy", "service": "telemetry-ingest"}
