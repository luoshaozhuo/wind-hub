import { describe, expect, it } from 'vitest'
import { buildDefinitionsPayload, pointProjection, type DefinitionsEditState } from '../../../src/web_admin/src/api/config'
import { connectionDefaultsFromForm, defaultConnectionForm } from '../../../src/web_admin/src/domain/deviceConnection'

const state = {
  units: { kW: { symbol: 'kW', name: 'Power' } },
  deviceTypes: [{ id: 'turbine', name: 'Turbine' }],
  deviceModels: [{
    id: 'm', device_type: 'turbine', manufacturer: 'Vendor', model: 'M',
    protocol: 'modbus', point_table: 'p',
    properties: { location: 'site-a' }, connection_defaults: { port: 502, timeout: 4, slave_id: 2 },
    protocol_options: { health_point_id: 'ALIVE' },
  }],
  pointTables: [{
    id: 'p', protocol: 'modbus', extends: '', remove_points: [], system: false,
  }],
  points: { p: [{
    point_id: 'P', variable_name: 'power', point_groups: ['all'],
    address: { register_type: 'holding', address: 1 }, data_type: 'float32',
    scale: 1, offset: 0, unit: 'kW', description: '',
  }] },
} as unknown as DefinitionsEditState

describe('Core definitions payload', () => {
  it('uses the canonical model IDs and keyed point table structure', () => {
    const result = buildDefinitionsPayload(state, {}, '')
    expect(result.device_models.m).toMatchObject({
      device_type_id: 'turbine',
      point_table_id: 'p',
      endpoint_defaults: { port: 502 },
      protocol_options: { health_point_id: 'ALIVE', timeout: 4, slave_id: 2 },
      properties: { location: 'site-a' },
    })
    expect(result.point_tables.p).toMatchObject({
      protocol: 'modbus',
      points: {
        P: {
          business_point_id: 'power',
          unit: 'kW',
          point_groups: ['all'],
        },
      },
    })
    expect(result.point_tables.p).not.toHaveProperty('extends')
  })

  it('三协议型号参数与 Core endpoint_defaults 契约一致', () => {
    for (const protocol of ['ads', 'modbus', 'iec104'] as const) {
      const snapshot = {
        ...state,
        deviceModels: [{
          ...state.deviceModels[0],
          protocol,
          connection_defaults: connectionDefaultsFromForm(defaultConnectionForm(protocol), protocol),
          protocol_options: {},
        }],
      } as unknown as DefinitionsEditState
      const serialized = buildDefinitionsPayload(snapshot, {}, '')
      const model = serialized.device_models.m
      expect(model.endpoint_defaults).toEqual({ port: defaultConnectionForm(protocol).port })
      expect(model.protocol_options).not.toHaveProperty('port')
      expect(model.protocol_options).not.toHaveProperty('reconnect_max_retries')
      if (protocol === 'modbus') {
        expect(model.protocol_options).toHaveProperty('slave_id', 1)
        expect(model.protocol_options).not.toHaveProperty('unit_id')
      }
    }
  })

  it('preserves the server point table document on an unchanged projection', () => {
    const raw = { p: { protocol: 'modbus', points: { P: { business_point_id: 'power' } } } }
    const result = buildDefinitionsPayload(state, raw, pointProjection(state))
    expect(result.point_tables).toEqual(raw)
    expect(result.point_tables).not.toBe(raw)
  })
})
