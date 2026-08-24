import { useState } from 'react';
import { Cpu, AlertTriangle, CheckCircle, Upload } from 'lucide-react';
import { FIRMWARE_VERSIONS, DEVICES } from '../data/mockData';

export default function FirmwarePanel() {
  const [updating, setUpdating] = useState(null);

  const triggerUpdate = (deviceType) => {
    setUpdating(deviceType);
    setTimeout(() => setUpdating(null), 3000);
  };

  return (
    <div className="space-y-6">
      <h3 className="text-lg font-semibold flex items-center gap-2">
        <Cpu size={20} className="text-blue-400" />
        Firmware Management
      </h3>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {FIRMWARE_VERSIONS.map(fw => (
          <div key={fw.device_type} className="bg-gray-800 rounded-lg p-5 border border-gray-700">
            <div className="flex items-center justify-between mb-3">
              <h4 className="font-medium">{fw.device_type}</h4>
              {fw.is_critical && (
                <span className="flex items-center gap-1 px-2 py-0.5 bg-red-500/20 text-red-400 rounded text-xs">
                  <AlertTriangle size={12} /> Critical
                </span>
              )}
            </div>

            <div className="space-y-2 text-sm">
              <div className="flex justify-between text-gray-400">
                <span>Latest Version</span>
                <span className="text-white font-mono">v{fw.version}</span>
              </div>
              <div className="flex justify-between text-gray-400">
                <span>Released</span>
                <span>{fw.release_date}</span>
              </div>
              <div className="flex justify-between text-gray-400">
                <span>Up to date</span>
                <span className="text-green-400">{fw.devices_on_version} devices</span>
              </div>
              <div className="flex justify-between text-gray-400">
                <span>Outdated</span>
                <span className={fw.devices_outdated > 0 ? 'text-amber-400' : 'text-green-400'}>
                  {fw.devices_outdated} devices
                </span>
              </div>
            </div>

            {fw.devices_outdated > 0 && (
              <button
                onClick={() => triggerUpdate(fw.device_type)}
                disabled={updating === fw.device_type}
                className="mt-4 w-full flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-600 rounded-lg text-sm transition-colors"
              >
                {updating === fw.device_type ? (
                  <>Pushing update...</>
                ) : (
                  <><Upload size={14} /> Push OTA Update</>
                )}
              </button>
            )}
            {fw.devices_outdated === 0 && (
              <div className="mt-4 flex items-center justify-center gap-2 text-green-400 text-sm">
                <CheckCircle size={14} /> All devices current
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="bg-gray-800 rounded-lg p-5 border border-gray-700">
        <h4 className="font-medium mb-3">Device Firmware Status</h4>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 border-b border-gray-700">
                <th className="text-left py-2">Device</th>
                <th className="text-left py-2">Type</th>
                <th className="text-left py-2">Current</th>
                <th className="text-left py-2">Latest</th>
                <th className="text-left py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {DEVICES.map(device => {
                const latest = FIRMWARE_VERSIONS.find(f => f.device_type === device.type);
                const isUpToDate = latest && device.firmware === latest.version;
                return (
                  <tr key={device.id} className="border-b border-gray-700/50">
                    <td className="py-2">{device.name}</td>
                    <td className="py-2 text-gray-400">{device.type}</td>
                    <td className="py-2 font-mono">v{device.firmware}</td>
                    <td className="py-2 font-mono text-gray-400">v{latest?.version || '?'}</td>
                    <td className="py-2">
                      {isUpToDate ? (
                        <span className="text-green-400 text-xs">Current</span>
                      ) : (
                        <span className="text-amber-400 text-xs">Update available</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
