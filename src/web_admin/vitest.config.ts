import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 集中式测试位于仓库 tests/ 下，超出默认 fs 白名单（web_admin 目录），
// 需显式允许，否则 Vite 以 /@fs/ 加载失败。
export default defineConfig({
  plugins: [vue()],
  server: { fs: { allow: ['../..'] } },
  test: {
    environment: 'jsdom',
    include: ['../../tests/web_admin/unit/**/*.spec.ts'],
    env: { VITE_API_BASE: 'http://wind-hub-test.invalid' },
  },
})
