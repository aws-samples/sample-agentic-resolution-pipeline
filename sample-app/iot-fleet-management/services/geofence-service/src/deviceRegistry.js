/**
 * Device Registry Client — tracks device lifecycle events from alert-engine.
 *
 * BUG (related to Bug #5 - Reconnect Stale State):
 * When alert-engine detects a device reconnect, it should notify geofence-service
 * so that zone state (deviceZoneState in geofenceChecker.js) is cleared.
 * Without this, a device that was "inside" a zone before disconnect will:
 *   - Not trigger a zone_enter event when it reconnects inside the same zone
 *   - Trigger a spurious zone_exit if it reconnects in a different location
 *   - Show incorrect "time_in_zone" metrics (includes offline time)
 *
 * Fix requires changes in BOTH services:
 *   1. alert-engine/src/deviceState.js: On reconnect, POST to geofence-service /device/reset
 *   2. This file + geofenceChecker.js: Handle the reset endpoint, clear deviceZoneState
 */

const logger = require('./logger');

// Cache of device connection states (populated by event from alert-engine)
const deviceLastSeen = new Map();

/**
 * Handle device reconnect notification from alert-engine.
 * Should clear zone state but currently does nothing with it.
 *
 * BUG: This function is defined but never called because alert-engine
 * doesn't send the reconnect notification. Even if it did, it doesn't
 * actually clear the geofence zone state (deviceZoneState is in geofenceChecker.js).
 */
function handleDeviceReconnect(deviceId, offlineDurationS) {
  logger.info('Device reconnect received', { device_id: deviceId, offline_s: offlineDurationS });
  deviceLastSeen.set(deviceId, Date.now());

  // BUG: Should clear zone state here:
  // const { clearDeviceZoneState } = require('./geofenceChecker');
  // clearDeviceZoneState(deviceId);
  //
  // But geofenceChecker.js doesn't even export clearDeviceZoneState —
  // that function needs to be added too.
}

/**
 * Get device connection info for zone calculations.
 */
function getDeviceInfo(deviceId) {
  return {
    deviceId,
    lastSeen: deviceLastSeen.get(deviceId),
    isKnown: deviceLastSeen.has(deviceId),
  };
}

module.exports = { handleDeviceReconnect, getDeviceInfo };
