import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_')
  const apiTarget = env.VITE_API_TARGET || (mode === 'docker' ? 'http://api:8000' : 'http://127.0.0.1:8000')
  return {
    plugins: [vue()],
    server: { proxy: { '/api': apiTarget, '/health': apiTarget } },
  }
})
