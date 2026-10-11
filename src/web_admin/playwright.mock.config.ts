import { existsSync } from 'node:fs'
import { join } from 'node:path'

import { defineConfig } from '@playwright/test'

// Mock 模式端到端：`npm run dev:mock` 启动的 SPA（MSW 拦截 /api/v1）。
// 与默认 playwright.config.ts（real 模式、后端可缺席）并列，
// 经 `npm run test:e2e:mock` 显式触发。

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
  testMatch: 'mock.spec.ts',
  timeout: 30_000,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:15174',
    launchOptions: { env: browserEnv() },
  },
  webServer: {
    command: 'npm run dev:mock -- --host 127.0.0.1 --port 15174 --strictPort',
    url: 'http://127.0.0.1:15174',
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
    timeout: 60_000,
  },
})
