import { useState } from 'react';
import Sidebar from './components/Sidebar';
import FleetMap from './components/FleetMap';
import TelemetryCharts from './components/TelemetryCharts';
import AlertFeed from './components/AlertFeed';
import FirmwarePanel from './components/FirmwarePanel';
import DeviceList from './components/DeviceList';
import StatsBar from './components/StatsBar';

export default function App() {
  const [activeTab, setActiveTab] = useState('map');

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar activeTab={activeTab} onTabChange={setActiveTab} />
      <main className="flex-1 flex flex-col overflow-hidden">
        <StatsBar />
        <div className="flex-1 p-4 overflow-auto">
          {activeTab === 'map' && <FleetMap />}
          {activeTab === 'telemetry' && <TelemetryCharts />}
          {activeTab === 'alerts' && <AlertFeed />}
          {activeTab === 'firmware' && <FirmwarePanel />}
          {activeTab === 'devices' && <DeviceList />}
        </div>
      </main>
    </div>
  );
}
