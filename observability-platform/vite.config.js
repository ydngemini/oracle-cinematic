import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  define: {
    'import.meta.env.VITE_WS_URL': JSON.stringify(process.env.VITE_WS_URL || 'ws://localhost:8000'),
    // REST base for /auth/login (mirrors the main Neoh app's VITE_API_BASE).
    'import.meta.env.VITE_API_BASE': JSON.stringify(process.env.VITE_API_BASE || 'http://localhost:8000'),
    // Optional: a pre-minted platform_admin JWT to skip the login screen (CI / kiosk).
    'import.meta.env.VITE_AWS_WS_TOKEN': JSON.stringify(process.env.VITE_AWS_WS_TOKEN || ''),
  },
});
