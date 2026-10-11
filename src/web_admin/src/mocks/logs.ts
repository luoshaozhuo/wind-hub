// 全局 Mock 日志存储：固定 seed（>=100 条，无随机内容），操作（verify /
// command / task / sink / config / quality check）动态追加，容量上限 500，
// 最新在前。与 GET /api/v1/logs 的 LogEntryResponse 形状一致。
import type { components } from '../api/generated/schema'

export type MockLogEntry = components['schemas']['LogEntryResponse']

const LOG_CAP = 500
const SEED_COUNT = 140

let logStore: MockLogEntry[] = []

export function appendLog(level: string, source: string, object: string, message: string): void {
  logStore.unshift({
    timestamp: new Date().toISOString(),
    level,
    source,
    object,
    message,
  })
  if (logStore.length > LOG_CAP) logStore.length = LOG_CAP
}

export interface LogQuery {
  level?: string
  source?: string
  keyword?: string
}

export function queryLogs(filter: LogQuery): MockLogEntry[] {
  let items = logStore
  if (filter.level) items = items.filter((l) => l.level === filter.level)
  if (filter.source) items = items.filter((l) => l.source === filter.source)
  if (filter.keyword) items = items.filter((l) => l.message.includes(filter.keyword!))
  return items
}

// 启动种子日志：确定性历史内容，时间为相对当前时刻的固定偏移。
export function seedLogs(): void {
  const samples: Array<[string, string, string, string]> = [
    ['INFO', 'ads', 'wtg-040', 'connected (AMS route OK)'],
    ['INFO', 'modbus', 'wtg-002', 'connected 192.168.100.102:502'],
    ['WARNING', 'task', 'turbine-ads-all', 'interval overrun 42 ms'],
    ['ERROR', 'modbus', 'wtg-003', 'read timeout after 5000 ms'],
    ['ERROR', 'ads', 'wtg-041', 'disconnected, retrying'],
    ['INFO', 'runtime', 'engine', 'config reloaded (3 tasks updated)'],
    ['WARNING', 'sink', 'file_archive', 'queue usage 82%'],
    ['INFO', 'task', 'pcs-fast', 'started'],
    ['ERROR', 'sink', 'db_main', 'write failed: connection refused'],
    ['INFO', 'ads', 'wtg-041', 'reconnect failed, backoff 8 s'],
    ['INFO', 'task', 'turbine-modbus-all', 'cycle completed · 0 errors'],
    ['WARNING', 'ads', 'wtg-017', 'read latency 412 ms above threshold'],
    ['INFO', 'task', 'turbine-ads-all', 'point batch read completed'],
    ['ERROR', 'sink', 'db_main', 'dropped 6 points after 3 retries'],
  ]
  const entries: MockLogEntry[] = []
  // 最新在前（与 appendLog 的 unshift 一致）
  for (let i = SEED_COUNT - 1; i >= 0; i--) {
    const [level, source, object, message] = samples[i % samples.length]
    entries.push({
      timestamp: new Date(Date.now() - (i * 47 + 11) * 1000).toISOString(),
      level,
      source,
      object,
      message,
    })
  }
  logStore = entries
}

export const LOG_SOURCES = ['ads', 'modbus', 'iec104', 'task', 'sink', 'config', 'runtime']

seedLogs()
