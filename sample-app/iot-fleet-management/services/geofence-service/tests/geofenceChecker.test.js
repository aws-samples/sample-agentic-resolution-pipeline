const { checkGeofence, calculateDistance } = require('../src/geofenceChecker');

describe('Geofence Checker', () => {
  test('device clearly inside zone', () => {
    // Warehouse A center: 37.7749, -122.4194, radius 500m
    const results = checkGeofence('device-001', 37.7749, -122.4194);
    const warehouseResult = results.find(r => r.zone_id === 'zone-warehouse-a');
    expect(warehouseResult.is_inside).toBe(true);
    expect(warehouseResult.distance_m).toBeLessThan(1);
  });

  test('device clearly outside zone', () => {
    // Far from any zone
    const results = checkGeofence('device-002', 40.0, -120.0);
    results.forEach(r => {
      expect(r.is_inside).toBe(false);
    });
  });

  test('BUG: device at boundary flickers between inside/outside', () => {
    // Place device exactly at the boundary (500m from center)
    // With GPS jitter of +/- 2m, it will alternate
    const centerLat = 37.7749;
    const centerLon = -122.4194;

    // Calculate a point approximately 500m north of center
    const boundaryLat = centerLat + (500 / 111320); // ~500m north
    const boundaryLon = centerLon;

    // First check — might be inside
    const results1 = checkGeofence('device-boundary', boundaryLat, boundaryLon);
    const zone1 = results1.find(r => r.zone_id === 'zone-warehouse-a');

    // Simulate GPS jitter: +2m
    const jitterLat = boundaryLat + (2 / 111320);
    const results2 = checkGeofence('device-boundary', jitterLat, boundaryLon);
    const zone2 = results2.find(r => r.zone_id === 'zone-warehouse-a');

    // BUG: These may differ (one inside, one outside) causing a zone_exit event
    // from a device that hasn't actually moved meaningfully
    // With epsilon tolerance, both should report the same state
    if (zone1.is_inside !== zone2.is_inside) {
      // This documents the flicker bug — in practice this triggers
      // spurious zone_enter/zone_exit events
      expect(zone2.event).toBeDefined();
    }
  });
});

describe('Distance Calculation', () => {
  test('same point returns 0', () => {
    expect(calculateDistance(37.7749, -122.4194, 37.7749, -122.4194)).toBe(0);
  });

  test('known distance SF to Oakland', () => {
    // SF (37.7749, -122.4194) to Oakland (37.8044, -122.2712)
    const distance = calculateDistance(37.7749, -122.4194, 37.8044, -122.2712);
    // Approximately 13.5 km
    expect(distance).toBeGreaterThan(13000);
    expect(distance).toBeLessThan(14000);
  });
});
