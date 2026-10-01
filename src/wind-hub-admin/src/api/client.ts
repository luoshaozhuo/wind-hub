const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code = 'HTTP_ERROR',
  ) {
    super(message)
  }
}

async function errorFrom(response: Response): Promise<ApiError> {
  try {
    const body = await response.json()
    const detail = body?.detail
    if (detail && typeof detail === 'object') {
      return new ApiError(String(detail.message || response.statusText), response.status, String(detail.code || 'HTTP_ERROR'))
    }
    return new ApiError(String(detail || response.statusText), response.status)
  } catch {
    return new ApiError(response.statusText || 'Request failed', response.status)
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  const response = await fetch(API_BASE + path, { ...init, headers })
  if (!response.ok) throw await errorFrom(response)
  if (response.status === 204) return undefined as T
  return await response.json() as T
}

export async function apiBlob(path: string): Promise<Blob> {
  const response = await fetch(API_BASE + path)
  if (!response.ok) throw await errorFrom(response)
  return await response.blob()
}

export function jsonBody(value: unknown): string {
  return JSON.stringify(value)
}
