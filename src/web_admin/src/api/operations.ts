// Operations capability：异步 Operation 查询与极薄等待 helper。
// 等待实现收敛于此一处（500ms 轮询 / 5 分钟超时，语义与旧 pollOperation 一致）；
// 页面不再各自写 sleep/while 循环。
import { promiseTimeout } from '@vueuse/core'
import { ApiError, call, client } from './client'
import type { components } from './generated/schema'

export type OperationDto = components['schemas']['OperationResponse']

const OPERATION_POLL_MS = 500
const OPERATION_TIMEOUT_MS = 5 * 60 * 1000
const TERMINAL_STATES = ['success', 'partial', 'failed', 'cancelled']

export function fetchOperation(operationId: string): Promise<OperationDto> {
  return call(
    client.GET('/api/v1/operations/{operation_id}', {
      params: { path: { operation_id: operationId } },
    }),
  )
}

export class OperationWaitCancelledError extends Error {
  constructor() {
    super('Operation wait cancelled')
    this.name = 'OperationWaitCancelledError'
  }
}

export async function awaitOperation(
  operationId: string,
  onProgress?: (completed: number, total: number) => void,
  signal?: AbortSignal,
): Promise<OperationDto> {
  const deadline = Date.now() + OPERATION_TIMEOUT_MS
  for (;;) {
    // 调用方取消（页面卸载/用户中止）时立即停止轮询；仅停止本地等待，
    // 服务端 Operation 是否取消由调用方显式决定（控制安全：不擅自中止）。
    if (signal?.aborted) throw new OperationWaitCancelledError()
    const row = await fetchOperation(operationId)
    onProgress?.(row.completed, row.total)
    if (TERMINAL_STATES.includes(row.state)) {
      if (row.state === 'failed' || row.state === 'cancelled') {
        throw new ApiError(
          row.error?.message || `Operation ${row.state}`,
          409,
          row.error?.code || `OPERATION_${row.state.toUpperCase()}`,
        )
      }
      return row
    }
    if (Date.now() >= deadline) throw new ApiError('Operation timed out', 504, 'OPERATION_TIMEOUT')
    await Promise.race([promiseTimeout(OPERATION_POLL_MS), aborted(signal)])
  }
}

function aborted(signal: AbortSignal | undefined): Promise<void> {
  if (!signal) return new Promise(() => {})
  return new Promise((resolve) => {
    if (signal.aborted) return resolve()
    signal.addEventListener('abort', () => resolve(), { once: true })
  })
}
