/**
 * Alert Evaluator — checks metric values against configured thresholds.
 *
 * Supports:
 *   - Static threshold (value > X)
 *   - Sliding average threshold (mean of window > X)
 *   - Rate of change (delta between consecutive readings)
 */

const ALERT_RULES = {
  temperature_c: {
    type: 'sliding_average',
    threshold: 85,
    severity: 'HIGH',
    message: 'Engine temperature average exceeds safe operating range',
  },
  battery_percent: {
    type: 'static_threshold',
    threshold: 15,
    comparator: 'lt',
    severity: 'MEDIUM',
    message: 'Battery level critically low',
  },
  speed_kmh: {
    type: 'static_threshold',
    threshold: 120,
    comparator: 'gt',
    severity: 'HIGH',
    message: 'Vehicle speed exceeds fleet safety limit',
  },
  engine_rpm: {
    type: 'rate_of_change',
    max_delta: 3000,
    severity: 'MEDIUM',
    message: 'Sudden RPM spike detected — possible mechanical issue',
  },
  fuel_level_percent: {
    type: 'rate_of_change',
    max_delta: -20,
    severity: 'HIGH',
    message: 'Rapid fuel loss — possible leak or theft',
  },
};

/**
 * Evaluate a metric reading against its alert rule.
 */
function evaluateAlert(deviceId, metricName, currentValue, window) {
  const rule = ALERT_RULES[metricName];
  if (!rule) {
    return { triggered: false, reason: 'no_rule_configured' };
  }

  switch (rule.type) {
    case 'static_threshold':
      return _evaluateStatic(currentValue, rule);

    case 'sliding_average':
      return _evaluateSlidingAverage(currentValue, window, rule);

    case 'rate_of_change':
      return _evaluateRateOfChange(currentValue, window, rule);

    default:
      return { triggered: false, reason: 'unknown_rule_type' };
  }
}

function _evaluateStatic(value, rule) {
  const breached = rule.comparator === 'lt'
    ? value < rule.threshold
    : value > rule.threshold;

  return {
    triggered: breached,
    type: 'static_threshold',
    severity: rule.severity,
    threshold: rule.threshold,
    message: breached ? rule.message : null,
  };
}

function _evaluateSlidingAverage(currentValue, window, rule) {
  if (window.length === 0) {
    return { triggered: false, reason: 'insufficient_data' };
  }

  const values = window.map(s => s.value);
  const avg = values.reduce((sum, v) => sum + v, 0) / values.length;
  const breached = avg > rule.threshold;

  return {
    triggered: breached,
    type: 'sliding_average',
    severity: rule.severity,
    threshold: rule.threshold,
    average: avg,
    window_count: values.length,
    message: breached ? rule.message : null,
  };
}

function _evaluateRateOfChange(currentValue, window, rule) {
  if (window.length === 0) {
    return { triggered: false, reason: 'insufficient_data' };
  }

  const lastSample = window[window.length - 1];
  const delta = currentValue - lastSample.value;
  const breached = rule.max_delta < 0
    ? delta < rule.max_delta
    : delta > rule.max_delta;

  return {
    triggered: breached,
    type: 'rate_of_change',
    severity: rule.severity,
    delta,
    max_delta: rule.max_delta,
    message: breached ? rule.message : null,
  };
}

module.exports = { evaluateAlert, ALERT_RULES };
