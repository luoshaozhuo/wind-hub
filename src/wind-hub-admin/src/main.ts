import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin, QueryClient } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import './styles/index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Server State 默认即时过期：刷新策略由各页面 refetchInterval / mutation
      // invalidation 显式控制，不依赖全局缓存窗口。
      staleTime: 0,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

createApp(App)
  .use(createPinia())
  .use(VueQueryPlugin, { queryClient })
  .use(ElementPlus)
  .mount('#app')
