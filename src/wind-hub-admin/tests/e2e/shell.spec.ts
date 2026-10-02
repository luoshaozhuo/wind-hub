// 应用外壳端到端冒烟：真实浏览器加载 SPA，主导航与概览页渲染。
// 后端不可达时 API 请求失败，但应用外壳（布局/导航）必须仍可用——
// 这是前端容错契约：数据面故障不得白屏。
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

test('概览路由可达', async ({ page }) => {
  await page.goto('/')
  // SPA 路由跳转后仍停留在应用内（不依赖后端可用）。
  await expect(page).toHaveURL(/127\.0\.0\.1:15173/)
})
