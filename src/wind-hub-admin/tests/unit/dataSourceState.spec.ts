import { describe, expect, it } from 'vitest'
import { dataSourceState, unavailableValue } from '../../src/domain/dataSourceState'

describe('data source availability', () => {
  it('does not mistake pending and missing data for healthy values', () => {
    expect(dataSourceState({ isPending: true, isError: false, hasData: false })).toBe('pending')
    expect(dataSourceState({ isPending: false, isError: false, hasData: false })).toBe('pending')
  })

  it('distinguishes first failure from failure after a successful response', () => {
    expect(dataSourceState({ isPending: false, isError: true, hasData: false })).toBe('unavailable')
    expect(dataSourceState({ isPending: false, isError: true, hasData: true })).toBe('stale')
  })

  it('preserves valid zero values and suppresses invalid cached values', () => {
    expect(unavailableValue('valid', 0)).toBe('0')
    expect(unavailableValue('stale', 0)).toBe('—')
    expect(unavailableValue('unavailable', 'RUNNING')).toBe('—')
    expect(dataSourceState({ isPending: false, isError: false, hasData: true, isInvalid: true })).toBe('invalid')
  })
})
