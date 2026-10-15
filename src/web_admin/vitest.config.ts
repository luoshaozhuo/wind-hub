import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// 测试文件位于项目根之外的 ../../tests：裸导入无法沿目录树解析回
// src/web_admin/node_modules，显式别名到本包解析出的真实入口。
const TEST_EXTERNALS = [
  'vue',
  'pinia',
  'element-plus',
  '@vue/test-utils',
  '@tanstack/vue-query',
  'msw',
]
const externalsAlias = TEST_EXTERNALS.map((name) => ({
  find: new RegExp(`^${name.replace(/\//g, '\\/')}$`),
  replacement: fileURLToPath(import.meta.resolve(name)),
}))

// 前端单元/组件测试：Vitest + jsdom + Vue Test Utils。
// 只收集仓库根目录 tests/web_admin/unit；Playwright 端到端在 tests/e2e（独立 runner）。
// VITE_API_BASE 指向不存在的测试域：未启动 MSW 的用例天然走“后端不可达”，
// 启动 MSW（src/mocks/node.ts）的用例在 HTTP 边界被拦截，与 dev:mock 同契约。
export default defineConfig({
  plugins: [vue()],
  // 测试文件位于项目根之外的 ../../tests：放开 fs.strict，
  // 否则 Vite 将以 /@fs/ URL 形式拒绝加载（vitest 5 + plugin-vue）。
  server: {
    fs: { strict: false },
  },
  resolve: {
    alias: externalsAlias,
  },
  test: {
    environment: 'jsdom',
    include: ['../../tests/web_admin/unit/**/*.spec.ts'],
    env: {
      VITE_API_BASE: 'http://wind-hub-mock.test',
    },
  },
})
