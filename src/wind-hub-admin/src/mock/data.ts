export const devices = [
  { id: 'wtg-002', protocol: 'modbus', host: '192.168.100.102', status: 'online', latency: 42 },
  { id: 'wtg-003', protocol: 'modbus', host: '192.168.100.103', status: 'online', latency: 51 },
  { id: 'wtg-040', protocol: 'ads', host: '192.168.151.40', status: 'online', latency: 28 },
  { id: 'wtg-041', protocol: 'ads', host: '192.168.151.41', status: 'offline', latency: 0 },
]

export const tasks = [
  { id: 'turbine-fast', target: 'turbine_modbus', interval: 1, status: 'running', successRate: 99.93 },
  { id: 'turbine-ads', target: 'turbine_ads', interval: 1, status: 'running', successRate: 99.71 },
  { id: 'slow-metrics', target: 'all', interval: 10, status: 'stopped', successRate: 100 },
]

export const trend = Array.from({ length: 30 }, (_, i) => ({
  time: `${String(i).padStart(2, '0')}s`,
  power: 1780 + Math.round(Math.sin(i / 4) * 120),
  wind: Number((8.3 + Math.cos(i / 5) * 0.7).toFixed(2)),
}))
