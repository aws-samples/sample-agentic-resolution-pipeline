import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend, ReferenceLine } from 'recharts';
import { TELEMETRY_HISTORY } from '../data/mockData';

export default function TelemetryCharts() {
  return (
    <div className="space-y-6">
      <div className="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <h3 className="text-lg font-semibold mb-4">Engine Temperature (°C)</h3>
        <div className="h-64">
          <ResponsiveContainer>
            <LineChart data={TELEMETRY_HISTORY}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="time" stroke="#9ca3af" fontSize={12} />
              <YAxis stroke="#9ca3af" fontSize={12} domain={[50, 100]} />
              <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: '8px' }} />
              <Legend />
              <ReferenceLine y={85} stroke="#ef4444" strokeDasharray="5 5" label={{ value: 'Threshold', fill: '#ef4444', fontSize: 11 }} />
              <Line type="monotone" dataKey="TRK-001_temp" stroke="#3b82f6" name="TRK-001" dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="TRK-003_temp" stroke="#f59e0b" name="TRK-003" dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="TRK-005_temp" stroke="#ef4444" name="TRK-005" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="bg-gray-800 rounded-lg p-6 border border-gray-700">
        <h3 className="text-lg font-semibold mb-4">Vehicle Speed (km/h)</h3>
        <div className="h-64">
          <ResponsiveContainer>
            <LineChart data={TELEMETRY_HISTORY}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="time" stroke="#9ca3af" fontSize={12} />
              <YAxis stroke="#9ca3af" fontSize={12} domain={[0, 140]} />
              <Tooltip contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: '8px' }} />
              <Legend />
              <ReferenceLine y={120} stroke="#ef4444" strokeDasharray="5 5" label={{ value: 'Speed Limit', fill: '#ef4444', fontSize: 11 }} />
              <Line type="monotone" dataKey="TRK-001_speed" stroke="#3b82f6" name="TRK-001" dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="TRK-004_speed" stroke="#10b981" name="TRK-004" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
