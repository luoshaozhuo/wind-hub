// 全局共享的 QueryClient：main.ts 挂载 VueQueryPlugin 与 store/composable
// 内的 invalidateQueries 必须使用同一实例，因此独立成模块。
import { QueryClient } from '@tanstack/vue-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Server State 默认即时过期：刷新策略由各页面 refetchInterval 与
      // mutation invalidation 显式控制，不依赖全局缓存窗口。
      staleTime: 0,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})
