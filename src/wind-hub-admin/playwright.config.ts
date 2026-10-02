import { existsSync } from 'node:fs'
import { join } from 'node:path'

import { defineConfig } from '@playwright/test'

// 前端端到端（Playwright）：真实浏览器加载 vite dev server 渲染的 SPA。
// 与 Vitest 单元层互不收集；经 `npm run test:e2e` 显式触发。
// 需要真实后端时由 WIND_HUB_E2E_API_BASE 指向运行中的 Server。

// 本机无系统级浏览器依赖（libnspr4/libnss3/libasound 等）时，Playwright
// 下载的 chromium 无法启动。conda 环境（conda-forge nspr/nss/alsa-lib）
// 提供这些库，通过 LD_LIBRARY_PATH 注入浏览器进程，无需 sudo 装系统包。
function browserEnv(): Record<string, string> {
  const env = { ...process.env } as Record<string, string>
  const condaLib = process.env.CONDA_PREFIX ? join(process.env.CONDA_PREFIX, 'lib') : ''
  if (condaLib && existsSync(condaLib)) {
    env.LD_LIBRARY_PATH = env.LD_LIBRARY_PATH ? `${condaLib}:${env.LD_LIBRARY_PATH}` : condaLib
  }
  return env
}

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:15173',
    launchOptions: { env: browserEnv() },
  },
  webServer: {
    command: 'npm run dev -- --port 15173 --strictPort',
    url: 'http://127.0.0.1:15173',
    reuseExistingServer: false,
    timeout: 60_000,
  },
})
