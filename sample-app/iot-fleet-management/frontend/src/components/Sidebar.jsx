import { Map, Activity, Bell, Cpu, Truck } from 'lucide-react';

const tabs = [
  { id: 'map', label: 'Fleet Map', icon: Map },
  { id: 'telemetry', label: 'Telemetry', icon: Activity },
  { id: 'alerts', label: 'Alerts', icon: Bell },
  { id: 'firmware', label: 'Firmware', icon: Cpu },
  { id: 'devices', label: 'Devices', icon: Truck },
];

export default function Sidebar({ activeTab, onTabChange }) {
  return (
    <aside className="w-64 bg-gray-800 border-r border-gray-700 flex flex-col">
      <div className="p-4 border-b border-gray-700">
        <h1 className="text-xl font-bold text-blue-400">IoT Fleet</h1>
        <p className="text-xs text-gray-400 mt-1">Management Dashboard</p>
      </div>
      <nav className="flex-1 p-2">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => onTabChange(id)}
            className={`w-full flex items-center gap-3 px-4 py-3 rounded-lg mb-1 text-sm transition-colors ${
              activeTab === id
                ? 'bg-blue-600 text-white'
                : 'text-gray-300 hover:bg-gray-700'
            }`}
          >
            <Icon size={18} />
            {label}
          </button>
        ))}
      </nav>
      <div className="p-4 border-t border-gray-700">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
          <span className="text-xs text-gray-400">All systems operational</span>
        </div>
      </div>
    </aside>
  );
}
