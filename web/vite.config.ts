import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 配置：开发服务器把 /api 和 /ws 代理到后端 127.0.0.1:8899
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8899', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8899', ws: true },
    },
  },
  build: { outDir: 'dist' },
})
