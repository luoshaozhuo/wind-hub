import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 前端单元/组件测试：Vitest + jsdom + Vue Test Utils。
// 只收集 tests/unit；Playwright 端到端在 tests/e2e（独立 runner）。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['tests/unit/**/*.spec.ts'],
  },
})
