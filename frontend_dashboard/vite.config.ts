import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// spec/03 §2: pure SPA build output. Dev port is fixed at 5173 to ease joint debugging with the backend WS/8000.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
});
