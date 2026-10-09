import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// spec/03 §2：纯 SPA 构建产物。dev 端口固定 5173，便于与后端 WS/8000 联调。
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
