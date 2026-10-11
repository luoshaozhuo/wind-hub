import { afterEach, describe, expect, it, vi } from 'vitest'
import { runtimeApi, RuntimeApiError } from '../../../src/web_admin/src/api/runtime'

afterEach(() => vi.unstubAllGlobals())

describe('new runtime REST contract', () => {
  it('reads Collector task list without legacy pagination', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ items: [{ task_id: 't1' }] }),
    })
    vi.stubGlobal('fetch', fetchMock)
    expect(await runtimeApi.tasks()).toEqual([{ task_id: 't1' }])
    expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/collector/tasks')
  })

  it('passes credentials only for mutating actions', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ task_id: 't1' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await runtimeApi.startTask('t 1', 'secret')
    expect(fetchMock.mock.calls[0][0]).toContain('/tasks/t%201/start')
    expect(fetchMock.mock.calls[0][1]).toEqual({
      method: 'POST', headers: { 'X-API-Key': 'secret' },
    })
  })

  it('preserves upstream RPC error status', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 503, statusText: 'Unavailable',
      json: async () => ({ detail: { code: 'UNAVAILABLE', message: 'collector offline' } }),
    }))
    await expect(runtimeApi.sinks()).rejects.toMatchObject({
      status: 503, code: 'UNAVAILABLE', message: 'collector offline',
    } satisfies Partial<RuntimeApiError>)
  })

  it('encodes device identity in latest-values query', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ values: [] }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await runtimeApi.latestValues('device/a')
    expect(fetchMock.mock.calls[0][0]).toContain('?device_id=device%2Fa')
  })
})
