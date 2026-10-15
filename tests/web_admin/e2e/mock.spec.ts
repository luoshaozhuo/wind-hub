// Mock 模式端到端：dev:mock（MSW）下不启动真实 Server 也能浏览完整数据。
// 契约：无 API OFFLINE；大规模数据可见；故障场景与跨页面操作可观察。
import { expect, test } from '@playwright/test'

test('mock 模式：Header LIVE，无 API 错误', async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.app-header')).toContainText('LIVE')
  await expect(page.locator('.app-header')).not.toContainText('API OFFLINE')
  await expect(page.locator('.app-api-error')).toHaveCount(0)
})

test('mock 模式：Overview 显示大规模派生数据', async ({ page }) => {
  await page.goto('/')
  await page.locator('.el-menu-item', { hasText: 'Overview' }).first().click()
  await expect(page.locator('.overview-page')).toBeVisible()
  // 56 台设备，52 台 connected（2 disabled + wtg-041 不可达 + wtg-043 协议失败）
  await expect(page.locator('.overview-page .devices-card')).toContainText('52 / 54')
})

test('mock 模式：Devices 故障设备与搜索过滤', async ({ page }) => {
  await page.goto('/')
  await page.locator('.el-menu-item', { hasText: 'Devices' }).first().click()
  const content = page.locator('section.content')
  await expect(content).toContainText('wtg-001')
  await expect(content).toContainText('Total 56')

  // 搜索过滤到固定故障设备
  const search = content.locator('input[placeholder*="Search device"]').first()
  await search.fill('wtg-041')
  await expect(content).toContainText('wtg-041')
  await expect(content).not.toContainText('wtg-001')
})

test('mock 模式：Tasks 故障目标启动失败', async ({ page }) => {
  await page.goto('/')
  await page.locator('.el-menu-item', { hasText: 'Tasks' }).first().click()
  const content = page.locator('section.content')
  await expect(content).toContainText('turbine-modbus-all')
  await expect(content).toContainText('wtg-041-diag')

  // wtg-041-diag 的目标网络不可达 → Start 必须失败
  const row = content.locator('tr', { hasText: 'wtg-041-diag' }).first()
  await row.locator('button', { hasText: 'Start' }).first().click()
  await expect(page.locator('.el-message--error')).toBeVisible()
  await expect(content.locator('.el-table__body')).toContainText('STOPPED')
})

test('mock 模式：Sinks 文件可用 / Modbus 不可达', async ({ page }) => {
  await page.goto('/')
  await page.locator('.el-menu-item', { hasText: 'Sinks' }).first().click()
  const content = page.locator('section.content')
  await expect(content).toContainText('redis_main')
  await expect(content).toContainText('modbus_main')
  await expect(content).toContainText('file_archive')

  const file = content.locator('tr', { hasText: 'file_archive' }).first()
  await file.getByRole('button', { name: '验证' }).click()
  await expect(file).toContainText('healthy', { timeout: 15000 })

  const modbus = content.locator('tr', { hasText: 'modbus_main' }).first()
  await modbus.getByRole('button', { name: '验证' }).click()
  await expect(modbus).toContainText('failed', { timeout: 15000 })
})

test('mock 模式：Logs 有内容且 Quality 反映故障场景', async ({ page }) => {
  await page.goto('/')
  await page.locator('.el-menu-item', { hasText: 'Logs' }).first().click()
  const content = page.locator('section.content')
  await expect(content.locator('.el-table__row').first()).toBeVisible()
  await expect(content).toContainText('wtg-041')

  await page.locator('.el-menu-item', { hasText: 'Quality' }).first().click()
  const quality = page.locator('.quality-page')
  await expect(quality).toBeVisible()
  await expect(quality).toContainText('wtg-041')
  await expect(quality).toContainText('modbus_main')
})
