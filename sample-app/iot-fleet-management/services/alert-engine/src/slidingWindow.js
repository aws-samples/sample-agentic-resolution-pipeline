/**
 * Sliding Window — maintains per-device metric windows in Redis.
 *
 * Each device+metric combination is tracked in a bounded buffer of
 * ALERT_WINDOW_SIZE samples. The oldest sample is evicted once the
 * buffer is full, giving a true sliding window of exactly N samples.
 */

const WINDOW_SIZE = parseInt(process.env.ALERT_WINDOW_SIZE || '10', 10);
const WINDOW_TTL_SECONDS = parseInt(process.env.ALERT_WINDOW_TTL || '300', 10);

const logger = require('./logger');

// In-memory store (replaced by Redis in production via IoRedis)
const windows = new Map();

/**
 * Get the current sliding window for a device+metric combination.
 *
 * Returns a shallow copy of the internal buffer (up to WINDOW_SIZE elements).
 * Invariant: the returned array length must equal min(samples_added, WINDOW_SIZE).
 *
 * FIX (IOT-6): was `window.slice(1)` which silently dropped the oldest sample,
 * yielding N-1 items on a full buffer. Corrected to `window.slice(0)` so the
 * full buffer is returned and threshold averages are computed over all N samples.
 */
async function getDeviceWindow(deviceId, metricName) {
  const key = `${deviceId}:${metricName}`;

  if (!windows.has(key)) {
    windows.set(key, []);
  }

  const buffer = windows.get(key);

  // Trim to window size — evict the oldest sample when over capacity
  while (buffer.length > WINDOW_SIZE) {
    buffer.shift();
  }

  // Return a copy of the full buffer.
  // slice(0) preserves all elements; slice(1) would incorrectly drop index 0.
  const result = buffer.slice(0);

  // Observability: warn if the returned window is unexpectedly short on a
  // fully-warmed buffer — this would have caught the off-by-one immediately.
  if (buffer.length === WINDOW_SIZE && result.length !== WINDOW_SIZE) {
    logger.warn('Window size mismatch', {
      deviceId,
      metricName,
      expected: WINDOW_SIZE,
      actual: result.length,
    });
  }

  return result;
}

/**
 * Clear the window for a device (used on reconnect or manual reset).
 */
async function clearDeviceWindow(deviceId, metricName) {
  const key = `${deviceId}:${metricName}`;
  windows.delete(key);
}

/**
 * Get window configuration.
 */
function getWindowConfig() {
  return { windowSize: WINDOW_SIZE, ttlSeconds: WINDOW_TTL_SECONDS };
}

module.exports = { getDeviceWindow, clearDeviceWindow, getWindowConfig, WINDOW_SIZE };
