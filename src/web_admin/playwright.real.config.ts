import { existsSync } from 'node:fs'
import { join } from 'node:path'

import { defineConfig } from '@playwright/test'

// 真实后端端到端：webServer 托管 scripts/acceptance.py（40 台模拟 PLC +
// Collector + Commander + Web Server + Vite dev server），浏览器驱动
// 完整业务链验收。经 `npm run test:e2e:real` 显式触发；与 shell.spec.ts
// （无后端冒烟）互不混跑——testMatch 只收 real_ 前缀用例。
//
// 与 playwright.config.ts 相同的 conda 库注入：本机无系统级浏览器依赖
// 时，chromium 需要 LD_LIBRARY_PATH 指向 conda-forge 的 nspr/nss/alsa。
function browserEnv(): Record<string, string> {
  const env = { ...process.env } as Record<string, string>
  const condaLib = process.env.CONDA_PREFIX ? join(process.env.CONDA_PREFIX, 'lib') : ''
  if (condaLib && existsSync(condaLib)) {
    env.LD_LIBRARY_PATH = env.LD_LIBRARY_PATH ? `${condaLib}:${env.LD_LIBRARY_PATH}` : condaLib
  }
  return env
}

export default defineConfig({
  testDir: '../../tests/system/web_admin',
  testMatch: 'real_*.spec.ts',
  timeout: 60_000,
  retries: 0,
  workers: 1, // 全链共享一个验收环境，串行执行
  use: {
    baseURL: 'http://127.0.0.1:5173',
    launchOptions: { env: browserEnv() },
  },
  webServer: {
    // 前台驻留模式：Playwright 终止本进程即触发整链优雅停机。
    command: 'python3 ../../scripts/acceptance.py start --foreground --with-frontend',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: false,
    stdout: 'pipe',
    stderr: 'pipe',
    timeout: 180_000, // 40 台协议 server + 三个服务 + Vite 冷启动
  },
})
