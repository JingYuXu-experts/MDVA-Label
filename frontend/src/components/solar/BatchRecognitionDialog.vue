<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Activity, Eclipse } from 'lucide-vue-next'
import { get, post } from '@/api/client'
import MDialog from '@/components/ui/MDialog.vue'
import { duration, estimateText, localFinish, type Channel, type Estimate, type Hardware, type Photo } from './projectTypes'

const props = defineProps<{open: boolean; project: string; photos: Photo[]; initialDevice: string}>()
const emit = defineEmits<{close: []; queued: [id: string, photos: string[]]}>()
const goals = [
  { id: 'flare-risk', label: '活动区识别与耀斑风险试算', hint: '使用 HMI 磁图定位活动区，并匹配 GOES 试算未来 24 小时风险。', icon: Activity },
  { id: 'coronal-hole', label: '冕洞识别', hint: '使用 AIA 193 Å 观测图识别冕洞范围。', icon: Eclipse },
] as const
type Goal = typeof goals[number]['id']
type State = 'eligible' | 'mismatch' | 'pending' | 'unsupported' | 'outside_time'
type DataType = 'hmi' | 'aia' | 'other' | 'unknown'
interface Check {
  id: string
  name: string
  data_type: DataType
  status: State
  reason: string
  can_confirm_channel: boolean
  can_supply_time: boolean
}
interface Selection { id: string; category?: Channel; observed_utc: string | null }
interface Review { target: Goal; photos: Check[]; selection: Selection[] }

const groups: { id: State; label: string }[] = [
  { id: 'eligible', label: '符合条件' },
  { id: 'mismatch', label: '目标不匹配' },
  { id: 'pending', label: '待确认通道或时间' },
  { id: 'unsupported', label: '输入不支持或不可用' },
  { id: 'outside_time', label: '不在观测时间范围内' },
]
const limit = 8
const showing = ref<Partial<Record<State | 'manual', number>>>({})
const target = ref<Goal | ''>('')
const device = ref('auto')
const hardware = ref<Hardware[]>([])
const estimate = ref<Estimate>()
const choices = ref<Record<string, Channel | ''>>({})
const times = ref<Record<string, string>>({})
const start = ref('')
const end = ref('')
const checked = ref<{ key: string; value: Review }>()
const checking = ref(false)
const refreshReview = ref(0)
const checkError = ref('')
const deviceError = ref('')
const submitError = ref('')
const submitting = ref(false)
const attempted = new Map<string, string>()

const goal = computed(() => goals.find(item => item.id === target.value))
const gpus = computed(() => hardware.value.filter(d => d.device === 'cuda'))
const availableGpus = computed(() => gpus.value.filter(d => d.available))
const canChooseDevice = computed(() => device.value === 'auto' || device.value === 'cpu' ||
  (device.value === 'all-gpus' ? availableGpus.value.length > 0 : availableGpus.value.some(gpu => gpu.id === device.value)),
)

function toUtc(value: string) {
  return value ? new Date(value + 'Z').toISOString() : null
}
const windowError = computed(() => {
  if (start.value && end.value && new Date(start.value + 'Z') > new Date(end.value + 'Z'))
    return '观测开始时间不能晚于结束时间。'
  return ''
})
const window = computed(() => ({ start_utc: toUtc(start.value), end_utc: toUtc(end.value) }))
const request = computed(() => ({
  project: props.project,
  goal: target.value,
  window: window.value,
  selection: props.photos.map(p => ({
    id: p.id,
    category: choices.value[p.id] || undefined,
    observed_utc: toUtc(times.value[p.id]),
  })),
}))
const checkKey = computed(() => JSON.stringify(request.value) + ':' + refreshReview.value)
const review = computed(() => checked.value?.value.target === target.value ? checked.value?.value : undefined)
const verified = computed(() => checked.value?.key === checkKey.value && !checking.value && !windowError.value
  ? checked.value.value : undefined)
const eligible = computed(() => verified.value?.selection.length ?? 0)

const grouped = computed(() => {
  const results: Record<State, Check[]> = { eligible: [], mismatch: [], pending: [], unsupported: [], outside_time: [] }
  for (const item of review.value?.photos || []) results[item.status].push(item)
  return results
})
const types = computed(() => {
  const counts = { hmi: 0, aia: 0, other: 0, unknown: 0 }
  for (const item of review.value?.photos || []) counts[item.data_type]++
  return counts
})
const manual = computed(() => review.value?.photos.filter(row => row.can_confirm_channel || row.can_supply_time) || [])
const orphans = computed(() => manual.value.filter(row => row.can_confirm_channel && !choices.value[row.id]))
const summary = computed(() => [
  { label: '已选图片', count: props.photos.length },
  { label: '符合识别目标', count: grouped.value.eligible.length, style: 'pass' },
  { label: '目标不匹配', count: grouped.value.mismatch.length, style: 'warn' },
  { label: '其他未入队', count: grouped.value.pending.length + grouped.value.unsupported.length + grouped.value.outside_time.length },
])

function clearRange() {
  start.value = ''
  end.value = ''
}
function preset(days: number) {
  const now = Date.now()
  start.value = new Date(now - days * 86400000).toISOString().slice(0, 19)
  end.value = new Date(now).toISOString().slice(0, 19)
}
function setKnownChannel(e: Event) {
  const select = e.target as HTMLSelectElement
  const value = select.value as Channel | ''
  if (value) for (const item of orphans.value) choices.value[item.id] = value
  select.value = ''
}

watch(() => props.open, open => {
  if (!open) return
  target.value = ''
  device.value = props.initialDevice
  choices.value = Object.fromEntries(props.photos.map(photo => [photo.id, '']))
  times.value = {}
  checked.value = undefined
  showing.value = {}
  checkError.value = ''
  submitError.value = ''
  attempted.clear()
  clearRange()
})

watch(() => [props.open, checkKey.value] as const, ([open], _, onCleanup) => {
  checking.value = false
  checkError.value = ''
  submitError.value = ''
  if (!open || !target.value || windowError.value || !props.photos.length) return
  const data = request.value
  const key = checkKey.value
  const controller = new AbortController()
  checking.value = true
  const timer = setTimeout(async () => {
    try {
      const result = await post<Review>('/solar/project/batch/precheck', data, { signal: controller.signal })
      if (!controller.signal.aborted) checked.value = { key, value: result }
    } catch (e) {
      if (!controller.signal.aborted) checkError.value = String(e)
    } finally {
      if (!controller.signal.aborted) checking.value = false
    }
  }, 180)
  onCleanup(() => { clearTimeout(timer); controller.abort() })
})

watch(() => [props.open, target.value, eligible.value, device.value] as const, ([open, goal, count, mode], _, onCleanup) => {
  estimate.value = undefined
  deviceError.value = ''
  if (!open || !goal) return
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  async function refresh() {
    try {
      const value = await get<Estimate & {hardware: Hardware[]}>('/solar/project/estimate',
        { AR: goal === 'flare-risk' ? count : 0, CH: goal === 'coronal-hole' ? count : 0, device: mode },
        { signal: controller.signal },
      )
      if (!controller.signal.aborted) {
        hardware.value = value.hardware
        estimate.value = value
        deviceError.value = ''
      }
    } catch (e) {
      if (!controller.signal.aborted) deviceError.value = String(e)
    }
    if (!controller.signal.aborted) timer = setTimeout(refresh, 2000)
  }
  void refresh()
  onCleanup(() => { controller.abort(); clearTimeout(timer) })
})

async function submit() {
  const result = verified.value
  if (!result?.selection.length || submitting.value || !canChooseDevice.value) return
  const signature = JSON.stringify([result.target, result.selection, window.value, device.value])
  const id = attempted.get(signature) || crypto.randomUUID().replace(/-/g, '')
  attempted.set(signature, id)
  const project = props.project
  submitting.value = true
  submitError.value = ''
  try {
    await post('/solar/project/batch', {
      project, id, selection: result.selection, device: device.value, goal: result.target, window: window.value,
    })
    if (props.project === project) emit('queued', id, result.selection.map(row => row.id))
  } catch (e) {
    submitError.value = String(e)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <MDialog :open="open" title="批量自动识别" :width="800" @close="!submitting && emit('close')">
    <fieldset :disabled="submitting" class="configuration">
      <section aria-labelledby="batch-target-label">
        <h3 id="batch-target-label">识别目标</h3>
        <div class="targets">
          <button v-for="item in goals" :key="item.id" type="button" class="target"
            :aria-pressed="target === item.id" @click="target = item.id">
            <component :is="item.icon" :size="21" aria-hidden="true"/>
            <span><strong>{{ item.label }}</strong><small>{{ item.hint }}</small></span>
          </button>
        </div>
        <p v-if="!target" class="hint">已选 {{ photos.length }} 张照片。请选择目标开始预检查。</p>
      </section>

      <template v-if="target">
        <section aria-labelledby="batch-stats-label" :aria-busy="checking">
          <div class="section-title">
            <h3 id="batch-stats-label">图片筛选结果</h3>
            <span v-if="checking" class="hint" role="status">正在核对 {{ photos.length }} 张照片…</span>
          </div>
          <template v-if="review">
            <div class="stats" :class="{ stale: !verified }">
              <div v-for="item in summary" :key="item.label" class="metric" :class="item.style">
                <strong>{{ item.count }}</strong><small>{{ item.label }}</small>
              </div>
            </div>
            <p class="type-stats">HMI 磁图 <b>{{ types.hmi }}</b> · AIA 观测图 <b>{{ types.aia }}</b>
              · 其他已识别 <b>{{ types.other }}</b> · 通道未知 <b>{{ types.unknown }}</b></p>
            <p v-if="verified && !eligible" class="warning" role="status">没有符合当前目标的照片。可核对通道和观测时间，或切换识别目标。</p>
          </template>
          <p v-if="checkError" class="error" role="alert">{{ checkError }} <button type="button" @click="refreshReview++">重新检查</button></p>
        </section>

        <section v-if="review" aria-labelledby="batch-groups-label">
          <h3 id="batch-groups-label">查看图片分类</h3>
          <details v-for="group in groups.filter(g => grouped[g.id].length)" :key="group.id">
            <summary>{{ group.label }} <span>{{ grouped[group.id].length }} 张</span></summary>
            <ul class="rows">
              <li v-for="item in grouped[group.id].slice(0, showing[group.id] || limit)" :key="item.id">
                <strong>{{ item.name }}</strong><small v-if="item.reason">{{ item.reason }}</small>
              </li>
            </ul>
            <button v-if="grouped[group.id].length > (showing[group.id] || limit)" type="button" class="more"
              @click="showing[group.id] = (showing[group.id] || limit) + 20">查看更多</button>
          </details>
          <details v-if="manual.length">
            <summary>逐张确认 <span>{{ manual.length }} 张可补充</span></summary>
            <p class="hint">请按原始来源确认仪器、通道和完整日面。只有实际匹配当前目标的照片才会入队。</p>
            <label v-if="orphans.length" class="field">为未确认照片统一指定通道
              <select @change="setKnownChannel">
                <option value="">请选择</option><option value="AR">HMI 磁图</option><option value="CH">AIA 193 Å</option>
              </select>
            </label>
            <ul class="rows">
              <li v-for="item in manual.slice(0, showing.manual || limit)" :key="item.id">
                <strong>{{ item.name }}</strong>
                <div class="manual-fields">
                  <label v-if="item.can_confirm_channel" class="field">观测通道
                    <select v-model="choices[item.id]" :aria-label="item.name + ' 通道'">
                      <option value="">待确认</option><option value="AR">HMI 磁图</option><option value="CH">AIA 193 Å</option>
                    </select>
                  </label>
                  <label v-if="item.can_supply_time" class="field">观测时间（UTC）
                    <input v-model="times[item.id]" type="datetime-local" step="1" :aria-label="item.name + ' 观测时间 UTC'">
                  </label>
                </div>
              </li>
            </ul>
            <button v-if="manual.length > (showing.manual || limit)" type="button" class="more"
              @click="showing.manual = (showing.manual || limit) + 20">查看更多</button>
          </details>
        </section>

        <section aria-labelledby="batch-device-label">
          <h3 id="batch-device-label">处理设备</h3>
          <select v-model="device" aria-label="处理设备" class="device">
            <option value="auto">自动（优先使用兼容显卡）</option>
            <option value="cpu">CPU</option>
            <option v-for="gpu in gpus" :key="gpu.id" :value="gpu.id" :disabled="!gpu.available">
              GPU {{ gpu.index }} · {{ gpu.name }}{{ gpu.available ? '' : '（当前不可用）' }}
            </option>
            <option value="all-gpus" :disabled="!availableGpus.length">全部兼容显卡（并行处理）</option>
          </select>
          <p v-for="gpu in gpus.filter(d => d.reason)" :key="gpu.id" class="warning">{{ gpu.name }} · {{ gpu.reason }}。可选择 CPU 执行。</p>
          <p v-if="deviceError" class="error" role="alert">{{ deviceError }}</p>
          <p v-if="eligible" class="hint"><b>预计处理时间</b> · {{ estimateText(estimate) }}
            <span v-if="estimate?.finish_utc"> · 预计 {{ localFinish(estimate) }} 完成</span><br>
            前方任务 {{ estimate?.ahead ?? '…' }} 个 · 含识别排队与处理时间
          </p>
          <ul v-if="eligible && estimate?.devices.length" class="device-times">
            <li v-for="d in estimate.devices" :key="d.id">{{ d.name }} · {{ d.remaining }} 张
              <span v-if="estimate.devices.length > 1"> · {{ d.seconds != null ? duration(d.seconds) : '等待估算' }}</span>
            </li>
          </ul>
        </section>

        <section aria-labelledby="batch-window-label">
          <div class="section-title"><h3 id="batch-window-label">观测时间（UTC）</h3>
            <div class="presets">
              <button type="button" @click="preset(1)">近 24 小时</button>
              <button type="button" @click="preset(7)">近 7 天</button>
              <button type="button" @click="clearRange">清空范围</button>
            </div>
          </div>
          <div class="bounds">
            <label class="field">开始时间（含）<input v-model="start" type="datetime-local" step="1"></label>
            <label class="field">结束时间（含）<input v-model="end" type="datetime-local" step="1"></label>
          </div>
          <p v-if="windowError" class="error" role="alert">{{ windowError }}</p>
          <p v-else class="hint">当前范围 · {{ start ? start.replace('T', ' ') : '不限开始' }} 至 {{ end ? end.replace('T', ' ') : '不限结束' }} UTC</p>
          <p v-if="target === 'flare-risk'" class="hint">试算需要原生 1024 × 1024 的完整日面 HMI 磁图和观测时间。是否能给出风险值取决于 GOES 资料覆盖。</p>
        </section>
      </template>
      <p v-if="submitError" class="error" role="alert">{{ submitError }} <button type="button" @click="refreshReview++">重新检查</button></p>
    </fieldset>
    <template #footer>
      <div class="footer">
        <span class="hint">{{ goal?.label || '请先选择识别目标' }}</span>
        <div class="footer-actions">
          <button :disabled="submitting" type="button" @click="emit('close')">取消</button>
          <button class="accent" type="button" :disabled="submitting || !eligible || !canChooseDevice" @click="submit">
            {{ submitting ? '正在登记…' : '确认并加入队列（' + eligible + ' 张）' }}
          </button>
        </div>
      </div>
    </template>
  </MDialog>
</template>

<style scoped>
.configuration { padding: 0; margin: 0; border: 0; min-width: 0; }
section + section { border-top: 1px solid var(--border); margin-top: 16px; padding-top: 16px; }
h3 { margin: 0 0 9px; font-size: 14px; }
button, select, input { font: inherit; color: var(--text-primary); border: 1px solid var(--border); background: var(--bg-panel); border-radius: 7px; padding: 7px 9px; min-width: 0; }
button { cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
button:focus-visible, summary:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid #48aeb2; outline-offset: 2px; }
.targets, .bounds { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.target { display: flex; align-items: flex-start; gap: 9px; padding: 12px; text-align: left; }
.target svg { flex: none; color: #53b9bd; }
.target strong, .target small { display: block; }
.target small { margin-top: 5px; line-height: 1.5; color: var(--text-secondary); }
.target[aria-pressed="true"] { border-color: #48aeb2; background: #19414470; box-shadow: inset 0 0 0 1px #48aeb2; }
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.stats.stale { opacity: .5; }
.metric { display: grid; gap: 3px; padding: 10px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-base); }
.metric strong { font-size: 24px; font-variant-numeric: tabular-nums; }
.metric small, .hint, .type-stats { color: var(--text-secondary); font-size: 12px; line-height: 1.6; }
.pass strong { color: #62dfae; }
.warn strong, .warning { color: #f1b371; }
.warning, .error { font-size: 13px; line-height: 1.5; }
.error { color: var(--danger); }
.section-title, .footer, .presets, .footer-actions { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.section-title, .footer { justify-content: space-between; }
.footer { width: 100%; }
.footer-actions { margin-left: auto; }
.accent { background: #17747b; border-color: #318b90; color: white; font-weight: 600; }
details { border: 1px solid var(--border); border-radius: 7px; margin-top: 6px; padding: 8px 10px; }
summary { cursor: pointer; color: var(--text-primary); font-size: 13px; }
summary span { float: right; color: var(--text-secondary); }
.rows { list-style: none; margin: 4px 0; padding: 0; }
.rows li { padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 12px; overflow-wrap: anywhere; }
.rows small { display: block; color: var(--text-secondary); margin-top: 3px; }
.more { margin-top: 5px; font-size: 12px; }
.manual-fields { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.field { display: grid; align-items: center; gap: 4px; font-size: 12px; color: var(--text-secondary); }
.manual-fields .field { margin-top: 5px; }
.device { width: 100%; max-width: 420px; }
.device-times { font-size: 12px; padding-inline-start: 18px; }
.presets button { font-size: 12px; }
@media (max-width: 580px) {
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .targets, .bounds { grid-template-columns: minmax(0, 1fr); }
}
</style>
