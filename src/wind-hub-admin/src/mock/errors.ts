// 统一 Mock 错误模型（§26）：所有 Mock Service 操作返回/记录相同结构的错误对象，
// 页面只展示，不自行构造错误码。
import { formatTimestamp } from '../utils/format'

export const MOCK_ERROR_CODES = {
  HOST_UNREACHABLE: 'HOST_UNREACHABLE',
  DEVICE_DISABLED: 'DEVICE_DISABLED',
  ADS_SESSION_UNAVAILABLE: 'ADS_SESSION_UNAVAILABLE',
  PROTOCOL_CONNECTION_FAILED: 'PROTOCOL_CONNECTION_FAILED',
  POINT_READ_FAILED: 'POINT_READ_FAILED',
  READ_TIMEOUT: 'READ_TIMEOUT',
  ADS_SYMBOL_NOT_FOUND: 'ADS_SYMBOL_NOT_FOUND',
  DECODE_ERROR: 'DECODE_ERROR',
  COMMAND_REJECTED: 'COMMAND_REJECTED',
  COMMAND_TIMEOUT: 'COMMAND_TIMEOUT',
  TASK_DEVICE_UNAVAILABLE: 'TASK_DEVICE_UNAVAILABLE',
  SINK_DISABLED: 'SINK_DISABLED',
  SINK_BROKER_TIMEOUT: 'SINK_BROKER_TIMEOUT',
  SINK_CONNECTION_REFUSED: 'SINK_CONNECTION_REFUSED',
  SINK_WRITE_FAILED: 'SINK_WRITE_FAILED',
  CONFIG_VALIDATION_FAILED: 'CONFIG_VALIDATION_FAILED',
} as const

export type MockErrorCode = (typeof MOCK_ERROR_CODES)[keyof typeof MOCK_ERROR_CODES]

export interface MockError {
  code: MockErrorCode | string
  message: string
  stage: string
  target: string
  timestamp: string
  details: string
}

export function mockError(
  code: MockErrorCode | string,
  stage: string,
  target: string,
  message: string,
  details = '',
): MockError {
  return { code, message, stage, target, timestamp: formatTimestamp(new Date()), details }
}
