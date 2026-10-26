import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  define: { __HERMES_UI_BUILD__: JSON.stringify(loadEnv(mode, '.', '').HERMES_UI_BUILD_SHA || 'development') },
  server: {
    port: 18878,
    strictPort: true,
    proxy: { '/api': { target: loadEnv(mode, '.', '').ATLAS_API_URL || 'http://127.0.0.1:18877', changeOrigin: true } },
  },
}));
