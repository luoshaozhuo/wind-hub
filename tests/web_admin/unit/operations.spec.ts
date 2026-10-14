// awaitOperation 测试：唯一的 Operation 等待 helper（500ms 轮询）。
// 终态语义：success/partial 返回，failed/cancelled 抛 ApiError（保留后端错误码）。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, client } from '../../src/api/client'
import { awaitOperation, type OperationDto } from '../../src/api/operations'

vi.mock('../../src/api/client', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/api/client')>()
  return { ...mod, client: { GET: vi.fn() } }
})

const getMock = vi.mocked(client.GET)

function operationRow(state: string, overrides: Record<string, unknown> = {}): OperationDto {
  return {
    operation_id: 'op-1',
    state,
    completed: 0,
    total: 10,
    error: null,
    result: null,
    ...overrides,
  } as unknown as OperationDto
}

function ok(row: OperationDto) {
  return Promise.resolve({ data: row, response: new Response(null, { status: 200 }) })
}

describe('awaitOperation', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    getMock.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('success 终态直接返回', async () => {
    getMock.mockReturnValueOnce(ok(operationRow('success', { completed: 10 })))
    const row = await awaitOperation('op-1')
    expect(row.state).toBe('success')
    expect(getMock).toHaveBeenCalledTimes(1)
  })

  it('轮询直到终态并回报进度', async () => {
    getMock
      .mockReturnValueOnce(ok(operationRow('running', { completed: 3 })))
      .mockReturnValueOnce(ok(operationRow('running', { completed: 7 })))
      .mockReturnValueOnce(ok(operationRow('partial', { completed: 10 })))
    const progress: Array<[number, number]> = []

    const promise = awaitOperation('op-1', (completed, total) => progress.push([completed, total]))
    await vi.advanceTimersByTimeAsync(1200)
    const row = await promise

    expect(row.state).toBe('partial')
    expect(getMock).toHaveBeenCalledTimes(3)
    expect(progress).toEqual([
      [3, 10],
      [7, 10],
      [10, 10],
    ])
  })

  it('failed 终态抛 ApiError 并保留后端错误码', async () => {
    getMock.mockReturnValueOnce(
      ok(operationRow('failed', { error: { code: 'DEVICE_UNREACHABLE', message: 'no route' } })),
    )
    await expect(awaitOperation('op-1')).rejects.toMatchObject({
      status: 409,
      code: 'DEVICE_UNREACHABLE',
      message: 'no route',
    })
  })

  it('cancelled 终态抛 ApiError', async () => {
    getMock.mockReturnValueOnce(ok(operationRow('cancelled')))
    const promise = awaitOperation('op-1')
    await expect(promise).rejects.toBeInstanceOf(ApiError)
    await expect(promise).rejects.toMatchObject({ code: 'OPERATION_CANCELLED' })
  })

  it('轮询请求失败时按 ApiError 传播', async () => {
    getMock.mockRejectedValueOnce(new ApiError('boom', 500, 'X'))
    await expect(awaitOperation('op-1')).rejects.toMatchObject({ code: 'X' })
  })
})
