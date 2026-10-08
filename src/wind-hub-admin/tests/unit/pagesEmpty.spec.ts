// 页面空数据可渲染性：Store 默认值 + 全部 API 失败（无 MSW，fetch 直连
// 不存在的测试域）时，每个主要页面都必须能挂载、不抛异常、渲染非空结构。
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import { VueQueryPlugin, QueryClient } from '@tanstack/vue-query'
import type { Component } from 'vue'

import OverviewPage from '../../src/pages/OverviewPage.vue'
import DevicesPage from '../../src/pages/DevicesPage.vue'
import PointsPage from '../../src/pages/PointsPage.vue'
import TasksPage from '../../src/pages/TasksPage.vue'
import SinksPage from '../../src/pages/SinksPage.vue'
import QualityPage from '../../src/pages/QualityPage.vue'
import DebugPage from '../../src/pages/DebugPage.vue'
import SystemHealthPage from '../../src/pages/SystemHealthPage.vue'
import SettingsPage from '../../src/pages/SettingsPage.vue'
import ConfigPage from '../../src/pages/ConfigPage.vue'
import LogsPage from '../../src/pages/LogsPage.vue'

const pages: Record<string, Component> = {
  Overview: OverviewPage,
  Devices: DevicesPage,
  Points: PointsPage,
  Tasks: TasksPage,
  Sinks: SinksPage,
  Quality: QualityPage,
  Diagnostics: DebugPage,
  SystemHealth: SystemHealthPage,
  Settings: SettingsPage,
  Config: ConfigPage,
  Logs: LogsPage,
}

describe('页面空数据可渲染性（API 不可达 + Store 默认值）', () => {
  for (const [name, component] of Object.entries(pages)) {
    it(`${name} 页面挂载不抛异常且渲染非空结构`, async () => {
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: 0, refetchOnWindowFocus: false } },
      })
      const wrapper = mount(component, {
        global: {
          plugins: [createPinia(), [VueQueryPlugin, { queryClient }], ElementPlus],
        },
        attachTo: document.body,
      })
      try {
        // 让首帧渲染与同步 watch 完成；异步查询失败不应导致卸载。
        await new Promise((resolve) => setTimeout(resolve, 50))
        expect(wrapper.html().length).toBeGreaterThan(0)
        expect(wrapper.find('div, section, main').exists()).toBe(true)
      } finally {
        wrapper.unmount()
      }
    })
  }
})
