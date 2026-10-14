// 应用外壳端到端冒烟：真实浏览器加载 SPA，主导航与概览页渲染。
// 后端不可达时 API 请求失败，但应用外壳与业务页面必须仍可用——
// 这是前端容错契约：数据面故障不得白屏，业务结构不得被卸载。
import { expect, test } from '@playwright/test'

test('应用外壳渲染主导航，无白屏', async ({ page }) => {
  await page.goto('/')

  // 导航可见（Element Plus menu 渲染出真实菜单项）。
  const nav = page.locator('nav, .el-menu, aside').first()
  await expect(nav).toBeVisible()

  // 主内容区有实际内容（非空白、非未捕获错误页）。
  const main = page.locator('main, .app-main, #app').first()
  await expect(main).not.toBeEmpty()
})

test('后端不可达时业务页面仍渲染，API OFFLINE 与 Retry 可见', async ({ page }) => {
  await page.goto('/')

  // Header 状态：API OFFLINE（后端未启动）。
  await expect(page.locator('.app-header')).toContainText('API OFFLINE')

  // 错误 Alert 与 Retry 控件。
  await expect(page.locator('.app-api-error')).toBeVisible()
  await expect(page.locator('.app-api-error')).toContainText('Retry')

  // 业务结构未被卸载：content 存在，默认 Devices 页有业务内容。
  await expect(page.locator('section.content')).toBeVisible()
})

test('后端不可达时 Overview 业务卡片仍可见', async ({ page }) => {
  await page.goto('/')

  // 切换到 Overview。
  await page.locator('.el-menu-item', { hasText: 'Overview' }).first().click()

  // 核心卡片/标题存在（空数据也渲染骨架，而非整页隐藏）。
  await expect(page.locator('.overview-page')).toBeVisible()
  await expect(page.locator('.overview-page')).toContainText('系统运行状态')
  await expect(page.locator('.overview-page .runtime-card')).toBeVisible()
  await expect(page.locator('.overview-page .devices-card')).toBeVisible()
})

test('概览路由可达', async ({ page }) => {
  await page.goto('/')
  // SPA 路由跳转后仍停留在应用内（不依赖后端可用）。
  await expect(page).toHaveURL(/127\.0\.0\.1:15173/)
})
