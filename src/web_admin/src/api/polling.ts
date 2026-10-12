/** 页面级周期刷新：组件挂载即启动，卸载即停止，跳过重叠调用。 */

import { onMounted, onUnmounted } from 'vue'

export function usePolling(refresh: () => Promise<void>, intervalMs = 5_000): void {
  let timer: ReturnType<typeof setInterval> | undefined
  let running = false
  onMounted(() => {
    void refresh()
    timer = setInterval(() => {
      if (running) return
      running = true
      void refresh().finally(() => {
        running = false
      })
    }, intervalMs)
  })
  onUnmounted(() => {
    if (timer !== undefined) clearInterval(timer)
  })
}
