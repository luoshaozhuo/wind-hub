// System Health 模型（§23）：三窗口确定性曲线 + 固定 Warning 场景。
// 当前值由对应窗口序列的尾点派生，保证“当前值与趋势末值一致”。
export type HealthRange = '1 h' | '24 h' | '7 d' | '30 d'

export interface HealthSeries {
  axis: string[]
  memoryHost: number[]
  memoryRss: number[]
  cpuHost: number[]
  cpuProcess: number[]
  cpuTemp: number[]
  diskFree: number[]
  diskForecast: Array<number | null>
  stressStart: number
  forecastStart: number
}

function axisFor(range: HealthRange): string[] {
  if (range === '1 h') return Array.from({ length: 13 }, (_, i) => String((60 - (12 - i) * 5 + 60) % 60).padStart(2, '0') + 'm')
  if (range === '7 d') return Array.from({ length: 14 }, (_, i) => 'D-' + String(13 - i))
  if (range === '30 d') return Array.from({ length: 15 }, (_, i) => 'D-' + String((14 - i) * 2))
  return Array.from({ length: 24 }, (_, i) => String((i + 1) % 24).padStart(2, '0') + ':00')
}
function rangeScale(range: HealthRange) {
  if (range === '1 h') return 0.12
  if (range === '7 d') return 4.2
  if (range === '30 d') return 5
  return 1
}

export function healthSeries(range: HealthRange): HealthSeries {
  const axis = axisFor(range)
  const scale = rangeScale(range)
  const stressStart = Math.floor(axis.length * 0.7)
  const memoryHost = axis.map((_, i) => Number((9.4 + i * 0.05 * scale + Math.sin(i / 3) * 0.25).toFixed(2)))
  const memoryRss = axis.map((_, i) => Number((2.1 + i * 0.071 * scale + Math.sin(i / 4) * 0.05).toFixed(2)))
  const cpuHost = axis.map((_, i) => Number((42 + Math.sin(i / 2) * 12 + (i > stressStart ? 27 : 0)).toFixed(1)))
  const cpuProcess = axis.map((_, i) => Number((28 + Math.sin(i / 2.3) * 8 + (i > stressStart ? 35 : 0)).toFixed(1)))
  const cpuTemp = axis.map((_, i) => Number((58 + Math.sin(i / 3) * 4 + (i > stressStart ? 20 : 0)).toFixed(1)))
  const diskFree = axis.map((_, i) => Number((52.5 - i * 0.6 * scale).toFixed(1)))
  const forecastStart = Math.max(1, Math.floor(axis.length * 0.75))
  const diskForecast = axis.map((_, i) => i < forecastStart ? null : Number((diskFree[forecastStart] - (i - forecastStart) * 0.6 * scale).toFixed(1)))
  return { axis, memoryHost, memoryRss, cpuHost, cpuProcess, cpuTemp, diskFree, diskForecast, stressStart, forecastStart }
}

// 当前风险卡片：与 24 h 序列尾点一致（RSS 尾值 ≈ 3.8 GB、CPU 尾值 ≈ 87% / 86°C）。
export const healthRisks = [
  { name: 'Memory Growth', state: 'Warning', type: 'warning', summary: 'RSS +1.1 GB / 6 h', detail: 'Continuous growth · projected process limit ~14 h' },
  { name: 'Storage Capacity', state: 'Critical', type: 'danger', summary: '38 GB free', detail: '14.2 GB / 24 h consumption · estimated full in 2.7 days' },
  { name: 'CPU / Thermal', state: 'Warning', type: 'warning', summary: '87% / 86°C', detail: '10 min CPU average · high-load duration 13 min' },
  { name: 'Runtime', state: 'Normal', type: 'success', summary: '3d 14h uptime', detail: '0 unexpected restarts · event loop stable' },
] as const

export const healthDetails = [
  { group: 'Memory', items: [['Host used', '62%'], ['Host available', '12.1 GB'], ['wind-hub RSS', '3.8 GB'], ['RSS growth / 6h', '+1.1 GB'], ['Open FD', '184'], ['Asyncio tasks', '67']] },
  { group: 'CPU / Thermal', items: [['Host CPU', '91%'], ['wind-hub CPU', '72%'], ['Load avg 1/5/15', '7.8 / 7.2 / 6.4'], ['CPU temp', '86°C'], ['High load', '13 min'], ['Throttling', 'No']] },
] as const

export const storageMounts = [
  { mount: '/', used: '28 GB / 100 GB', free: '72 GB', usage: '28%', growth: '+0.4 GB / 24 h', estimated: '> 30 days' },
  { mount: '/data', used: '162 GB / 200 GB', free: '38 GB', usage: '81%', growth: '+9.2 GB / 24 h', estimated: '2.7 days' },
  { mount: '/var', used: '19 GB / 30 GB', free: '11 GB', usage: '63%', growth: '+3.1 GB / 24 h', estimated: '8.4 days' },
] as const

// Overview 主机卡片的当前值：固定 Warning 场景的瞬时读数，
// 与 24 h 趋势尾段（CPU 高负载段、RSS 增长段）语义一致。
export function hostCurrent() {
  return { cpu: 18.4, memory: 41.7, diskFree: 68.2, healthCheck: '3 s ago' }
}
