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
  it('calls current Commander diagnostics endpoints and encodes path parameters', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ ok: true }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await runtimeApi.verifyPoint('dev/1', 'pt #1')
    await runtimeApi.resolvePoint('dev/1', 'pt #1')
    await runtimeApi.verifyPoints('dev/1', 'group/a')
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
      '/api/v1/commander/devices/dev%2F1/points/pt%20%231/verify',
      '/api/v1/commander/devices/dev%2F1/points/pt%20%231/resolve',
      '/api/v1/commander/devices/dev%2F1/verify-points?point_group=group%2Fa',
    ])
  })

  it('performs read-many without a control credential', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ values: [] }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await runtimeApi.readMany('device/2', ['point-1', 'point-2'])
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/commander/devices/device%2F2/read-many',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ point_ids: ['point-1', 'point-2'] }),
      },
    )
  })

  it('uses the current Collector and Commander status endpoints', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ status: 'running' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await runtimeApi.collectorStatus()
    await runtimeApi.commanderStatus()
    await runtimeApi.collectorMetrics()
    await runtimeApi.collectorInfo()
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
      '/api/v1/collector/status',
      '/api/v1/commander/status',
      '/api/v1/collector/metrics',
      '/api/v1/collector/info',
    ])
  })

  it('queries Collector metadata without control credentials', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ device_id: 'd/1', points: [] }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const result = await runtimeApi.pointMetadata('d/1', ['p1', 'p/2'])
    expect(result.device_id).toBe('d/1')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/collector/points/metadata?device_id=d%2F1',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ point_ids: ['p1', 'p/2'] }),
      },
    )
  })

  it('controls Collector instances with encoded IDs and a write credential', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ instance_id: 'i/1', state: 'running' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    await runtimeApi.startInstance('i/1', 'secret')
    await runtimeApi.stopInstance('i/1', 'secret')
    expect(fetchMock.mock.calls).toEqual([
      ['/api/v1/collector/instances/i%2F1/start', {
        method: 'POST', headers: { 'X-API-Key': 'secret' },
      }],
      ['/api/v1/collector/instances/i%2F1/stop', {
        method: 'POST', headers: { 'X-API-Key': 'secret' },
      }],
    ])
  })

})
