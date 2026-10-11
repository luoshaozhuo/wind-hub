// Point 连通性测试的纯转换：候选类型清单与原始字节的多类型解码展示。
export const TEST_CANDIDATE_TYPES = [
  'bool',
  'int8',
  'uint8',
  'int16',
  'uint16',
  'int32',
  'uint32',
  'float32',
  'float64',
]

export interface TestCandidate {
  type: string
  value: string
}

export function emptyTestCandidates(): TestCandidate[] {
  return TEST_CANDIDATE_TYPES.map((type) => ({ type, value: '—' }))
}

/** 原始字节候选解码仅在后端协议适配器返回 raw bytes 时启用。 */
export function decodeCandidates(bytes: Uint8Array): TestCandidate[] {
  const view = new DataView(bytes.buffer)
  const format = (v: number) =>
    Number.isFinite(v)
      ? String(Math.abs(v) >= 1e6 ? v.toExponential(6) : Number(v.toFixed(6)))
      : String(v)
  return [
    { type: 'bool', value: bytes[0] ? 'true' : 'false' },
    { type: 'int8', value: String(view.getInt8(0)) },
    { type: 'uint8', value: String(view.getUint8(0)) },
    { type: 'int16', value: String(view.getInt16(0, true)) },
    { type: 'uint16', value: String(view.getUint16(0, true)) },
    { type: 'int32', value: String(view.getInt32(0, true)) },
    { type: 'uint32', value: String(view.getUint32(0, true)) },
    { type: 'float32', value: format(view.getFloat32(0, true)) },
    { type: 'float64', value: format(view.getFloat64(0, true)) },
  ]
}
