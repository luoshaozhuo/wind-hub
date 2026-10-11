// Bootstrap 模式切换：mock 模式启动 MSW worker 后再 mount；real 模式（默认）
// 不加载 MSW。worker.start 失败必须抛出并 console.error，不得静默白屏。
import { afterEach, describe, expect, it, vi } from 'vitest'

const startMock = vi.fn().mockResolvedValue(undefined)

vi.mock('../../src/mocks/browser', () => ({
  worker: { start: (...args: unknown[]) => startMock(...args) },
}))

describe('main.ts bootstrap', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.resetModules()
    startMock.mockClear()
    document.body.innerHTML = ''
  })

  it('VITE_API_MODE=mock 时启动 MSW worker', async () => {
    vi.stubEnv('VITE_API_MODE', 'mock')
    document.body.innerHTML = '<div id="app"></div>'
    await import('../../src/main')
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(startMock).toHaveBeenCalledTimes(1)
    expect(document.querySelector('#app .app-shell')).not.toBeNull()
  })

  it('real 模式（默认）不启动 MSW', async () => {
    vi.stubEnv('VITE_API_MODE', 'real')
    document.body.innerHTML = '<div id="app"></div>'
    await import('../../src/main')
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(startMock).not.toHaveBeenCalled()
    expect(document.querySelector('#app .app-shell')).not.toBeNull()
  })

  it('mock worker 启动失败时 console.error 且页面仍挂载', async () => {
    vi.stubEnv('VITE_API_MODE', 'mock')
    document.body.innerHTML = '<div id="app"></div>'
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    startMock.mockRejectedValueOnce(new Error('sw registration failed'))
    await import('../../src/main')
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(consoleSpy).toHaveBeenCalledWith(
      '[wind-hub-admin] MSW mock worker failed to start:',
      expect.any(Error),
    )
    expect(document.querySelector('#app .app-shell')).not.toBeNull()
    consoleSpy.mockRestore()
  })
})
