/**
 * Alert Engine — evaluates telemetry against thresholds and fires alarms.
 *
 * Consumes telemetry records from DynamoDB Streams (or direct POST for testing),
 * evaluates sliding window rules, and publishes alerts to SNS.
 */

const express = require('express');
const { evaluateAlert } = require('./alertEvaluator');
const { getDeviceWindow } = require('./slidingWindow');
const { publishAlert } = require('./publisher');
const logger = require('./logger');

const app = express(); // nosemgrep: express-check-csurf-middleware-usage
app.use(express.json());

const PORT = process.env.PORT || 8081;

app.get('/health', (req, res) => {
  res.json({ status: 'healthy', service: 'alert-engine' });
});

/**
 * Evaluate a new telemetry reading against alert rules.
 * Called by DynamoDB Stream processor or directly for testing.
 */
app.post('/evaluate', async (req, res) => {
  const { device_id, fleet_id, metric_name, value, timestamp } = req.body;

  if (!device_id || !metric_name || value === undefined) {
    return res.status(400).json({ error: 'device_id, metric_name, and value are required' });
  }

  try {
    const window = await getDeviceWindow(device_id, metric_name);
    window.push({ value, timestamp: timestamp || new Date().toISOString() });

    const alertResult = evaluateAlert(device_id, metric_name, value, window);

    if (alertResult.triggered) {
      await publishAlert({
        device_id,
        fleet_id,
        metric_name,
        alert_type: alertResult.type,
        severity: alertResult.severity,
        message: alertResult.message,
        current_value: value,
        threshold: alertResult.threshold,
        window_size: window.length,
      });
      logger.info('Alert triggered', { device_id, metric_name, type: alertResult.type });
    }

    res.json({
      evaluated: true,
      alert_triggered: alertResult.triggered,
      window_size: window.length,
      details: alertResult,
    });
  } catch (err) {
    logger.error('Evaluation failed', { device_id, metric_name, error: err.message });
    res.status(500).json({ error: 'Evaluation failed' });
  }
});

/**
 * Get current alert rules configuration.
 */
app.get('/rules', (req, res) => {
  const { ALERT_RULES } = require('./alertEvaluator');
  res.json(ALERT_RULES);
});

app.listen(PORT, () => {
  logger.info(`Alert engine listening on port ${PORT}`);
});

module.exports = app;
