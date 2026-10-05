// client.ts 单元测试：统一错误信封解析与网络错误语义。
// 后端契约：失败响应为 {"error": {"code","message","details"}}——前端必须
// 按信封提取稳定 code（错误分派依据），不得退化成裸 HTTP 状态文本。
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError, jsonBody } from '../../src/api/client'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api 成功路径', () => {
  it('2xx 返回解析后的 JSON 体', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(200, { ok: 1 })),
    )
    await expect(api<{ ok: number }>('/x')).resolves.toEqual({ ok: 1 })
  })

  it('204 返回 undefined', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(null, { status: 204 })),
    )
    await expect(api('/x')).resolves.toBeUndefined()
  })

  it('带 body 的请求自动设置 JSON Content-Type', async () => {
    const spy = vi.fn(async () => jsonResponse(200, {}))
    vi.stubGlobal('fetch', spy)
    await api('/x', { method: 'POST', body: jsonBody({ a: 1 }) })
    const headers = new Headers((spy.mock.calls[0][1] as RequestInit).headers)
    expect(headers.get('Content-Type')).toBe('application/json')
  })
})

describe('api 错误信封', () => {
  it('按统一信封提取 code/message/details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(503, {
          error: {
            code: 'PROTOCOL_ERROR',
            message: 'device unreachable',
            details: { device: 'd1' },
          },
        }),
      ),
    )
    const err = await api('/x').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    const apiErr = err as ApiError
    expect(apiErr.status).toBe(503)
    expect(apiErr.code).toBe('PROTOCOL_ERROR')
    expect(apiErr.message).toBe('device unreachable')
    expect(apiErr.details).toEqual({ device: 'd1' })
  })

  it('非 JSON 错误响应退化为 HTTP 状态文本', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('Bad Gateway', { status: 502, statusText: 'Bad Gateway' })),
    )
    const err = (await api('/x').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(502)
    expect(err.code).toBe('HTTP_ERROR')
  })

  it('fetch 抛异常映射为 NETWORK_ERROR（status 0）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )
    const err = (await api('/x').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(0)
    expect(err.code).toBe('NETWORK_ERROR')
  })
})
