// 真实后端全链路 E2E：真实 Modbus 从站 + Collector + Commander + Web Server
// （均为独立真实进程，无 MSW、无 mock 数据），真实 Chromium 驱动 SPA。
// 业务效果必须来自真实链路：设备在线状态来自 Collector gRPC 聚合，
// 任务启停真正经过 Server → Collector placement 栅栏与采集引擎，
// points_collected 增长证明真实 Modbus 读事务到达 Server 指标面。
import { mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { expect, test } from '@playwright/test'

// Playwright 从 web_admin/ 目录运行；截图与 spec 同目录留存证。
const SHOT_DIR = resolve('../../tests/web_admin/e2e/screenshots')

function shot(page: import('@playwright/test').Page, name: string) {
  mkdirSync(SHOT_DIR, { recursive: true })
  return page.screenshot({ path: join(SHOT_DIR, `${name}.png`), fullPage: true })
}

/** 桌面分辨率下不得出现横向滚动/溢出（1366/1920 常见现场桌面）。 */
async function assertNoHorizontalOverflow(page: import('@playwright/test').Page) {
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement
    return { scroll: doc.scrollWidth, client: doc.clientWidth }
  })
  expect(
    overflow.scroll,
    `横向布局溢出: scrollWidth=${overflow.scroll} clientWidth=${overflow.client}`,
  ).toBeLessThanOrEqual(overflow.client + 2)
}

test.describe('真实后端全链路', () => {
  test('Header LIVE，Overview 反映真实拓扑（1/1 设备在线）', async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('.app-header')).toContainText('LIVE')
    // 默认落地页是 Devices；Overview 需显式切换。
    await page.locator('.el-menu-item', { hasText: 'Overview' }).first().click()
    await expect(page.locator('.overview-page')).toBeVisible()
    await expect(page.locator('.overview-page .devices-card')).toContainText('1 / 1')
    await expect(page.locator('.overview-page .devices-card')).toContainText('HEALTHY')
    await shot(page, '01-overview-real-backend')
    await assertNoHorizontalOverflow(page)
  })

  test('Devices 页展示真实设备 modbus-1', async ({ page }) => {
    await page.goto('/')
    await page.locator('.el-menu-item', { hasText: 'Devices' }).first().click()
    const deviceLink = page.locator('.device-id-link', { hasText: 'modbus-1' }).first()
    await expect(deviceLink).toBeVisible()
    await shot(page, '02-devices-real-device')
    await assertNoHorizontalOverflow(page)
  })

  test('Tasks 真实启停：采集点数经真实 Modbus 链路增长', async ({ page, request }) => {
    await page.goto('/')
    await page.locator('.el-menu-item', { hasText: 'Tasks' }).first().click()

    const row = page.locator('tr', { hasText: 'modbus-telemetry' }).first()
    await expect(row).toBeVisible()

    // STARTING → RUNNING 由后端真实状态机驱动；UI 只做镜像。
    await row.getByRole('button', { name: 'Start' }).click()
    await expect(row).toContainText('RUNNING', { timeout: 30_000 })

    // 真实业务效果：points_collected 从真实 Modbus 读事务累计（经 Collector
    // gRPC 上报），而非界面假数据。
    let collected = 0
    for (let attempt = 0; attempt < 30; attempt += 1) {
      const response = await request.get('/api/v1/overview')
      expect(response.ok()).toBeTruthy()
      const body = (await response.json()) as { points_collected?: number }
      collected = body.points_collected ?? 0
      if (collected > 0) break
      await page.waitForTimeout(500)
    }
    expect(collected).toBeGreaterThan(0)
    expect(
      (await (await request.get('/api/v1/overview')).json()).devices_connected,
    ).toBe(1)
    await shot(page, '03-task-running-collecting')

    await row.getByRole('button', { name: 'Stop' }).click()
    await expect(row).toContainText('STOPPED', { timeout: 20_000 })
  })

  // FINDING-WEB-LAYOUT-01（复现，修复前在长挂载路径环境失败）：
  // System Health 的 Storage 表 Mount 列固定 70px 宽，对 WSL Docker
  // bind-mount 的长 hash 路径既不省略也不截断，内容逐字符换行，
  // 单行高度膨胀到数千像素（见 screenshots/04-system-health.png）。
  // 现场 /data 等短挂载点不触发，但任何长挂载路径都会破坏桌面布局。
  test('System Health Storage 长挂载路径不撑爆行高', async ({ page }) => {
    await page.goto('/')
    await page.locator('.el-menu-item', { hasText: 'System Health' }).first().click()
    const rows = page.locator('.health-storage-table, .storage table, table').last().locator('tbody tr')
    await expect(rows.first()).toBeVisible()
    const bad = await rows.evaluateAll((nodes) =>
      nodes
        .map((tr) => ({
          height: (tr as HTMLElement).getBoundingClientRect().height,
          mount: ((tr as HTMLElement).querySelector('td')?.textContent || '').trim(),
        }))
        .filter((r) => r.mount.length > 20)
        .filter((r) => r.height > 80),
    )
    expect(bad).toEqual([])
  })

  for (const menu of ['Sinks', 'Quality', 'Logs', 'System Health']) {
    test(`桌面布局检查：${menu} 页无横向溢出`, async ({ page }) => {
      await page.goto('/')
      await page.locator('.el-menu-item', { hasText: menu }).first().click()
      await page.waitForTimeout(800)
      await shot(page, `04-${menu.toLowerCase().replace(/\s+/g, '-')}`)
      await assertNoHorizontalOverflow(page)
    })
  }
})
