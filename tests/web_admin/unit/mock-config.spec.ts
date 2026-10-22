import { describe, expect, it } from 'vitest'
import {
  CONFIG_FILE_NAMES,
  buildDefinitions,
  devicesSeed,
  seedYamlFiles,
  settingsSeed,
  sinksSeed,
  tasksSeed,
} from '../../../src/web_admin/src/mocks/data'

describe('Mock configuration follows Core canonical schema', () => {
  it('exposes exactly the current eight YAML documents', () => {
    expect(CONFIG_FILE_NAMES).toEqual([
      'system.yaml', 'units.yaml', 'device_models.yaml', 'points.yaml',
      'devices.yaml', 'tasks.yaml', 'sinks.yaml', 'business_points.yaml',
    ])
  })

  it('keeps process listener and per-device credentials out of system.yaml', () => {
    const documents = seedYamlFiles(settingsSeed, devicesSeed, tasksSeed)
    const system = JSON.parse(documents['system.yaml'])
    expect(system).not.toHaveProperty('interfaces')
    expect(system.ads).not.toHaveProperty('username')
    expect(system.ads).not.toHaveProperty('password')
  })

  it('uses stable mapped point IDs and canonical model keys', () => {
    const definitions = buildDefinitions()
    const model = definitions.device_models.modbus_wtg
    expect(model).toHaveProperty('device_type_id', 'turbine')
    expect(model).toHaveProperty('point_table_id', 'modbus_wtg_v1')
    expect(model).not.toHaveProperty('connection_defaults')
    const table = definitions.point_tables.modbus_wtg_v1
    expect(table.points).not.toBeInstanceOf(Array)
    expect(table.points).toHaveProperty('wtg_mb_001')
  })

  it('serializes seed model protocol options without hiding driver defaults', () => {
    const definitions = buildDefinitions()
    const modbus = definitions.device_models.modbus_wtg
    expect(modbus.endpoint_defaults).toEqual({ port: 502 })
    expect(modbus.protocol_options).toMatchObject({
      slave_id: 1,
      mode: 'tcp',
      timeout: 5,
      word_order: 'little_endian',
    })
    expect(modbus.protocol_options).not.toHaveProperty('unit_id')
    expect(definitions.device_models.iec104_wtg.protocol_options).not.toHaveProperty('timeout')
    expect(definitions.device_models.beckhoff_wtg.protocol_options).not.toHaveProperty('target_port')
  })

  it('excludes removed sink backends in both runtime seed and YAML', () => {
    const config = JSON.parse(seedYamlFiles(settingsSeed, devicesSeed, tasksSeed)['sinks.yaml'])
    const names = Object.keys(config.sinks)
    expect(names).toEqual(sinksSeed.map((sink) => sink.name))
    for (const definition of Object.values(config.sinks) as Array<{ type: string }>) {
      expect(['file', 'redis', 'modbus']).toContain(definition.type)
    }
  })

  it('keeps boolean point measurements dimensionless', () => {
    const documents = seedYamlFiles(settingsSeed, devicesSeed, tasksSeed)
    const points = JSON.parse(documents['points.yaml']).point_tables
    for (const table of Object.values(points) as Array<{ points: Record<string, { data_type: string; unit: string }> }>) {
      for (const point of Object.values(table.points)) {
        if (point.data_type === 'bool') expect(point.unit).toBe('none')
      }
    }
  })

  it('retains unit quantity and base-unit links', () => {
    const units = JSON.parse(seedYamlFiles(settingsSeed, devicesSeed, tasksSeed)['units.yaml'])
    for (const [unitId, definition] of Object.entries(units.units) as Array<[string, { quantity: string }]>) {
      expect(units.quantities[definition.quantity].base_unit).toBe(unitId)
    }
  })
})
