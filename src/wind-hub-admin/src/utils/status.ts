// 共享状态展示映射：业务状态字符串 → Element Plus tag type。
// 只负责“状态色语义”这一件事；状态文案（label）仍由各页面按业务语境自行决定。
// 覆盖的状态集合来自 mock/types.ts 的真实枚举：
//   DeviceVerifyState / VerifyStepState / SinkRuntimeState / TaskDef.runtime / Quality 页状态文案。
// 进行中的状态（checking/testing/starting/stopping/verifying）返回 undefined，
// 让 el-tag 落回默认 primary 样式，避免传 '' 触发 prop 校验警告。

export type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

const STATUS_TAG: Record<string, TagType> = {
  // 正常 / 健康 / 运行中
  success: 'success',
  healthy: 'success',
  normal: 'success',
  recovered: 'success',
  passed: 'success',
  ok: 'success',
  running: 'success',
  // 告警 / 退化 / 部分成功
  warning: 'warning',
  degraded: 'warning',
  partial: 'warning',
  // 故障 / 错误 / 失效
  fault: 'danger',
  failed: 'danger',
  interrupted: 'danger',
  active: 'danger',
  error: 'danger',
  invalid: 'danger',
  // 中性 / 未启用 / 未验证
  unknown: 'info',
  disabled: 'info',
  stopped: 'info',
  idle: 'info',
  never: 'info',
  unverified: 'info',
  skipped: 'info',
}

/** 状态 → el-tag type；进行中与未收录状态返回 undefined（默认样式），未知状态收敛为 info。 */
export function statusTagType(state: string | null | undefined): TagType | undefined {
  if (!state) return undefined
  const key = state.trim().toLowerCase()
  if (key === 'checking' || key === 'testing' || key === 'starting' || key === 'stopping' || key === 'verifying') {
    return undefined
  }
  return STATUS_TAG[key] ?? 'info'
}
