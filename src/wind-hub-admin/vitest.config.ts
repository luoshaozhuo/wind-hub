import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 前端单元/组件测试：Vitest + jsdom + Vue Test Utils。
// 只收集 tests/unit；Playwright 端到端在 tests/e2e（独立 runner）。
// VITE_API_BASE 指向不存在的测试域：未启动 MSW 的用例天然走“后端不可达”，
// 启动 MSW（src/mocks/node.ts）的用例在 HTTP 边界被拦截，与 dev:mock 同契约。
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['tests/unit/**/*.spec.ts'],
    env: {
      VITE_API_BASE: 'http://wind-hub-mock.test',
    },
  },
})
