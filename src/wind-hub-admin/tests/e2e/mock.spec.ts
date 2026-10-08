// Mock 模式端到端：dev:mock（MSW）下不启动真实 Server 也能浏览完整数据。
// 契约：无 API OFFLINE；Overview 有 mock 数据；Devices / Tasks 可切换。
import { expect, test } from '@playwright/test'

test('mock 模式：Header LIVE，无 API 错误', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.app-header')).toContainText('LIVE')
  await expect(page.locator('.app-header')).not.toContainText('API OFFLINE')
  await expect(page.locator('.app-api-error')).toHaveCount(0)
})

test('mock 模式：Overview 显示 mock 数据', async ({ page }) => {
  await page.goto('/')
  await page.locator('.el-menu-item', { hasText: 'Overview' }).first().click()
  await expect(page.locator('.overview-page')).toBeVisible()
  await expect(page.locator('.overview-page')).toContainText('系统运行状态')
  // mock 设备：3 台中 1 台 online（devices-card 显示 Online / Enabled）。
  await expect(page.locator('.overview-page .devices-card')).toContainText('1 /')
})

test('mock 模式：Devices / Tasks 页面可切换且有数据', async ({ page }) => {
  await page.goto('/')

  await page.locator('.el-menu-item', { hasText: 'Devices' }).first().click()
  await expect(page.locator('section.content')).toContainText('inv-ads-01')
  await expect(page.locator('section.content')).toContainText('wtg-104-01')

  await page.locator('.el-menu-item', { hasText: 'Tasks' }).first().click()
  await expect(page.locator('section.content')).toContainText('task-inv-fast')
  await expect(page.locator('section.content')).toContainText('task-wtg-slow')
})
