/**
 * Geofence service configuration.
 *
 * BUG (related to Bug #4 - Boundary Precision):
 * BOUNDARY_EPSILON_M is set to 0 (disabled). This means the geofence
 * checker uses exact distance comparison with no tolerance buffer.
 *
 * The fix requires:
 *   1. Set BOUNDARY_EPSILON_M to a reasonable value (e.g., 5 meters)
 *   2. Update geofenceChecker.js to use the epsilon in boundary checks:
 *      - Enter: distance < (radius - epsilon)
 *      - Exit: distance > (radius + epsilon)
 *   3. Update tests to verify the dead zone behavior
 */

const config = {
  port: parseInt(process.env.PORT || '8083', 10),
  region: process.env.AWS_REGION || 'us-east-1',
  zonesTable: process.env.ZONES_TABLE || 'iot-fleet-geofences',

  // BUG: Epsilon is 0 — no tolerance buffer at boundaries
  // Fix: set to 5.0 (meters) to prevent GPS jitter from causing flicker
  boundaryEpsilonM: parseFloat(process.env.BOUNDARY_EPSILON_M || '0'),

  // How often to check for zone config updates (ms)
  zoneRefreshIntervalMs: parseInt(process.env.ZONE_REFRESH_MS || '60000', 10),

  // Alert-engine integration for zone events
  alertEngineUrl: process.env.ALERT_ENGINE_URL || 'https://alert-engine:8081',

  // Maximum zone transitions per device per minute before suppressing
  maxTransitionsPerMinute: parseInt(process.env.MAX_TRANSITIONS_PER_MINUTE || '10', 10),
};

module.exports = config;
