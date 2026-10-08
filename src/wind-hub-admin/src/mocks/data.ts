// Mock 种子数据：小而完整、确定性（无随机数），直接使用 generated schema 类型。
// 覆盖 3 台设备（ADS / Modbus / IEC104，一台 online、两台中一台 offline）、
// 2 个 task、3 个 sink，以及最小 logs / quality / system-health / workers。
import type { components } from '../api/generated/schema'

export type SettingsDto = components['schemas']['SettingsResponse']
export type DefinitionsDto = components['schemas']['DefinitionsResponse']
export type DeviceDto = components['schemas']['DeviceResponse']
export type DeviceDataItem = components['schemas']['DeviceDataItemResponse']
export type TaskDto = components['schemas']['TaskResponse']
export type TaskInstanceDto = components['schemas']['TaskInstanceResponse']
export type SinkDto = components['schemas']['SinkResponse']
export type OverviewDto = components['schemas']['OverviewResponse']
export type WorkerDto = components['schemas']['WorkerResponse']
export type QualityDto = components['schemas']['QualityResponse']
export type LogEntryDto = components['schemas']['LogEntryResponse']
export type SystemHealthDto = components['schemas']['SystemHealthResponse']
export type ConfigFileDto = components['schemas']['ConfigFileResponse']
export type ConfigRevisionDto = components['schemas']['ConfigRevisionResponse']

export const MOCK_NOW = '2026-10-08T08:00:00Z'

export const settingsSeed: SettingsDto = {
  site_id: 'mock-site',
  site_name: 'Mock Wind Farm',
  api_enabled: true,
  api_host: '0.0.0.0',
  api_port: 8080,
  ads_local_ip: '127.0.0.1',
  ads_local_ams_net_id: '127.0.0.1.1.1',
  ads_username: 'Administrator',
  ads_password: '',
}

export const definitionsSeed: DefinitionsDto = {
  units: {
    mw: { name: 'Megawatt', symbol: 'MW' },
    mps: { name: 'Meter per second', symbol: 'm/s' },
    celsius: { name: 'Celsius', symbol: '°C' },
  },
  device_types: {
    turbine: { name: 'Wind Turbine' },
    inverter: { name: 'Inverter' },
  },
  device_models: {
    'ads-cx9020': {
      device_type: 'inverter',
      manufacturer: 'Beckhoff',
      model: 'CX9020',
      protocol: 'ads',
      point_table: 'ads-inv',
      properties: {},
      connection_defaults: { port: 48898 },
    },
    'modbus-gw': {
      device_type: 'inverter',
      manufacturer: 'Generic',
      model: 'Modbus TCP',
      protocol: 'modbus',
      point_table: 'modbus-inv',
      properties: {},
      connection_defaults: { port: 502 },
    },
    'iec104-rtu': {
      device_type: 'turbine',
      manufacturer: 'Generic',
      model: 'IEC 60870-5-104',
      protocol: 'iec104',
      point_table: 'iec104-turbine',
      properties: {},
      connection_defaults: { port: 2404 },
    },
  },
  point_tables: {
    'ads-inv': {
      protocol: 'ads',
      points: [
        {
          point_id: 'INV.POWER',
          variable_name: 'Active Power',
          point_groups: ['fast'],
          address: { symbol: 'MAIN.fPower' },
          data_type: 'float',
          scale: 1,
          offset: 0,
          unit: 'mw',
        },
      ],
    },
    'modbus-inv': {
      protocol: 'modbus',
      points: [
        {
          point_id: 'INV.POWER',
          variable_name: 'Active Power',
          point_groups: ['fast'],
          address: { function: 'holding', address: 40001 },
          data_type: 'float',
          scale: 0.001,
          offset: 0,
          unit: 'mw',
        },
      ],
    },
    'iec104-turbine': {
      protocol: 'iec104',
      points: [
        {
          point_id: 'WTG.WIND_SPEED',
          variable_name: 'Wind Speed',
          point_groups: ['slow'],
          address: { ioa: 1001 },
          data_type: 'float',
          scale: 1,
          offset: 0,
          unit: 'mps',
        },
      ],
    },
  },
  device_groups: ['array-a'],
  point_groups: ['fast', 'slow'],
}

export const devicesSeed: DeviceDto[] = [
  {
    device_id: 'inv-ads-01',
    model: 'ads-cx9020',
    device_type: 'inverter',
    device_group: 'array-a',
    host: '192.168.10.11',
    port: 48898,
    protocol: 'ads',
    point_table: 'ads-inv',
    enabled: true,
    connected: true,
    consecutive_failures: 0,
    last_error: null,
    extensions: {},
  },
  {
    device_id: 'inv-mbus-01',
    model: 'modbus-gw',
    device_type: 'inverter',
    device_group: 'array-a',
    host: '192.168.10.12',
    port: 502,
    protocol: 'modbus',
    point_table: 'modbus-inv',
    enabled: true,
    connected: false,
    consecutive_failures: 3,
    last_error: 'connection refused',
    extensions: {},
  },
  {
    device_id: 'wtg-104-01',
    model: 'iec104-rtu',
    device_type: 'turbine',
    device_group: null,
    host: '192.168.10.21',
    port: 2404,
    protocol: 'iec104',
    point_table: 'iec104-turbine',
    enabled: false,
    connected: false,
    consecutive_failures: 0,
    last_error: null,
    extensions: {},
  },
]

export const deviceDataSeed: Record<string, DeviceDataItem[]> = {
  'inv-ads-01': [
    {
      point_id: 'INV.POWER',
      variable_name: 'Active Power',
      point_groups: ['fast'],
      data_type: 'float',
      unit: 'mw',
      unit_symbol: 'MW',
      value: 1.25,
      quality: 'good',
      source: 'ads',
      timestamp: MOCK_NOW,
      description: null,
    },
  ],
  'inv-mbus-01': [
    {
      point_id: 'INV.POWER',
      variable_name: 'Active Power',
      point_groups: ['fast'],
      data_type: 'float',
      unit: 'mw',
      unit_symbol: 'MW',
      value: null,
      quality: 'bad',
      source: 'modbus',
      timestamp: MOCK_NOW,
      description: null,
    },
  ],
  'wtg-104-01': [
    {
      point_id: 'WTG.WIND_SPEED',
      variable_name: 'Wind Speed',
      point_groups: ['slow'],
      data_type: 'float',
      unit: 'mps',
      unit_symbol: 'm/s',
      value: 8.4,
      quality: 'good',
      source: 'iec104',
      timestamp: MOCK_NOW,
      description: null,
    },
  ],
}

export const tasksSeed: TaskDto[] = [
  {
    task_id: 'task-inv-fast',
    device: 'inv-ads-01',
    device_group: null,
    point_group: 'fast',
    interval: 1,
    enabled: true,
    runtime_state: 'running',
    placement_state: 'placed',
    instance_count: 1,
    running_instances: 1,
    failed_instances: 0,
    stopped_instances: 0,
    assigned_worker_id: 'collector-1',
    targets: ['inv-ads-01'],
  },
  {
    task_id: 'task-wtg-slow',
    device: 'wtg-104-01',
    device_group: null,
    point_group: 'slow',
    interval: 10,
    enabled: true,
    runtime_state: 'stopped',
    placement_state: 'unplaced',
    instance_count: 0,
    running_instances: 0,
    failed_instances: 0,
    stopped_instances: 1,
    assigned_worker_id: null,
    targets: ['wtg-104-01'],
  },
]

export const taskInstancesSeed: Record<string, TaskInstanceDto[]> = {
  'task-inv-fast': [
    {
      instance_id: 'task-inv-fast#0',
      task_id: 'task-inv-fast',
      device_id: 'inv-ads-01',
      point_group: 'fast',
      interval: 1,
      state: 'running',
      assigned_worker_id: 'collector-1',
      targets: ['inv-ads-01'],
    },
  ],
  'task-wtg-slow': [],
}

export const sinksSeed: SinkDto[] = [
  {
    name: 'kafka-main',
    type: 'kafka',
    enabled: true,
    healthy: true,
    message: null,
    queue_depth: 0,
    point_count: 2,
    connection: { bootstrap_servers: '192.168.10.30:9092', topic: 'wind.telemetry' },
    points: [{ point_group: 'fast' }],
  },
  {
    name: 'pg-archive',
    type: 'postgresql',
    enabled: true,
    healthy: true,
    message: null,
    queue_depth: 12,
    point_count: 1,
    connection: { dsn: 'postgresql://db.local/wind' },
    points: [{ point_group: 'slow' }],
  },
  {
    name: 'debug-file',
    type: 'file',
    enabled: false,
    healthy: false,
    message: 'disabled',
    queue_depth: 0,
    point_count: 0,
    connection: { path: '/tmp/wind-debug.ndjson' },
    points: [],
  },
]

export const workersSeed: WorkerDto[] = [
  {
    worker_id: 'collector-1',
    role: 'collector',
    endpoint: 'grpc://192.168.10.40:50051',
    state: 'online',
    capabilities: ['collect', 'route'],
    runtime_running: true,
    reported_id: 'collector-1',
    boot_id: 'boot-20261008-01',
    active_revision: 'r42',
    active_config_hash: 'sha256:mock',
    last_seen_at: MOCK_NOW,
    last_probe_at: MOCK_NOW,
    last_error: null,
  },
]

export const logsSeed: LogEntryDto[] = [
  {
    timestamp: '2026-10-08T07:59:50Z',
    level: 'INFO',
    source: 'collector',
    object: 'inv-ads-01',
    message: 'device connected',
  },
  {
    timestamp: '2026-10-08T07:59:55Z',
    level: 'WARNING',
    source: 'collector',
    object: 'inv-mbus-01',
    message: 'connection refused, retrying',
  },
  {
    timestamp: '2026-10-08T08:00:00Z',
    level: 'INFO',
    source: 'commander',
    object: 'task-inv-fast',
    message: 'task instance running',
  },
]

export const logSourcesSeed: string[] = ['collector', 'commander', 'server']

export const qualitySeed: QualityDto = {
  window: '1h',
  sampled_from: '2026-10-08T07:00:00Z',
  sampled_to: MOCK_NOW,
  channel_summary: [
    {
      key: 'acquisition_online',
      label: 'Acquisition Online',
      value: 1,
      status: 'warn',
      hint: '1 of 2 enabled devices connected',
    },
  ],
  data_metrics: [
    {
      key: 'points_collected',
      label: 'Points Collected',
      value: 3600,
      status: 'ok',
      hint: 'last 1h',
    },
    { key: 'points_dropped', label: 'Points Dropped', value: 4, status: 'warn', hint: 'last 1h' },
  ],
  acquisition_channels: [
    {
      object: 'inv-ads-01',
      source: 'ads',
      target: '192.168.10.11:48898',
      protocol: 'ads',
      state: 'online',
      latency_ms: 12,
      reconnects: 0,
      timeouts: 0,
      issue: null,
    },
    {
      object: 'inv-mbus-01',
      source: 'modbus',
      target: '192.168.10.12:502',
      protocol: 'modbus',
      state: 'offline',
      latency_ms: null,
      reconnects: 3,
      timeouts: 2,
      issue: 'connection refused',
    },
  ],
  delivery_channels: [
    {
      object: 'kafka-main',
      source: 'kafka',
      target: '192.168.10.30:9092',
      protocol: 'kafka',
      state: 'healthy',
      latency_ms: 8,
      reconnects: 0,
      timeouts: 0,
      issue: null,
    },
  ],
  dimensions: [
    {
      dimension: 'acquisition',
      key: 'inv-mbus-01',
      metric: 'connectivity',
      status: 'fail',
      detail: 'device offline',
    },
  ],
  issues: [
    {
      dimension: 'acquisition',
      kind: 'connectivity',
      level: 'warning',
      object: 'inv-mbus-01',
      issue: 'connection refused',
      error: 'connection refused',
      duration_seconds: 300,
    },
  ],
  events: [
    {
      timestamp: '2026-10-08T07:59:55Z',
      object: 'inv-mbus-01',
      event: 'disconnect',
      state: 'offline',
      evidence: 'connection refused',
    },
  ],
}

export const systemHealthSeed: SystemHealthDto = {
  range: '1h',
  sampled_at: MOCK_NOW,
  cpu_count: 8,
  load_average: [0.42, 0.38, 0.35],
  uptime_seconds: 86400,
  current: {
    cpu_host_pct: 12.5,
    cpu_process_pct: 3.1,
    memory_rss_gb: 0.8,
    memory_host_gb: 16,
    disk_free_gb: 120.5,
    cpu_temp_c: null,
  },
  mounts: [
    {
      mount: '/',
      total_gb: 256,
      used_gb: 135.5,
      free_gb: 120.5,
      usage_pct: 52.9,
      growth_24h_gb: 0.4,
      estimated_full_days: 300,
    },
  ],
  risks: [],
  series: {
    timestamps: ['2026-10-08T07:00:00Z', '2026-10-08T07:30:00Z', MOCK_NOW],
    cpu_host_pct: [10.2, 11.8, 12.5],
    cpu_process_pct: [2.8, 3.0, 3.1],
    cpu_temp_c: [null, null, null],
    memory_rss_gb: [0.7, 0.75, 0.8],
    memory_host_gb: [16, 16, 16],
    disk_free_gb: [121.3, 120.9, 120.5],
  },
}

export const configFilesSeed: ConfigFileDto[] = [
  { name: 'settings.yaml', exists: true, optional: false },
  { name: 'definitions.yaml', exists: true, optional: false },
  { name: 'admin-state.yaml', exists: true, optional: false },
]

export const configContentsSeed: Record<string, string> = {
  'settings.yaml':
    'site_id: mock-site\nsite_name: Mock Wind Farm\napi:\n  enabled: true\n  host: 0.0.0.0\n  port: 8080\n',
  'definitions.yaml': 'units:\n  mw: {name: Megawatt, symbol: MW}\n',
  'admin-state.yaml': 'devices:\n  - device_id: inv-ads-01\n    model: ads-cx9020\n',
}

export const configHistorySeed: ConfigRevisionDto[] = [
  {
    revision: 2,
    created_at: '2026-10-08T07:00:00Z',
    source: 'apply',
    comment: 'enable kafka-main sink',
  },
  {
    revision: 1,
    created_at: '2026-10-07T08:00:00Z',
    source: 'import',
    comment: 'initial import',
  },
]
