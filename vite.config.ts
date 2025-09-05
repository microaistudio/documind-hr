// Path: ui/vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api':   { target: 'http://127.0.0.1:9000', changeOrigin: true, secure: false },
      '/files': { target: 'http://127.0.0.1:9000', changeOrigin: true, secure: false },
      '/health':{ target: 'http://127.0.0.1:9000', changeOrigin: true, secure: false }
    }
  }
})
