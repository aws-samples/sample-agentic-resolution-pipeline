import { Wifi, WifiOff, AlertTriangle, Battery, Thermometer, Gauge } from 'lucide-react';
import { DEVICES } from '../data/mockData';

const statusConfig = {
  online: { icon: Wifi, color: 'text-green-400', bg: 'bg-green-400/10' },
  warning: { icon: AlertTriangle, color: 'text-amber-400', bg: 'bg-amber-400/10' },
  offline: { icon: WifiOff, color: 'text-red-400', bg: 'bg-red-400/10' },
};

export default function DeviceList() {
  return (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold">Fleet Devices ({DEVICES.length})</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {DEVICES.map(device => {
          const status = statusConfig[device.status];
          const StatusIcon = status.icon;
          return (
            <div key={device.id} className="bg-gray-800 rounded-lg p-4 border border-gray-700 hover:border-gray-600 transition-colors">
              <div className="flex items-center justify-between mb-3">
                <div>
                  <h4 className="font-medium">{device.name}</h4>
                  <p className="text-xs text-gray-500">{device.id}</p>
                </div>
                <div className={`flex items-center gap-1.5 px-2 py-1 rounded ${status.bg}`}>
                  <StatusIcon size={12} className={status.color} />
                  <span className={`text-xs ${status.color}`}>{device.status}</span>
                </div>
              </div>

              <div className="grid grid-cols-3 gap-2">
                {device.battery !== null && (
                  <div className="text-center">
                    <Battery size={14} className={`mx-auto ${device.battery < 20 ? 'text-red-400' : 'text-gray-400'}`} />
                    <p className="text-xs mt-1">{device.battery}%</p>
                  </div>
                )}
                {device.temp !== null && (
                  <div className="text-center">
                    <Thermometer size={14} className={`mx-auto ${device.temp > 85 ? 'text-red-400' : 'text-gray-400'}`} />
                    <p className="text-xs mt-1">{device.temp}°C</p>
                  </div>
                )}
                {device.speed !== null && (
                  <div className="text-center">
                    <Gauge size={14} className={`mx-auto ${device.speed > 120 ? 'text-red-400' : 'text-gray-400'}`} />
                    <p className="text-xs mt-1">{device.speed} km/h</p>
                  </div>
                )}
              </div>

              <div className="mt-3 pt-3 border-t border-gray-700 flex justify-between text-xs text-gray-500">
                <span>{device.fleet}</span>
                <span>v{device.firmware}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
