import { Truck, Wifi, WifiOff, AlertTriangle } from 'lucide-react';
import { DEVICES, ALERTS } from '../data/mockData';

export default function StatsBar() {
  const online = DEVICES.filter(d => d.status === 'online').length;
  const offline = DEVICES.filter(d => d.status === 'offline').length;
  const warning = DEVICES.filter(d => d.status === 'warning').length;
  const activeAlerts = ALERTS.filter(a => !a.acknowledged).length;

  const stats = [
    { label: 'Total Devices', value: DEVICES.length, icon: Truck, color: 'text-blue-400' },
    { label: 'Online', value: online, icon: Wifi, color: 'text-green-400' },
    { label: 'Offline', value: offline, icon: WifiOff, color: 'text-red-400' },
    { label: 'Active Alerts', value: activeAlerts, icon: AlertTriangle, color: 'text-amber-400' },
  ];

  return (
    <div className="bg-gray-800 border-b border-gray-700 px-6 py-3">
      <div className="flex items-center gap-8">
        {stats.map(({ label, value, icon: Icon, color }) => (
          <div key={label} className="flex items-center gap-2">
            <Icon size={16} className={color} />
            <span className="text-sm text-gray-400">{label}:</span>
            <span className={`text-sm font-semibold ${color}`}>{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
