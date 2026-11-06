import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物直接落到后端静态目录（server.py STATIC_DIR），SPA 回退已就绪
export default defineConfig({
  base: '/',
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: '../src/mdva/web/static',
    emptyOutDir: true,
    assetsInlineLimit: 4096,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
})
