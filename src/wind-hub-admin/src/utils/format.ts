// 共享展示格式化：全站统一的时间戳格式与空值占位。
// 时间格式固定为 `YYYY-MM-DD HH:mm:ss`（本地时区），不随浏览器 locale 变化。
// 空值统一用 '—'；'Never' 仅用于表达“从未执行过”。

export const EMPTY = '—'

const pad = (n: number) => String(n).padStart(2, '0')

/** 格式化为 `YYYY-MM-DD HH:mm:ss`。 */
export function formatTimestamp(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 当前时间的统一展示格式。 */
export function nowText(): string {
  return formatTimestamp(new Date())
}

/** 空值兜底：null/undefined/空字符串统一显示为 '—'。 */
export function orEmpty(v: string | number | null | undefined): string | number {
  return v === null || v === undefined || v === '' ? EMPTY : v
}
