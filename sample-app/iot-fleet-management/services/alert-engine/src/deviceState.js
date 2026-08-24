/**
 * Device State Manager — tracks device connection state for alert suppression.
 *
 * BUG: When a device reconnects after being offline, old metric state is NOT
 * cleared from the sliding window. This means:
 *   - Stale readings from before the disconnect persist in the window
 *   - Alert evaluation runs against a mix of old (pre-disconnect) and new
 *     (post-reconnect) data points
 *   - Ghost alerts fire because the old high values combine with new normal
 *     values to push the average over threshold
 *
 * Example: Device was overheating before disconnect (readings 90, 92, 95°C).
 * Device gets fixed and reconnects with normal temp (25°C). The window still
 * has [90, 92, 95, 25] → average 75.5°C → still triggers the 85°C alert
 * even though the device is now fine.
 *
 * Fix: On reconnect (detected via heartbeat gap > OFFLINE_THRESHOLD_S),
 * clear all sliding windows for that device before processing new telemetry.
 */

const logger = require('./logger');

const OFFLINE_THRESHOLD_S = parseInt(process.env.OFFLINE_THRESHOLD_S || '120', 10);

// Tracks last heartbeat per device
const lastHeartbeat = new Map();

/**
 * Record a device heartbeat and detect reconnection.
 *
 * BUG: Does NOT clear sliding windows on reconnect. Old stale data
 * persists and contaminates new readings, causing ghost alerts.
 *
 * The fix should call clearDeviceWindow() for all metrics when
 * a reconnect is detected.
 */
function recordHeartbeat(deviceId) {
  const now = Date.now();
  const lastSeen = lastHeartbeat.get(deviceId);
  const isReconnect = lastSeen && (now - lastSeen) > OFFLINE_THRESHOLD_S * 1000;

  if (isReconnect) {
    logger.info('Device reconnected', {
      device_id: deviceId,
      offline_duration_s: Math.round((now - lastSeen) / 1000),
    });
    // BUG: Should clear sliding windows here but doesn't
    // Fix: const { clearDeviceWindow } = require('./slidingWindow');
    //      for (const metric of ['temperature_c', 'battery_percent', 'speed_kmh', 'engine_rpm', 'fuel_level_percent']) {
    //        clearDeviceWindow(deviceId, metric);
    //      }
  }

  lastHeartbeat.set(deviceId, now);

  return {
    deviceId,
    isReconnect,
    offlineDurationS: isReconnect ? Math.round((now - lastSeen) / 1000) : 0,
  };
}

/**
 * Check if a device is currently considered online.
 */
function isDeviceOnline(deviceId) {
  const lastSeen = lastHeartbeat.get(deviceId);
  if (!lastSeen) return false;
  return (Date.now() - lastSeen) < OFFLINE_THRESHOLD_S * 1000;
}

/**
 * Get all known devices and their connection status.
 */
function getDeviceStates() {
  const states = {};
  for (const [deviceId, lastSeen] of lastHeartbeat.entries()) {
    states[deviceId] = {
      lastSeen: new Date(lastSeen).toISOString(),
      online: (Date.now() - lastSeen) < OFFLINE_THRESHOLD_S * 1000,
    };
  }
  return states;
}

module.exports = { recordHeartbeat, isDeviceOnline, getDeviceStates };
