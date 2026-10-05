// client.ts 单元测试：统一错误信封解析与网络错误语义。
// 后端契约：失败响应为 {"error": {"code","message","details"}}——前端必须
// 按信封提取稳定 code（错误分派依据），不得退化成裸 HTTP 状态文本。
import { describe, expect, it } from 'vitest'

import { ApiError, call, normalizeApiError } from '../../src/api/client'

describe('normalizeApiError', () => {
  it('解析统一错误包络并保留 code/details', () => {
    const error = normalizeApiError(
      {
        error: { code: 'DEVICE_NOT_FOUND', message: 'device missing', details: { id: 'wtg-001' } },
      },
      404,
    )
    expect(error).toBeInstanceOf(ApiError)
    expect(error.status).toBe(404)
    expect(error.code).toBe('DEVICE_NOT_FOUND')
    expect(error.message).toBe('device missing')
    expect(error.details).toEqual({ id: 'wtg-001' })
  })

  it('FastAPI detail 对象兜底', () => {
    const error = normalizeApiError(
      { detail: { code: 'VALIDATION_FAILED', message: 'bad payload' } },
      422,
    )
    expect(error.code).toBe('VALIDATION_FAILED')
    expect(error.message).toBe('bad payload')
    expect(error.status).toBe(422)
  })

  it('FastAPI detail 字符串兜底', () => {
    const error = normalizeApiError({ detail: 'Not Found' }, 404)
    expect(error.message).toBe('Not Found')
    expect(error.status).toBe(404)
  })

  it('Error 实例归为 NETWORK_ERROR 且 status 0', () => {
    const error = normalizeApiError(new TypeError('fetch failed'), 0)
    expect(error.code).toBe('NETWORK_ERROR')
    expect(error.status).toBe(0)
    expect(error.message).toBe('fetch failed')
  })

  it('已是 ApiError 时原样返回', () => {
    const original = new ApiError('boom', 500, 'X')
    expect(normalizeApiError(original, 200)).toBe(original)
  })

  it('未知值兜底为 HTTP_ERROR', () => {
    const error = normalizeApiError('weird', 500)
    expect(error.code).toBe('HTTP_ERROR')
    expect(error.message).toBe('weird')
  })
})

describe('call', () => {
  it('成功时解包 data', async () => {
    await expect(
      call(Promise.resolve({ data: { ok: 1 }, response: new Response(null, { status: 200 }) })),
    ).resolves.toEqual({ ok: 1 })
  })

  it('业务错误按响应状态归一化抛出', async () => {
    const promise = call(
      Promise.resolve({
        error: { error: { code: 'TASK_RUNNING', message: 'task is running' } },
        response: new Response(null, { status: 409 }),
      }),
    )
    await expect(promise).rejects.toMatchObject({ status: 409, code: 'TASK_RUNNING' })
  })

  it('网络层 reject 归为 status 0 NETWORK_ERROR', async () => {
    const promise = call(Promise.reject(new TypeError('connection refused')))
    await expect(promise).rejects.toMatchObject({ status: 0, code: 'NETWORK_ERROR' })
  })
})
