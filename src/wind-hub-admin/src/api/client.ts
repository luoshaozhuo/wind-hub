const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')

interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown>
  }
  detail?: unknown
}

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

async function errorFrom(response: Response): Promise<ApiError> {
  let body: ErrorEnvelope | null = null
  try {
    body = await response.json() as ErrorEnvelope
  } catch {
    // Non-JSON upstream/proxy errors still retain HTTP status below.
  }

  const error = body?.error
  if (error && typeof error === 'object') {
    return new ApiError(
      String(error.message || response.statusText || 'Request failed'),
      response.status,
      String(error.code || 'HTTP_ERROR'),
      error.details && typeof error.details === 'object' ? error.details : {},
    )
  }

  // Legacy FastAPI detail fallback while old root routes still coexist.
  const detail = body?.detail
  if (detail && typeof detail === 'object') {
    const value = detail as Record<string, unknown>
    return new ApiError(
      String(value.message || response.statusText || 'Request failed'),
      response.status,
      String(value.code || 'HTTP_ERROR'),
      value.details && typeof value.details === 'object'
        ? value.details as Record<string, unknown>
        : {},
    )
  }
  return new ApiError(
    String(detail || response.statusText || 'Request failed'),
    response.status,
  )
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  try {
    return await fetch(API_BASE + path, { ...init, headers })
  } catch (error) {
    throw new ApiError(
      error instanceof Error ? error.message : 'Network request failed',
      0,
      'NETWORK_ERROR',
    )
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await request(path, init)
  if (!response.ok) throw await errorFrom(response)
  if (response.status === 204) return undefined as T
  return await response.json() as T
}

export async function apiBlob(path: string): Promise<Blob> {
  const response = await request(path)
  if (!response.ok) throw await errorFrom(response)
  return await response.blob()
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value)
}
