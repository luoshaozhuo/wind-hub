// 离线渲染契约：API 失败 / 首载 pending 不得卸载业务页面。
// - 全部启动 API 失败：App 不白屏、业务页仍在、API OFFLINE + Retry 可见；
// - 首次 pending：页面渲染、Header CONNECTING；
// - 请求成功：Header LIVE、数据同步；
// - 曾成功后失败：Store 保留最后有效数据、页面继续显示旧值、API OFFLINE。
import { describe, expect, it, beforeAll, afterAll, afterEach } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import { VueQueryPlugin, QueryClient } from '@tanstack/vue-query'
import { http, HttpResponse } from 'msw'
import App from '../../src/App.vue'
import { server } from '../../src/mocks/node'
import { resetMockState } from '../../src/mocks/state'

function mountApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: 0, refetchOnWindowFocus: false } },
  })
  const wrapper = mount(App, {
    global: {
      plugins: [createPinia(), [VueQueryPlugin, { queryClient }], ElementPlus],
    },
    attachTo: document.body,
  })
  return { wrapper, queryClient }
}

async function waitFor(predicate: () => boolean, timeoutMs = 8000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    if (predicate()) return
    if (Date.now() >= deadline) throw new Error('waitFor timeout')
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
}

describe('离线渲染（后端不可达）', () => {
  it('全部启动 API 失败：业务页仍渲染，API OFFLINE + Retry 可见', async () => {
    const { wrapper } = mountApp()
    try {
      await waitFor(() => wrapper.text().includes('API OFFLINE'))
      await flushPromises()

      // 不白屏：导航与默认业务页（Devices）仍存在。
      expect(wrapper.find('.app-sidebar').exists()).toBe(true)
      expect(wrapper.find('section.content').exists()).toBe(true)
      // Devices 页面业务结构（标题/新建控件）可见。
      const text = wrapper.text()
      expect(text).toContain('Devices')
      // 错误 Alert 与 Retry。
      expect(wrapper.find('.app-api-error').exists()).toBe(true)
      const retryButton = wrapper.findAll('button').find((b) => b.text().trim() === 'Retry')
      expect(retryButton).toBeDefined()
      await retryButton!.trigger('click')
    } finally {
      wrapper.unmount()
    }
  })

  it('首次 pending：页面仍渲染，Header CONNECTING', () => {
    const { wrapper } = mountApp()
    try {
      // 挂载后查询处于 pending（fetch 尚未 settle）：同步断言。
      expect(wrapper.text()).toContain('CONNECTING')
      expect(wrapper.find('section.content').exists()).toBe(true)
    } finally {
      wrapper.unmount()
    }
  })
})

describe('MSW mock 后端下的 App 行为', () => {
  beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
  afterEach(() => {
    server.resetHandlers()
    resetMockState()
  })
  afterAll(() => server.close())

  async function mountLiveApp(): Promise<{ wrapper: VueWrapper; queryClient: QueryClient }> {
    const mounted = mountApp()
    await waitFor(() => mounted.wrapper.text().includes('LIVE'))
    await flushPromises()
    return mounted
  }

  it('请求成功：Header LIVE，无错误 Alert', async () => {
    const { wrapper } = await mountLiveApp()
    try {
      expect(wrapper.text()).toContain('LIVE')
      expect(wrapper.find('.app-api-error').exists()).toBe(false)
      expect(wrapper.text()).toContain('Mock Wind Farm')
    } finally {
      wrapper.unmount()
    }
  })

  it('曾成功后再失败：保留最后有效数据，API OFFLINE 出现', async () => {
    const { wrapper, queryClient } = await mountLiveApp()
    try {
      // Store 已同步 mock 设备数据。
      expect(wrapper.text()).toContain('inv-ads-01')

      // 断线：全部 API 返回 503。
      server.use(
        http.all('*/api/v1/*', () =>
          HttpResponse.json(
            { error: { code: 'UNAVAILABLE', message: 'backend down' } },
            { status: 503 },
          ),
        ),
      )
      // 触发新一轮查询（等价于用户点 Retry / 轮询 refetch）。
      await queryClient.refetchQueries()

      await waitFor(() => wrapper.text().includes('API OFFLINE'))
      await flushPromises()

      // 旧数据保留，页面未卸载。
      expect(wrapper.find('section.content').exists()).toBe(true)
      expect(wrapper.text()).toContain('inv-ads-01')
    } finally {
      wrapper.unmount()
    }
  })
})
