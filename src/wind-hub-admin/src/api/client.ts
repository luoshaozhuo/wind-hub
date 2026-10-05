// HTTP 访问层：openapi-fetch + OpenAPI 生成契约（api/generated/schema.d.ts）。
// Wire DTO 一律来自生成契约，禁止手写 DeviceDto/TaskDto 等接口。
// 错误统一归一化为 ApiError（保留既有 status/code/details 语义）：
// 后端错误包络为 {"error": {"code", "message", "details"}}（见 webapi/errors.py）。
import createClient from 'openapi-fetch'
import type { paths } from './generated/schema'

// 生成路径已含 /api/v1 前缀；VITE_API_BASE 允许指向其他主机（可含 /api/v1 后缀，
// 这里剥掉以避免重复）。
const API_BASE = (import.meta.env.VITE_API_BASE || '')
  .replace(/\/api\/v1\/?$/, '')
  .replace(/\/$/, '')

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code = 'HTTP_ERROR',
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown>
  }
  detail?: unknown
}

/** 把任意捕获值归一化为 ApiError；已是 ApiError 时原样返回。 */
export function normalizeApiError(error: unknown, status = 0): ApiError {
  if (error instanceof ApiError) return error
  if (error && typeof error === 'object') {
    const body = error as ErrorEnvelope
    const envelope = body.error
    if (envelope && typeof envelope === 'object') {
      return new ApiError(
        String(envelope.message || 'Request failed'),
        status,
        String(envelope.code || 'HTTP_ERROR'),
        envelope.details && typeof envelope.details === 'object' ? envelope.details : {},
      )
    }
    // FastAPI 默认 detail 兜底（非统一包络的框架级错误）。
    const detail = body.detail
    if (detail && typeof detail === 'object') {
      const value = detail as Record<string, unknown>
      return new ApiError(
        String(value.message || 'Request failed'),
        status,
        String(value.code || 'HTTP_ERROR'),
        value.details && typeof value.details === 'object'
          ? (value.details as Record<string, unknown>)
          : {},
      )
    }
    if (detail !== undefined) return new ApiError(String(detail), status)
  }
  if (error instanceof Error) return new ApiError(error.message, status, 'NETWORK_ERROR')
  return new ApiError(String(error || 'Request failed'), status)
}

export const client = createClient<paths>({
  baseUrl: API_BASE,
  // openapi-fetch 默认在抛出前保留 fetch TypeError；统一在 call() 归一化。
})

// openapi-fetch 返回 { data?, error?, response }（非判别联合）；data 缺失即错误路径。
interface FetchResult<T> {
  data?: T | undefined
  error?: unknown
  response: Response
}

/** 执行一次 openapi-fetch 调用：错误包络/网络异常统一抛 ApiError。 */
export async function call<T>(result: Promise<FetchResult<T>>): Promise<T> {
  let resolved: FetchResult<T>
  try {
    resolved = await result
  } catch (error) {
    // fetch 网络层失败（连接拒绝、DNS、CORS）：HTTP 状态不可用，记 0。
    throw normalizeApiError(error, 0)
  }
  if (resolved.error !== undefined || resolved.data === undefined) {
    throw normalizeApiError(resolved.error, resolved.response.status)
  }
  return resolved.data
}

/** 二进制下载（配置备份等非 JSON 响应）。 */
export async function downloadBlob(path: string): Promise<Blob> {
  let response: Response
  try {
    response = await fetch(API_BASE + path)
  } catch (error) {
    throw normalizeApiError(error, 0)
  }
  if (!response.ok) {
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      // 非 JSON 上游/代理错误仍保留 HTTP 状态。
    }
    throw normalizeApiError(body ?? response.statusText, response.status)
  }
  return await response.blob()
}
