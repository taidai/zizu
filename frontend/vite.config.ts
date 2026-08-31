import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync } from 'fs'
import { resolve } from 'path'

const rootVersionPath = resolve(__dirname, '..', 'VERSION')
const appVersion = readFileSync(rootVersionPath, 'utf-8').trim()
const developmentProxyTarget = process.env.ZIZU_DEV_PROXY_TARGET || 'http://localhost:9000'

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api': {
        target: developmentProxyTarget,
        changeOrigin: true,
        ws: true,
      },
      '/ws': {
        target: developmentProxyTarget,
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1000,
  },
})
