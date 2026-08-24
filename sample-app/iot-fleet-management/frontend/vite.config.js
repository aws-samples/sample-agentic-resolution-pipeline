import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/telemetry': 'http://localhost:8080',
      '/alerts': 'http://localhost:8081',
      '/firmware': 'http://localhost:8082',
      '/geofence': 'http://localhost:8083',
    },
  },
});
