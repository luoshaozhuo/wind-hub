// queryKeys 测试：集中 key 的稳定性与失效范围。
// 失效粒度依赖 key 结构（如 ['devices', id] 前缀命中 deviceData/trend），
// 这些结构关系是契约，必须被测试钉住。
import { describe, expect, it } from 'vitest'

import { CONFIG_QUERY_KEYS, qk } from '../../src/api/queryKeys'

describe('queryKeys', () => {
  it('deviceTrend 对 point id 排序，与传入顺序无关', () => {
    expect(qk.deviceTrend('d1', ['p2', 'p1'], 3600)).toEqual(
      qk.deviceTrend('d1', ['p1', 'p2'], 3600),
    )
    expect(qk.deviceTrend('d1', ['p1', 'p2'], 3600)).not.toEqual(
      qk.deviceTrend('d1', ['p1', 'p2'], 7200),
    )
  })

  it('设备作用域 key 共享 devices/id 前缀（前缀失效命中全部派生查询）', () => {
    expect(qk.deviceData('d1')).toEqual(['devices', 'd1', 'data'])
    expect(qk.deviceTrend('d1', ['p1'], 60).slice(0, 2)).toEqual(['devices', 'd1'])
    expect(qk.device('d1')).toEqual(['devices', 'd1'])
    // 不同设备互不干扰。
    expect(qk.deviceData('d1')).not.toEqual(qk.deviceData('d2'))
  })

  it('logs key 嵌入查询参数', () => {
    expect(qk.logs({ page: 1, pageSize: 200 })).toEqual(['logs', { page: 1, pageSize: 200 }])
  })

  it('CONFIG_QUERY_KEYS 覆盖快照同步的六个配置部分', () => {
    expect(CONFIG_QUERY_KEYS).toEqual([
      qk.devices,
      qk.tasks,
      qk.sinks,
      qk.definitions,
      qk.settings,
      qk.overview,
    ])
  })
})
