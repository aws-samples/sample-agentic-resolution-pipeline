import { useState } from 'react';
import { AlertTriangle, CheckCircle, Clock } from 'lucide-react';
import { ALERTS } from '../data/mockData';

const severityStyles = {
  HIGH: 'border-red-500 bg-red-500/10',
  MEDIUM: 'border-amber-500 bg-amber-500/10',
  LOW: 'border-blue-500 bg-blue-500/10',
};

const severityBadge = {
  HIGH: 'bg-red-500/20 text-red-400',
  MEDIUM: 'bg-amber-500/20 text-amber-400',
  LOW: 'bg-blue-500/20 text-blue-400',
};

export default function AlertFeed() {
  const [alerts, setAlerts] = useState(ALERTS);

  const acknowledge = (id) => {
    setAlerts(prev => prev.map(a => a.id === id ? { ...a, acknowledged: true } : a));
  };

  const activeAlerts = alerts.filter(a => !a.acknowledged);
  const resolvedAlerts = alerts.filter(a => a.acknowledged);

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-lg font-semibold mb-3 flex items-center gap-2">
          <AlertTriangle size={20} className="text-amber-400" />
          Active Alerts ({activeAlerts.length})
        </h3>
        <div className="space-y-3">
          {activeAlerts.map(alert => (
            <div key={alert.id} className={`border-l-4 rounded-lg p-4 ${severityStyles[alert.severity]}`}>
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${severityBadge[alert.severity]}`}>
                      {alert.severity}
                    </span>
                    <span className="text-sm text-gray-400">{alert.device_id}</span>
                    <span className="text-xs text-gray-500 flex items-center gap-1">
                      <Clock size={12} />
                      {new Date(alert.timestamp).toLocaleTimeString()}
                    </span>
                  </div>
                  <p className="text-sm">{alert.message}</p>
                </div>
                <button
                  onClick={() => acknowledge(alert.id)}
                  className="px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded text-xs transition-colors"
                >
                  Acknowledge
                </button>
              </div>
            </div>
          ))}
          {activeAlerts.length === 0 && (
            <p className="text-gray-500 text-sm">No active alerts</p>
          )}
        </div>
      </div>

      <div>
        <h3 className="text-lg font-semibold mb-3 flex items-center gap-2">
          <CheckCircle size={20} className="text-green-400" />
          Acknowledged ({resolvedAlerts.length})
        </h3>
        <div className="space-y-2">
          {resolvedAlerts.map(alert => (
            <div key={alert.id} className="border border-gray-700 rounded-lg p-3 opacity-60">
              <div className="flex items-center gap-2">
                <span className={`px-2 py-0.5 rounded text-xs ${severityBadge[alert.severity]}`}>
                  {alert.severity}
                </span>
                <span className="text-sm text-gray-400">{alert.device_id}</span>
                <span className="text-sm text-gray-500">{alert.message}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
