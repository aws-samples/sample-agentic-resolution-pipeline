/**
 * Telemetry Consumer — fetches recent telemetry from the ingest service
 * for alert evaluation context.
 *
 * BUG (related to Bug #1 - Timestamp Drift):
 * This module queries telemetry records by timestamp range to build the
 * alert evaluation window. It constructs the query range assuming all
 * device_timestamp values are in UTC. But because telemetry-ingest stores
 * them as-is (with timezone offsets), records from devices in UTC+5:30
 * are invisible to queries looking for "last 5 minutes UTC".
 *
 * Impact: Devices in non-UTC timezones never trigger sliding-window alerts
 * because their records don't appear in the time-range query.
 *
 * Fix requires BOTH:
 *   1. telemetry-ingest/app/ingestion.py: normalize timestamps to UTC before storing
 *   2. This file: update query logic to handle the transition period where
 *      old records have offsets and new records are UTC (or re-index old data)
 */

const AWS = require('aws-sdk');
const logger = require('./logger');

const TELEMETRY_TABLE = process.env.TELEMETRY_TABLE || 'iot-fleet-telemetry';
const REGION = process.env.AWS_REGION || 'us-east-1';

const dynamodb = new AWS.DynamoDB.DocumentClient({ region: REGION });

/**
 * Fetch recent telemetry for a device within a time window.
 *
 * BUG: Assumes all SK (sort key) values are UTC ISO strings.
 * Records stored with timezone offsets (e.g., "2024-03-15T14:30:00+05:30")
 * sort lexicographically AFTER "2024-03-15T10:00:00Z" even though they
 * represent an earlier point in time (09:00 UTC).
 */
async function getRecentTelemetry(deviceId, windowMinutes = 5) {
  const now = new Date();
  const windowStart = new Date(now.getTime() - windowMinutes * 60 * 1000);

  const params = {
    TableName: TELEMETRY_TABLE,
    KeyConditionExpression: 'PK = :pk AND SK BETWEEN :start AND :end',
    ExpressionAttributeValues: {
      ':pk': `DEVICE#${deviceId}`,
      ':start': `TS#${windowStart.toISOString()}`,
      ':end': `TS#${now.toISOString()}`,
    },
    ScanIndexForward: true,
  };

  try {
    const result = await dynamodb.query(params).promise();
    logger.debug('Telemetry query', {
      device_id: deviceId,
      window_minutes: windowMinutes,
      records_found: result.Items.length,
    });
    return result.Items;
  } catch (err) {
    logger.error('Telemetry query failed', { device_id: deviceId, error: err.message });
    return [];
  }
}

/**
 * Enrich alert context with recent telemetry history.
 * Used by the alert evaluator to provide historical context in alert messages.
 */
async function getAlertContext(deviceId, metricName, windowMinutes = 10) {
  const records = await getRecentTelemetry(deviceId, windowMinutes);
  const values = records
    .filter(r => r[metricName] !== undefined)
    .map(r => ({
      value: r[metricName],
      timestamp: r.device_timestamp,
    }));

  return {
    sample_count: values.length,
    values: values.slice(-20),
    min: values.length > 0 ? Math.min(...values.map(v => v.value)) : null,
    max: values.length > 0 ? Math.max(...values.map(v => v.value)) : null,
  };
}

module.exports = { getRecentTelemetry, getAlertContext };
