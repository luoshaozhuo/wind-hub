// deviceConnection domain 测试：协议默认值、表单映射、覆盖序列化、
// 协议切换、空值/数值规范化与校验。ADS / Modbus / IEC104 三协议全覆盖。
import { describe, expect, it } from 'vitest'

import {
  amsNetIdFromHost,
  applyModelDefaults,
  connectionDefaultsFromForm,
  connectionOverridesFromForm,
  defaultConnectionForm,
  formFromConnection,
  formFromModelDefaults,
  resetFormForProtocol,
  validateConnectionForm,
} from '../../src/domain/deviceConnection'
import type { DeviceModelDef } from '../../src/domain/types'

function model(protocol: DeviceModelDef['protocol'], defaults: Record<string, unknown> = {}) {
  return {
    id: `${protocol}-model`,
    device_type: 'turbine',
    manufacturer: 'generic',
    protocol,
    point_table: 't1',
    read_mode: '',
    properties: {},
    connection_defaults: defaults,
  } satisfies DeviceModelDef
}

describe('defaultConnectionForm', () => {
  it('ADS 默认：port 801 / TwinCAT 2', () => {
    const form = defaultConnectionForm('ads')
    expect(form.port).toBe(801)
    expect(form.twincat_version).toBe('2')
    expect(form.timeout).toBe(3)
  })

  it('Modbus 默认：port 502 / unit 1 / tcp / little_endian', () => {
    const form = defaultConnectionForm('modbus')
    expect(form).toMatchObject({ port: 502, unit_id: 1, mode: 'tcp', word_order: 'little_endian' })
  })

  it('IEC104 默认：port 2404 + 流控参数', () => {
    const form = defaultConnectionForm('iec104')
    expect(form).toMatchObject({
      port: 2404,
      common_addr: 1,
      k: 12,
      w: 8,
      t0: 30,
      t1: 15,
      t2: 10,
      t3: 20,
      max_reconnect_retries: 5,
    })
  })
})

describe('formFromConnection', () => {
  it('空连接对象回落协议默认值', () => {
    expect(formFromConnection('iec104', {})).toEqual(defaultConnectionForm('iec104'))
  })

  it('undefined / null / 空字符串均按缺省处理', () => {
    const form = formFromConnection('modbus', {
      port: undefined,
      unit_id: null,
      mode: '',
      timeout: 'not-a-number',
    })
    expect(form.port).toBe(502)
    expect(form.unit_id).toBe(1)
    expect(form.mode).toBe('tcp')
    expect(form.timeout).toBe(3)
  })

  it('数值规范化：字符串数字转为 number', () => {
    const form = formFromConnection('iec104', { common_addr: '7', t1: '2.5' })
    expect(form.common_addr).toBe(7)
    expect(form.t1).toBe(2.5)
  })

  it('0 是合法值，不被默认值覆盖', () => {
    const form = formFromConnection('modbus', { unit_id: 0, timeout: 0.5 })
    expect(form.unit_id).toBe(0)
    expect(form.timeout).toBe(0.5)
  })
})

describe('formFromModelDefaults / applyModelDefaults', () => {
  it('从 model connection_defaults 填充表单', () => {
    const m = model('modbus', { port: 1502, unit_id: 9, mode: 'rtu' })
    const form = formFromModelDefaults(m)
    expect(form).toMatchObject({ port: 1502, unit_id: 9, mode: 'rtu', word_order: 'little_endian' })
  })

  it('model 切换时应用 defaults；ADS 按 host 推导空缺的 target_net_id', () => {
    const form = defaultConnectionForm('modbus')
    const adsModel = model('ads', { timeout: 8, twincat_version: '3' })
    applyModelDefaults(form, adsModel, '192.168.1.10')
    expect(form.timeout).toBe(8)
    expect(form.twincat_version).toBe('3')
    expect(form.target_net_id).toBe('192.168.1.10.1.1')
  })

  it('ADS 已有 target_net_id 时不覆盖手动值', () => {
    const form = defaultConnectionForm('ads')
    form.target_net_id = '1.2.3.4.5.6'
    applyModelDefaults(form, model('ads'), '10.0.0.1')
    expect(form.target_net_id).toBe('1.2.3.4.5.6')
  })

  it('协议切换：modbus → iec104 应用 iec104 defaults，不残留 modbus 语义', () => {
    const form = defaultConnectionForm('modbus')
    applyModelDefaults(form, model('iec104', { common_addr: 3, k: 6 }))
    expect(form).toMatchObject({ common_addr: 3, k: 6, port: 2404 })
  })

  it('model 无 port 默认时回落协议默认端口', () => {
    const form = defaultConnectionForm('modbus')
    form.port = 1502
    applyModelDefaults(form, model('ads'))
    expect(form.port).toBe(801)
  })
})

describe('resetFormForProtocol', () => {
  it('切换协议只重置 port / TwinCAT 版本，其余保留', () => {
    const form = defaultConnectionForm('modbus')
    form.timeout = 9
    resetFormForProtocol(form, 'ads')
    expect(form.port).toBe(801)
    expect(form.twincat_version).toBe('2')
    expect(form.timeout).toBe(9)
    resetFormForProtocol(form, 'iec104')
    expect(form.port).toBe(2404)
  })
})

const MODBUS_FULL_DEFAULTS = {
  port: 502,
  unit_id: 1,
  mode: 'tcp',
  timeout: 3,
  word_order: 'little_endian',
}

const IEC104_FULL_DEFAULTS = {
  port: 2404,
  common_addr: 1,
  k: 12,
  w: 8,
  t0: 30,
  t1: 15,
  t2: 10,
  t3: 20,
  max_reconnect_retries: 5,
}

describe('connectionOverridesFromForm', () => {
  it('与 model defaults 一致时不产生覆盖', () => {
    const m = model('modbus', MODBUS_FULL_DEFAULTS)
    const { extensions, port } = connectionOverridesFromForm(defaultConnectionForm('modbus'), m)
    expect(extensions).toEqual({})
    expect(port).toBeUndefined()
  })

  it('差异项落 extensions；port 差异单独返回', () => {
    const m = model('modbus', MODBUS_FULL_DEFAULTS)
    const form = defaultConnectionForm('modbus')
    form.unit_id = 5
    form.port = 1502
    form.word_order = 'big_endian'
    const { extensions, port } = connectionOverridesFromForm(form, m)
    expect(extensions).toEqual({ unit_id: 5, word_order: 'big_endian' })
    expect(port).toBe(1502)
  })

  it('defaults 缺失的键与表单值不同即视为覆盖（与原始逐键比较语义一致）', () => {
    const m = model('modbus', { port: 502 })
    const { extensions } = connectionOverridesFromForm(defaultConnectionForm('modbus'), m)
    expect(extensions).toEqual({ unit_id: 1, mode: 'tcp', timeout: 3, word_order: 'little_endian' })
  })

  it('ADS target_net_id 作为身份键始终写入，其余按键集序列化', () => {
    const m = model('ads', { port: 801, timeout: 3, twincat_version: '2' })
    const form = defaultConnectionForm('ads')
    form.target_net_id = ' 10.0.0.1.1.1 '
    form.twincat_version = '3'
    const { extensions } = connectionOverridesFromForm(form, m)
    expect(extensions).toEqual({ target_net_id: '10.0.0.1.1.1', twincat_version: '3' })
  })

  it('IEC104 仅差异流控键落 extensions', () => {
    const m = model('iec104', IEC104_FULL_DEFAULTS)
    const form = defaultConnectionForm('iec104')
    form.common_addr = 2
    form.t3 = 25
    const { extensions } = connectionOverridesFromForm(form, m)
    expect(extensions).toEqual({ common_addr: 2, t3: 25 })
  })

  it('round-trip：overrides 合并回 defaults 后表单等价', () => {
    const m = model('iec104', IEC104_FULL_DEFAULTS)
    const form = defaultConnectionForm('iec104')
    form.common_addr = 9
    form.t1 = 3
    const { extensions, port } = connectionOverridesFromForm(form, m)
    const merged = {
      ...m.connection_defaults,
      ...extensions,
      port: port ?? m.connection_defaults.port,
    }
    expect(formFromConnection('iec104', merged)).toEqual(form)
  })
})

describe('connectionDefaultsFromForm', () => {
  it('ADS 序列化键集（含重连参数，不含 modbus/iec104 键）', () => {
    const form = defaultConnectionForm('ads')
    const defaults = connectionDefaultsFromForm(form, 'ads')
    expect(defaults).toEqual({
      port: 801,
      timeout: 3,
      twincat_version: '2',
      reconnect_max_retries: 5,
      reconnect_backoff_max: 30,
    })
  })

  it('IEC104 序列化键集（不含 timeout / word_order）', () => {
    const form = defaultConnectionForm('iec104')
    const defaults = connectionDefaultsFromForm(form, 'iec104')
    expect(Object.keys(defaults).sort()).toEqual(
      ['common_addr', 'k', 'max_reconnect_retries', 'port', 't0', 't1', 't2', 't3', 'w'].sort(),
    )
  })
})

describe('validateConnectionForm', () => {
  it('ADS 缺少 target_net_id 报错', () => {
    const form = defaultConnectionForm('ads')
    expect(validateConnectionForm(form, 'ads')).toContain('Target AMS Net ID')
    form.target_net_id = amsNetIdFromHost('192.168.1.1')
    expect(validateConnectionForm(form, 'ads')).toBe('')
  })

  it('Modbus / IEC104 无强制字段', () => {
    expect(validateConnectionForm(defaultConnectionForm('modbus'), 'modbus')).toBe('')
    expect(validateConnectionForm(defaultConnectionForm('iec104'), 'iec104')).toBe('')
  })
})

describe('amsNetIdFromHost', () => {
  it('host 追加 .1.1 并去空白', () => {
    expect(amsNetIdFromHost(' 192.168.151.3 ')).toBe('192.168.151.3.1.1')
  })
})
