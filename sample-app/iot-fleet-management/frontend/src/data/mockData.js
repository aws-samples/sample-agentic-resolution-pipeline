export const DEVICES = [
  { id: 'TRK-001', name: 'Truck Alpha', fleet: 'fleet-north', type: 'tracker-v3', firmware: '3.1.2', lat: 37.7749, lng: -122.4194, status: 'online', battery: 85, speed: 65, temp: 72 },
  { id: 'TRK-002', name: 'Truck Bravo', fleet: 'fleet-north', type: 'tracker-v3', firmware: '3.2.0', lat: 37.7849, lng: -122.4094, status: 'online', battery: 92, speed: 0, temp: 25 },
  { id: 'TRK-003', name: 'Truck Charlie', fleet: 'fleet-south', type: 'tracker-v3', firmware: '2.10.1', lat: 37.8044, lng: -122.2712, status: 'warning', battery: 12, speed: 45, temp: 88 },
  { id: 'SEN-001', name: 'Sensor Dock-A', fleet: 'fleet-warehouse', type: 'sensor-v1', firmware: '2.9.0', lat: 37.7650, lng: -122.4300, status: 'online', battery: 100, speed: 0, temp: 22 },
  { id: 'SEN-002', name: 'Sensor Dock-B', fleet: 'fleet-warehouse', type: 'sensor-v1', firmware: '2.10.1', lat: 37.7655, lng: -122.4290, status: 'offline', battery: 0, speed: 0, temp: null },
  { id: 'GW-001', name: 'Gateway North', fleet: 'fleet-north', type: 'gateway-v2', firmware: '1.5.0', lat: 37.7900, lng: -122.3900, status: 'online', battery: null, speed: null, temp: 30 },
  { id: 'TRK-004', name: 'Truck Delta', fleet: 'fleet-south', type: 'tracker-v3', firmware: '3.0.0', lat: 37.7500, lng: -122.4500, status: 'online', battery: 67, speed: 82, temp: 45 },
  { id: 'TRK-005', name: 'Truck Echo', fleet: 'fleet-north', type: 'tracker-v3', firmware: '2.10.1', lat: 37.7720, lng: -122.3980, status: 'online', battery: 54, speed: 110, temp: 91 },
];

export const ALERTS = [
  { id: 'ALT-001', device_id: 'TRK-003', metric: 'battery_percent', severity: 'MEDIUM', message: 'Battery level critically low (12%)', timestamp: '2024-03-15T14:32:00Z', acknowledged: false },
  { id: 'ALT-002', device_id: 'TRK-005', metric: 'temperature_c', severity: 'HIGH', message: 'Engine temperature average exceeds safe range (91°C)', timestamp: '2024-03-15T14:30:00Z', acknowledged: false },
  { id: 'ALT-003', device_id: 'TRK-005', metric: 'speed_kmh', severity: 'HIGH', message: 'Vehicle speed exceeds fleet safety limit (110 km/h)', timestamp: '2024-03-15T14:28:00Z', acknowledged: true },
  { id: 'ALT-004', device_id: 'SEN-002', metric: 'connectivity', severity: 'MEDIUM', message: 'Device offline for >2 minutes', timestamp: '2024-03-15T14:25:00Z', acknowledged: false },
  { id: 'ALT-005', device_id: 'TRK-003', metric: 'temperature_c', severity: 'HIGH', message: 'Engine temperature spike (88°C)', timestamp: '2024-03-15T14:20:00Z', acknowledged: true },
];

export const GEOFENCES = [
  { id: 'zone-warehouse-a', name: 'Warehouse A', center: [37.7749, -122.4194], radius: 500, color: '#3b82f6' },
  { id: 'zone-depot-north', name: 'North Depot', center: [37.8044, -122.2712], radius: 300, color: '#10b981' },
  { id: 'zone-restricted', name: 'Restricted Area', center: [37.7849, -122.4094], radius: 100, color: '#ef4444' },
];

export const FIRMWARE_VERSIONS = [
  { device_type: 'sensor-v1', version: '2.10.1', release_date: '2024-03-01', is_critical: true, devices_on_version: 1, devices_outdated: 1 },
  { device_type: 'gateway-v2', version: '1.5.0', release_date: '2024-02-15', is_critical: false, devices_on_version: 1, devices_outdated: 0 },
  { device_type: 'tracker-v3', version: '3.2.0', release_date: '2024-03-10', is_critical: false, devices_on_version: 1, devices_outdated: 4 },
];

export const TELEMETRY_HISTORY = Array.from({ length: 30 }, (_, i) => ({
  time: `14:${String(i).padStart(2, '0')}`,
  'TRK-001_temp': 68 + Math.random() * 10,
  'TRK-003_temp': 80 + Math.random() * 15,
  'TRK-005_temp': 85 + Math.random() * 10,
  'TRK-001_speed': 55 + Math.random() * 20,
  'TRK-004_speed': 75 + Math.random() * 15,
}));
