import { MapContainer, TileLayer, Marker, Popup, Circle } from 'react-leaflet';
import { DEVICES, GEOFENCES } from '../data/mockData';
import L from 'leaflet';

const statusColors = { online: '#10b981', warning: '#f59e0b', offline: '#ef4444' };

function createIcon(status) {
  const color = statusColors[status] || '#6b7280';
  const el = document.createElement('div');
  el.style.cssText = `width:14px;height:14px;border-radius:50%;background:${color};border:2px solid white;box-shadow:0 2px 4px rgba(0,0,0,0.3)`;
  return L.divIcon({
    className: 'custom-marker',
    html: el.outerHTML,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
}

export default function FleetMap() {
  return (
    <div className="h-full rounded-lg overflow-hidden">
      <MapContainer center={[37.78, -122.4]} zoom={13} className="h-full">
        <TileLayer
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          attribution='&copy; <a href="https://carto.com">CARTO</a>'
        />
        {GEOFENCES.map(zone => (
          <Circle
            key={zone.id}
            center={zone.center}
            radius={zone.radius}
            pathOptions={{ color: zone.color, fillOpacity: 0.1, weight: 2 }}
          >
            <Popup>
              <div className="text-sm">
                <strong>{zone.name}</strong>
                <br />Radius: {zone.radius}m
              </div>
            </Popup>
          </Circle>
        ))}
        {DEVICES.map(device => (
          <Marker
            key={device.id}
            position={[device.lat, device.lng]}
            icon={createIcon(device.status)}
          >
            <Popup>
              <div className="text-sm min-w-[180px]">
                <strong>{device.name}</strong> ({device.id})
                <div className="mt-1 space-y-0.5 text-xs text-gray-600">
                  <div>Fleet: {device.fleet}</div>
                  <div>Status: <span style={{color: statusColors[device.status]}}>{device.status}</span></div>
                  {device.battery !== null && <div>Battery: {device.battery}%</div>}
                  {device.speed !== null && <div>Speed: {device.speed} km/h</div>}
                  {device.temp !== null && <div>Temp: {device.temp}°C</div>}
                  <div>Firmware: v{device.firmware}</div>
                </div>
              </div>
            </Popup>
          </Marker>
        ))}
      </MapContainer>
    </div>
  );
}
