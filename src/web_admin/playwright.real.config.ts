import { existsSync } from 'node:fs'
import { join } from 'node:path'

import { defineConfig } from '@playwright/test'

// 真实后端 E2E：真实 Modbus 从站 + Collector + Commander + Web Server
// （编排见 tests/web_admin/e2e/real_backend_stack.py）+ Vite dev server，
// 真实浏览器驱动。仅收集 real_backend.spec.ts。
function browserEnv(): Record<string, string> {
  const env = { ...process.env } as Record<string, string>
  const condaLib = process.env.CONDA_PREFIX ? join(process.env.CONDA_PREFIX, 'lib') : ''
  if (condaLib && existsSync(condaLib)) {
    env.LD_LIBRARY_PATH = env.LD_LIBRARY_PATH ? `${condaLib}:${env.LD_LIBRARY_PATH}` : condaLib
  }
  return env
}

export default defineConfig({
  testDir: '../../tests/web_admin/e2e',
  testMatch: 'real_backend.spec.ts',
  timeout: 60_000,
  expect: { timeout: 20_000 },
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:15175',
    viewport: { width: 1920, height: 1080 },
    launchOptions: { env: browserEnv() },
  },
  webServer: [
    {
      command: 'exec python3 ../../tests/web_admin/e2e/real_backend_stack.py',
      url: 'http://127.0.0.1:8080/openapi.json',
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
      timeout: 90_000,
      // 三个真实进程的优雅停机需要回收窗口。
      gracePeriod: 25_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 15175 --strictPort',
      url: 'http://127.0.0.1:15175',
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
      timeout: 60_000,
    },
  ],
})
