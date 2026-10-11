import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import { queryClient } from './api/queryClient'
import './styles/index.css'

// VITE_API_MODE=mock 时先启动 MSW（HTTP 边界拦截 /api/v1/...），再 mount Vue；
// real 模式（默认）不加载 MSW。mock 启动失败必须显式报错，不能静默白屏。
async function bootstrap() {
  if (import.meta.env.VITE_API_MODE === 'mock') {
    try {
      const { worker } = await import('./mocks/browser')
      await worker.start({ onUnhandledRequest: 'bypass' })
    } catch (error) {
      // 明确报错后继续 mount：页面以 API OFFLINE 形态可用，不静默白屏。
      console.error('[wind-hub-admin] MSW mock worker failed to start:', error)
    }
  }

  createApp(App)
    .use(createPinia())
    .use(VueQueryPlugin, { queryClient })
    .use(ElementPlus)
    .mount('#app')
}

void bootstrap()
