/**
 * Geofence Checker — determines if a point is inside/outside defined zones.
 *
 * BUG: Boundary comparison uses strict equality (===) and direct floating-point
 * comparison without epsilon tolerance. GPS coordinates have inherent imprecision
 * (~1-3 meter jitter), so a vehicle sitting exactly at a zone boundary will
 * "flicker" between inside and outside on successive readings.
 *
 * This causes:
 *   - Rapid zone_enter/zone_exit event storms for vehicles parked near boundaries
 *   - Alert-engine receives hundreds of spurious boundary-crossing events
 *   - Time-in-zone calculations are wildly inaccurate (shows 0 seconds when
 *     the device was actually stationary at the boundary for hours)
 */

const { v4: uuidv4 } = require('uuid');
const logger = require('./logger');

// Pre-configured geofence zones
const zones = [
  {
    id: 'zone-warehouse-a',
    name: 'Warehouse A',
    type: 'circle',
    center: { latitude: 37.7749, longitude: -122.4194 },
    radius_m: 500,
  },
  {
    id: 'zone-depot-north',
    name: 'North Depot',
    type: 'circle',
    center: { latitude: 37.8044, longitude: -122.2712 },
    radius_m: 300,
  },
  {
    id: 'zone-restricted',
    name: 'Restricted Area',
    type: 'circle',
    center: { latitude: 37.7849, longitude: -122.4094 },
    radius_m: 100,
  },
];

// Track last known zone state per device (for enter/exit detection)
const deviceZoneState = new Map();

/**
 * Check a device position against all geofence zones.
 *
 * BUG: The distance calculation compares against the radius using >= and <=
 * without any epsilon buffer. A device at distance 499.9999999m from a 500m
 * zone center will alternate between "inside" and "outside" as GPS jitter
 * adds/subtracts fractions of a meter.
 *
 * Fix: Add an epsilon buffer (e.g., 5 meters) so that transitions only
 * trigger when the device moves clearly across the boundary:
 *   - Enter: distance < (radius - epsilon)
 *   - Exit: distance > (radius + epsilon)
 * This creates a "dead zone" at the boundary that prevents flicker.
 */
function checkGeofence(deviceId, latitude, longitude) {
  const results = [];
  const previousState = deviceZoneState.get(deviceId) || {};
  const currentState = {};

  for (const zone of zones) {
    const distance = calculateDistance(
      latitude, longitude,
      zone.center.latitude, zone.center.longitude
    );

    // BUG: No epsilon tolerance — direct comparison causes flicker at boundary
    const isInside = distance <= zone.radius_m;
    currentState[zone.id] = isInside;

    const wasInside = previousState[zone.id] || false;
    let event = null;

    if (isInside && !wasInside) {
      event = 'zone_enter';
    } else if (!isInside && wasInside) {
      event = 'zone_exit';
    }

    results.push({
      zone_id: zone.id,
      zone_name: zone.name,
      is_inside: isInside,
      distance_m: Math.round(distance * 100) / 100,
      event,
    });

    if (event) {
      logger.info('Zone transition', {
        device_id: deviceId,
        zone: zone.name,
        event,
        distance_m: distance,
      });
    }
  }

  deviceZoneState.set(deviceId, currentState);
  return results;
}

/**
 * Calculate distance between two GPS coordinates using Haversine formula.
 * Returns distance in meters.
 */
function calculateDistance(lat1, lon1, lat2, lon2) {
  const R = 6371000; // Earth's radius in meters
  const dLat = toRadians(lat2 - lat1);
  const dLon = toRadians(lon2 - lon1);

  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRadians(lat1)) * Math.cos(toRadians(lat2)) *
    Math.sin(dLon / 2) * Math.sin(dLon / 2);

  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

function toRadians(degrees) {
  return degrees * (Math.PI / 180);
}

function getZones() {
  return zones;
}

function addZone(zoneConfig) {
  const zone = { id: `zone-${uuidv4().slice(0, 8)}`, ...zoneConfig };
  zones.push(zone);
  return zone;
}

module.exports = { checkGeofence, calculateDistance, getZones, addZone };
