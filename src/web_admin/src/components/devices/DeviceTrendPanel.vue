<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { runtimeApi, type LatestPointValue } from '../../api/runtime'

const props = defineProps<{ deviceId: string; active: boolean }>()
type Sample = { time: number; value: number }
const selected = ref('')
const points = ref<string[]>([])
const history = ref<Sample[]>([])
const error = ref('')
const duration = ref(60)
let timer: ReturnType<typeof setInterval> | undefined
let busy = false
let generation = 0

const plot = computed(() => {
  if (!history.value.length) return ''
  const values = history.value.map(item => item.value)
  const low = Math.min(...values)
  const high = Math.max(...values)
  const span = high === low ? 1 : high - low
  const first = history.value[0].time
  const last = history.value[history.value.length - 1].time
  const elapsed = Math.max(1, last - first)
  return history.value.map((item, index) => {
    const x = 8 + ((item.time - first) / elapsed) * 784
    const y = 192 - ((item.value - low) / span) * 176
    return `${index ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`
  }).join(' ')
})
const range = computed(() => {
  const values = history.value.map(item => item.value)
  return values.length ? `${Math.min(...values)} ～ ${Math.max(...values)}` : '—'
})
function reset() { history.value = []; error.value = ''; generation += 1 }
function decode(value: LatestPointValue): number | null {
  const raw: unknown = value.value
  if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null
  if (raw && typeof raw === 'object') {
    const wrapped = raw as Record<string, unknown>
    for (const key of ['double_value', 'sint64_value', 'uint64_value']) {
      const scalar = wrapped[key]
      if (typeof scalar === 'number' && Number.isFinite(scalar)) return scalar
      if (typeof scalar === 'string' && /^-?\d+$/.test(scalar)) {
        const integer = Number(scalar)
        if (Number.isSafeInteger(integer)) return integer
      }
    }
  }
  return null
}
async function poll() {
  if (!props.active || busy) return
  const current = generation
  busy = true
  try {
    const values = await runtimeApi.latestValues(props.deviceId)
    if (!props.active || current !== generation) return
    points.value = [...new Set(values.map(v => v.point_id))].sort()
    if (!selected.value || !points.value.includes(selected.value)) {
      selected.value = points.value[0] || ''
    }
    const item = values.find(v => v.point_id === selected.value)
    if (!item) return
    const value = decode(item)
    if (value === null || !/^(good|valid)$/i.test(item.quality)) {
      error.value = '当前测点值不可绘制（非数值或质量非有效）'
      return
    }
    const now = Date.now()
    history.value = [...history.value.filter(v => now - v.time < duration.value * 1000), { time: now, value }].slice(-300)
    error.value = ''
  } catch (cause) {
    if (current === generation) error.value = String(cause)
  } finally { busy = false }
}
watch([selected, () => props.deviceId], reset)
watch(() => props.active, active => { if (active) void poll() })
onMounted(() => { timer = setInterval(() => void poll(), 1000); void poll() })
onUnmounted(() => { generation++; if (timer) clearInterval(timer) })
</script>

<template>
  <section>
    <el-alert title="这是浏览器采样的短时趋势，不是历史数据库曲线；关闭页面后样本不保留。" type="info" :closable="false" />
    <el-space wrap style="margin: 12px 0">
      <el-select v-model="selected" filterable placeholder="选择数值测点" style="width: 250px">
        <el-option v-for="point in points" :key="point" :label="point" :value="point" />
      </el-select>
      <el-select v-model="duration" style="width: 130px">
        <el-option :value="30" label="30 秒" />
        <el-option :value="60" label="60 秒" />
        <el-option :value="300" label="5 分钟" />
      </el-select>
      <el-button @click="reset">清空曲线</el-button>
    </el-space>
    <el-alert v-if="error" :title="error" type="warning" show-icon :closable="false" />
    <div class="trend-frame">
      <svg viewBox="0 0 800 210" role="img" aria-label="实时测点短时趋势">
        <path d="M8 16 V192 H792" fill="none" stroke="currentColor" opacity=".3" />
        <path v-if="plot" :d="plot" fill="none" stroke="var(--el-color-primary)" stroke-width="2" />
      </svg>
    </div>
    <p>样本数：{{ history.length }} · 数值范围：{{ range }} · 每秒尝试获取一次最新值</p>
  </section>
</template>

<style scoped>
.trend-frame { border: 1px solid var(--el-border-color); border-radius: 6px; margin-top: 8px; }
.trend-frame svg { width: 100%; height: auto; display: block; }
</style>
