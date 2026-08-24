# IoT Fleet Management — Sample Application

A realistic multi-service IoT fleet management application deployed on AWS ECS Fargate. Used for end-to-end testing of the Agentic Resolution Pipeline.

## Architecture

```
                    ┌──────────────┐
                    │     ALB      │
                    └──────┬───────┘
           ┌───────────────┼───────────────┐───────────────┐
           │               │               │               │
    ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
    │  Telemetry  │ │   Alert     │ │  Firmware   │ │  Geofence   │
    │   Ingest    │ │   Engine    │ │   Service   │ │   Service   │
    │ (Python)    │ │ (Node.js)   │ │ (Python)    │ │ (Node.js)   │
    └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
           │               │               │               │
    ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
    │  DynamoDB   │ │   Redis     │ │  DynamoDB   │ │  DynamoDB   │
    │ (telemetry) │ │  (windows)  │ │ (firmware)  │ │  (zones)    │
    └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘
```

## Services

| Service | Language | Port | Purpose |
|---------|----------|------|---------|
| telemetry-ingest | Python/FastAPI | 8080 | Receives device payloads, validates, stores |
| alert-engine | Node.js/Express | 8081 | Threshold evaluation, anomaly detection |
| firmware-service | Python/FastAPI | 8082 | OTA update orchestration, version mgmt |
| geofence-service | Node.js/Express | 8083 | Boundary definitions, location checks |

## Planted Bugs (for pipeline testing)

Each bug maps to a realistic Jira ticket the pipeline will investigate and fix:

| # | Service | Bug | Impact |
|---|---------|-----|--------|
| 1 | telemetry-ingest | Timestamp not normalized to UTC | Out-of-order writes, wrong alert windows |
| 2 | alert-engine | Sliding window returns N-1 samples (off-by-one) | Missed/false alerts |
| 3 | firmware-service | Version comparison uses string sort (not semver) | Devices told to downgrade |
| 4 | geofence-service | No epsilon tolerance at boundaries | Flicker: rapid enter/exit events |
| 5 | alert-engine | Device reconnect doesn't clear stale state | Ghost alerts on recovered devices |

## Observability

- **X-Ray:** Distributed tracing across all services
- **CloudWatch Logs:** Structured JSON format (service name, trace ID, request context)
- **CloudWatch Metrics:** Custom metrics per service (TelemetryIngested, AlertTriggered, etc.)
- **CloudWatch Alarms:** P99 latency, error rate, alert storm detection

## Deployment

```bash
cd infrastructure
cdk deploy IoTFleetStack
```

## Local Development

```bash
# Telemetry Ingest
cd services/telemetry-ingest
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080

# Alert Engine
cd services/alert-engine
npm install
npm run dev

# Firmware Service
cd services/firmware-service
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8082

# Geofence Service
cd services/geofence-service
npm install
npm run dev
```

## Testing

```bash
# Python services
cd services/telemetry-ingest && pytest tests/
cd services/firmware-service && pytest tests/

# Node.js services
cd services/alert-engine && npm test
cd services/geofence-service && npm test
```
# KB ingestion verified

# ingestion test
# KB ingestion test 8
