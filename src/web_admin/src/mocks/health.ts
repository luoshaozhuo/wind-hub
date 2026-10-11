// Mock System Health：CPU / Memory / RSS / Disk / 温度随时间确定性变化
// （sin/cos + epoch 哈希，无随机数），历史序列按 range 生成。
import type { components } from '../api/generated/schema'
import { epochSecNow, hash32 } from './values'

type SystemHealthResponse = components['schemas']['SystemHealthResponse']
type Mount = components['schemas']['StorageMountResponse']
type Risk = components['schemas']['HealthRiskResponse']

const RANGE_POINTS: Record<string, { count: number; stepSec: number }> = {
  '1h': { count: 60, stepSec: 60 },
  '24h': { count: 96, stepSec: 900 },
  '7d': { count: 84, stepSec: 7200 },
}

const MEMORY_TOTAL_GB = 32
const DISK_TOTAL_GB = 470
const BOOT_EPOCH = epochSecNow() - 9 * 86400 - 7 * 3600 // 固定推导的启动时间

function cpuHostPct(t: number): number {
  return (
    Math.round((22 + 14 * Math.sin(t / 47) + 6 * Math.sin(t / 13) + (hash32(String(t)) % 5)) * 10) /
    10
  )
}

function cpuProcessPct(t: number): number {
  return Math.round((8 + 5 * Math.sin(t / 31 + 1) + (hash32(`p${t}`) % 3)) * 10) / 10
}

function cpuTempC(t: number): number {
  return Math.round((46 + 6 * Math.sin(t / 97 + 2)) * 10) / 10
}

function memoryHostGb(t: number): number {
  return Math.round((14.2 + 1.8 * Math.sin(t / 211) + 0.4 * Math.sin(t / 29)) * 100) / 100
}

function memoryRssGb(t: number): number {
  return Math.round((1.9 + 0.25 * Math.sin(t / 151 + 3)) * 100) / 100
}

function diskFreeGb(t: number): number {
  // 磁盘随时间缓慢下降（采集归档），确定性
  return Math.round((412 - ((t % 604800) / 604800) * 3.5) * 10) / 10
}

export function systemHealthForRange(range: string): SystemHealthResponse {
  const { count, stepSec } = RANGE_POINTS[range] ?? RANGE_POINTS['1h']
  const now = epochSecNow()
  const times = Array.from({ length: count }, (_, i) => now - (count - 1 - i) * stepSec)

  const cpuHost = times.map(cpuHostPct)
  const cpuProc = times.map(cpuProcessPct)
  const cpuTemp = times.map(cpuTempC)
  const memHost = times.map(memoryHostGb)
  const memRss = times.map(memoryRssGb)
  const diskFree = times.map(diskFreeGb)

  const diskFreeNow = diskFree[diskFree.length - 1]
  const diskUsed = Math.round((DISK_TOTAL_GB - diskFreeNow) * 10) / 10
  const diskPct = Math.round((diskUsed / DISK_TOTAL_GB) * 1000) / 10
  const memUsed = memHost[memHost.length - 1]

  const mounts: Mount[] = [
    {
      mount: '/',
      total_gb: DISK_TOTAL_GB,
      used_gb: diskUsed,
      free_gb: diskFreeNow,
      usage_pct: diskPct,
      growth_24h_gb: 3.5,
      estimated_full_days: Math.round(diskFreeNow / 3.5),
    },
    {
      mount: '/var/lib/wind-hub',
      total_gb: 200,
      used_gb: Math.round((200 - diskFreeNow * 0.3) * 10) / 10,
      free_gb: Math.round(diskFreeNow * 0.3 * 10) / 10,
      usage_pct: Math.round((1 - (diskFreeNow * 0.3) / 200) * 1000) / 10,
      growth_24h_gb: 2.1,
      estimated_full_days: Math.round((diskFreeNow * 0.3) / 2.1),
    },
    {
      mount: '/boot',
      total_gb: 1,
      used_gb: 0.3,
      free_gb: 0.7,
      usage_pct: 30,
      growth_24h_gb: 0,
      estimated_full_days: null,
    },
  ]

  const cpuNow = cpuHost[cpuHost.length - 1]
  const tempNow = cpuTemp[cpuTemp.length - 1]
  const risks: Risk[] = [
    {
      name: 'disk-growth',
      state: diskPct > 85 ? 'critical' : diskPct > 70 ? 'warning' : 'ok',
      summary: `root filesystem ${diskPct}% used`,
      detail: `archive growth ≈3.5 GB/day, estimated full in ${Math.round(diskFreeNow / 3.5)} days`,
    },
    {
      name: 'cpu-temperature',
      state: tempNow > 75 ? 'warning' : 'ok',
      summary: `cpu temperature ${tempNow} °C`,
      detail: 'thermal envelope within limits under current load profile',
    },
    {
      name: 'memory-headroom',
      state: memUsed / MEMORY_TOTAL_GB > 0.85 ? 'warning' : 'ok',
      summary: `host memory ${memUsed}/${MEMORY_TOTAL_GB} GB`,
      detail: 'collector + commander RSS stable, no swap pressure',
    },
  ]

  const load = Math.round((cpuNow / 100) * 8 * 100) / 100
  return {
    range,
    sampled_at: new Date(now * 1000).toISOString(),
    cpu_count: 8,
    uptime_seconds: now - BOOT_EPOCH + (epochSecNow() - now), // 随时间增长
    load_average: [load, Math.round(load * 0.9 * 100) / 100, Math.round(load * 0.8 * 100) / 100],
    current: {
      cpu_host_pct: cpuNow,
      cpu_process_pct: cpuProc[cpuProc.length - 1],
      cpu_temp_c: tempNow,
      memory_used_gb: memUsed,
      memory_total_gb: MEMORY_TOTAL_GB,
      process_rss_gb: memRss[memRss.length - 1],
      disk_free_gb: diskFreeNow,
      disk_total_gb: DISK_TOTAL_GB,
    },
    series: {
      timestamps: times.map((t) => new Date(t * 1000).toISOString()),
      cpu_host_pct: cpuHost,
      cpu_process_pct: cpuProc,
      cpu_temp_c: cpuTemp,
      memory_host_gb: memHost,
      memory_rss_gb: memRss,
      disk_free_gb: diskFree,
    },
    mounts,
    risks,
  }
}
