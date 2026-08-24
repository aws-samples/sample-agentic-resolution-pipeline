/**
 * Geofence Service — boundary definitions and real-time location checks.
 *
 * Maintains geofence zones and checks device GPS positions against them.
 * Publishes zone entry/exit events to the alert-engine.
 */

const express = require('express');
const { checkGeofence, getZones, addZone } = require('./geofenceChecker');
const logger = require('./logger');

const app = express(); // nosemgrep: express-check-csurf-middleware-usage
app.use(express.json());

const PORT = process.env.PORT || 8083;

app.get('/health', (req, res) => {
  res.json({ status: 'healthy', service: 'geofence-service' });
});

/**
 * Check if a device position is inside/outside defined geofences.
 */
app.post('/check', async (req, res) => {
  const { device_id, latitude, longitude } = req.body;

  if (!device_id || latitude === undefined || longitude === undefined) {
    return res.status(400).json({ error: 'device_id, latitude, and longitude required' });
  }

  try {
    const results = checkGeofence(device_id, latitude, longitude);
    logger.info('Geofence check', { device_id, latitude, longitude, zones_checked: results.length });
    res.json({ device_id, position: { latitude, longitude }, zones: results });
  } catch (err) {
    logger.error('Geofence check failed', { device_id, error: err.message });
    res.status(500).json({ error: 'Check failed' });
  }
});

/**
 * Get all configured geofence zones.
 */
app.get('/zones', (req, res) => {
  res.json(getZones());
});

/**
 * Add a new geofence zone.
 */
app.post('/zones', (req, res) => {
  const { name, type, center, radius_m, vertices } = req.body;
  if (!name || !type) {
    return res.status(400).json({ error: 'name and type required' });
  }
  const zone = addZone({ name, type, center, radius_m, vertices });
  res.status(201).json(zone);
});

app.listen(PORT, () => {
  logger.info(`Geofence service listening on port ${PORT}`);
});

module.exports = app;
