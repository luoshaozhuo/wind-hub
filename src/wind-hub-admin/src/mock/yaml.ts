// Config 页 YAML Editor 的 mock 文件内容 —— 文件名与结构对应
// configs/template 下当前真实的 7 个配置文件（内容为其精简版）。
import { reactive } from 'vue'

export const CONFIG_FILES = [
  'system.yaml',
  'units.yaml',
  'device_models.yaml',
  'points.yaml',
  'devices.yaml',
  'tasks.yaml',
  'reporting.yaml',
]

export const yamlFiles = reactive<Record<string, string>>({
  'system.yaml': `site:
  site_id: wind_farm_a
  name: 示例风场

runtime:
  queue_maxsize: 1000
  backpressure_policy: drop_old
  shutdown_timeout: 30.0
  connect_timeout: 10.0
  read_timeout: 5.0
  write_timeout: 5.0

ads:
  local_ams_net_id: "192.168.151.244.1.2"
  local_ip: "192.168.151.244"
  username: "Administrator"
  password: ""

sinks:
  - name: kafka_main
    type: kafka
    enabled: false
    params:
      bootstrap_servers: "localhost:9092"
      topic: wind-hub.raw
  - name: db_main
    type: db
    enabled: false
    params:
      dsn: "postgresql://windhub:windhub@localhost:5432/windhub"
      table: points
  - name: file_archive
    type: file
    enabled: true
    params:
      path: /var/tmp/wind-hub/archive.jsonl
      format: jsonl

interfaces:
  api:
    enabled: true
    host: "127.0.0.1"
    port: 8080
  cli:
    enabled: true`,

  'units.yaml': `units:
  none:
    symbol: ""
    name: Dimensionless
  percent:
    symbol: "%"
    name: Percent
  kilowatt:
    symbol: kW
    name: Kilowatt
  hertz:
    symbol: Hz
    name: Hertz
  rpm:
    symbol: rpm
    name: Revolutions per minute
  meter_per_second:
    symbol: m/s
    name: Meter per second
  celsius:
    symbol: °C
    name: Degree Celsius`,

  'device_models.yaml': `device_types:
  turbine:
    name: 风力发电机组
  pcs:
    name: 储能变流器

device_models:
  modbus_wtg:
    device_type: turbine
    protocol: modbus
    point_table: modbus_wtg_v1
    connection_defaults:
      port: 502
      unit_id: 1
      word_order: little_endian

  beckhoff_wtg:
    device_type: turbine
    manufacturer: Beckhoff
    protocol: ads
    point_table: beckhoff_wtg_v1
    read_mode: sum
    connection_defaults:
      port: 48898
      twincat_version: "2"

  pcs_modbus_a:
    device_type: pcs
    protocol: modbus
    point_table: pcs_modbus_v1
    connection_defaults:
      port: 502
      unit_id: 1`,

  'points.yaml': `point_tables:
  modbus_wtg_v1:
    protocol: modbus
    points:
      - point_id: active_power
        variable_name: active_power
        point_groups: [all]
        address:
          type: input
          address: 178
        data_type: int32
        scale: 0.001
        offset: 0.0
        unit: kilowatt
        description: "有功功率"

  beckhoff_base_v1:
    protocol: ads
    points:
      - point_id: rotor_speed
        variable_name: rotor_speed
        point_groups: [all]
        address:
          symbol: MAIN.rotorSpeed
        data_type: float32
        unit: rpm
        description: "风轮转速"

  beckhoff_wtg_v1:
    extends: beckhoff_base_v1
    points:
      - point_id: gen_power
        address:
          symbol: MAIN.power.genActivePower`,

  'devices.yaml': `devices:
  - device_id: wtg-001
    model: modbus_wtg
    device_group: turbine_modbus
    enabled: true
    endpoint:
      host: "192.168.100.101"

  - device_id: wtg-025
    model: beckhoff_wtg
    device_group: turbine_ads
    enabled: true
    endpoint:
      host: "192.168.151.25"
      extensions:
        target_net_id: "192.168.151.25.1.1"`,

  'tasks.yaml': `tasks:
  - task_id: turbine-modbus-all
    device_group: turbine_modbus
    point_group: all
    interval: 1.0
    targets:
      - sink: file_archive
    enabled: true

  - task_id: turbine-ads-all
    device_group: turbine_ads
    point_group: all
    interval: 1.0
    targets:
      - sink: file_archive
    enabled: true`,

  'reporting.yaml': `reporting: []
batch_size: 50
common_address: 1
host: "127.0.0.1"
port: 12404`,
})

export function updateMockSiteYaml(siteId: string, siteName: string) {
  const source = yamlFiles['system.yaml']
  const next = source
    .replace(/(^site:\s*\n\s*site_id:\s*).+$/m, `$1${siteId}`)
    .replace(/(^site:\s*\n\s*site_id:.*\n\s*name:\s*).+$/m, `$1${siteName}`)

  yamlFiles['system.yaml'] = next
}

// 双引号 YAML 标量的最小转义，避免用户输入破坏 mock 文档结构。
function yamlQuote(value: string) {
  return `"${value.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

// ads 段结构对齐正式 schema：Global ADS 直接保存 username/password。
export function updateMockAdsYaml(settings: {
  local_ip: string
  local_ams_net_id: string
  username: string
  password: string
}) {
  const source = yamlFiles['system.yaml']
  const block = `ads:\n  local_ams_net_id: ${yamlQuote(settings.local_ams_net_id)}\n  local_ip: ${yamlQuote(settings.local_ip)}\n  username: ${yamlQuote(settings.username)}\n  password: ${yamlQuote(settings.password)}`
  yamlFiles['system.yaml'] = source.replace(/ads:\n[\s\S]*?(?=\n\nsinks:)/m, block)
}

// interfaces.api 段同步（正式 schema InterfaceConfig.api 含 host/port）
export function updateMockApiYaml(host: string, port: number) {
  const source = yamlFiles['system.yaml']
  const block = `api:
    enabled: true
    host: ${yamlQuote(host)}
    port: ${port}`
  yamlFiles['system.yaml'] = source.replace(/api:\n\s*enabled:[^\n]*\n\s*host:[^\n]*\n\s*port:[^\n]*/m, block)
}

// Import diff 不再使用静态样例：ConfigPage 用 buildReview 对真实文本生成 diff（§21）。
