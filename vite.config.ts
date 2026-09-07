import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: { rollupOptions: { input: {main:fileURLToPath(new URL('./index.html',import.meta.url)),login:fileURLToPath(new URL('./login.html',import.meta.url))}, output: { manualChunks: { 'tradingview-chart': ['lightweight-charts'], motion: ['motion/react'] } } } },
  server: { port: 5173, strictPort: true, proxy: { '/api': 'http://127.0.0.1:18473', '/login':'http://127.0.0.1:18473' } },
})
