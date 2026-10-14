// 集中管理的稳定 query key：所有 useQuery / invalidateQueries 只能从这里取 key。
export const qk = {
  overview: ['overview'] as const,
  workers: ['workers'] as const,
  worker: (id: string) => ['workers', id] as const,
  devices: ['devices'] as const,
  device: (id: string) => ['devices', id] as const,
  deviceData: (id: string) => ['devices', id, 'data'] as const,
  deviceTrend: (id: string, pointIds: string[], windowSeconds: number) =>
    ['devices', id, 'trend', [...pointIds].sort(), windowSeconds] as const,
  tasks: ['tasks'] as const,
  task: (id: string) => ['tasks', id] as const,
  taskInstances: (id: string) => ['tasks', id, 'instances'] as const,
  sinks: ['sinks'] as const,
  sink: (name: string) => ['sinks', name] as const,
  definitions: ['definitions'] as const,
  settings: ['settings'] as const,
  operations: (id: string) => ['operations', id] as const,
  quality: (window: string) => ['quality', window] as const,
  logs: (params: Record<string, string | number>) => ['logs', params] as const,
  logSources: ['logs', 'sources'] as const,
  systemHealth: (range: string) => ['system-health', range] as const,
  configFiles: ['config', 'files'] as const,
  configFile: (name: string) => ['config', 'files', name] as const,
  configContents: ['config', 'contents'] as const,
  configHistory: ['config', 'history'] as const,
}

/** 配置类查询：admin-state 保存 / config apply 后统一失效。 */
export const CONFIG_QUERY_KEYS = [
  qk.devices,
  qk.tasks,
  qk.sinks,
  qk.definitions,
  qk.settings,
  qk.overview,
] as const
